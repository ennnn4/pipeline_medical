"""생성 결과(package.json) → PostgreSQL 적재 — 통합 1단계 핵심.

GPT 검토 반영:
 - generation_jobs(status·idempotency_key·raw_output·retry) 로 중복생성 방지·실패추적.
 - 구조(script/version/block/sentence/claim-휴리스틱)는 1차 트랜잭션. LLM 근거검증은 2차(별도, evidence/llm_verify).
 - app_rw + tenant_conn(RLS)로 적재(owner 아님) → 라이브 RLS DB에서도 동작. 병원은 이미 존재해야 함.

ensure_gen_schema(owner_engine): generation_jobs 테이블 additive 생성(비파괴, full reset 불필요).
ingest_package(engine, hospital_id, topic, script_list, ...): 적재 후 새 version_id 반환(idempotent).
"""
import io, json, hashlib, uuid
from sqlalchemy import text
from store.repositories import tenant_conn
from nlp.segment import segment, SEGMENTER_VERSION
from store.migrate import block_type_of, is_claim, claim_type_of

_TENANT_SET = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

_DDL = """
CREATE TABLE IF NOT EXISTS generation_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hospital_id uuid NOT NULL REFERENCES hospitals(id),
  topic text NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  idempotency_key text NOT NULL,
  version_id uuid,
  raw_output jsonb,
  error_message text,
  retry_count int NOT NULL DEFAULT 0,
  created_by_membership_id uuid,
  prompt_version text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (hospital_id, idempotency_key)
);
"""

def _gen_policies():
    return [
        "ALTER TABLE generation_jobs ENABLE ROW LEVEL SECURITY;",
        "ALTER TABLE generation_jobs FORCE ROW LEVEL SECURITY;",
        "DROP POLICY IF EXISTS gj_rw ON generation_jobs;",
        f"CREATE POLICY gj_rw ON generation_jobs TO app_rw "
        f"USING (hospital_id = {_TENANT_SET}) WITH CHECK (hospital_id = {_TENANT_SET});",
        "DROP POLICY IF EXISTS gj_def ON generation_jobs;",
        "CREATE POLICY gj_def ON generation_jobs TO app_owner USING (true) WITH CHECK (true);",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON generation_jobs TO app_rw;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON generation_jobs TO app_owner;",
    ]

def ensure_gen_schema(owner_engine):
    with owner_engine.begin() as cn:
        cn.execute(text(_DDL))
        for s in _gen_policies():
            cn.execute(text(s))

def _idem(hospital_id, topic, script_list):
    payload = json.dumps({"h": str(hospital_id), "t": topic, "s": script_list},
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def ingest_package(engine, hospital_id, topic, script_list, raw=None, membership_id=None, prompt_version=None):
    """script_list: [{block, scene, say, tags}]. 새 immutable 버전으로 적재. 동일 내용 재요청은 기존 반환.
    반환: {"version_id": uuid, "status": str, "reused": bool, "blocks": n, "claims": n}."""
    idem = _idem(hospital_id, topic, script_list)
    with tenant_conn(engine, hospital_id, membership_id=membership_id) as cn:
        ex = cn.execute(text("select id, status, version_id from generation_jobs "
                             "where hospital_id=:h and idempotency_key=:k"),
                        {"h": hospital_id, "k": idem}).first()
        if ex and ex.status == "completed" and ex.version_id:
            return {"version_id": ex.version_id, "status": "completed", "reused": True, "blocks": 0, "claims": 0}
        job_id = ex.id if ex else uuid.uuid4()
        if ex:
            cn.execute(text("update generation_jobs set status='parsing', retry_count=retry_count+1, "
                            "updated_at=now() where id=:j"), {"j": job_id})
        else:
            cn.execute(text("insert into generation_jobs(id,hospital_id,topic,status,idempotency_key,"
                            "raw_output,created_by_membership_id,prompt_version) "
                            "values(:j,:h,:t,'parsing',:k,cast(:r as jsonb),:m,:pv)"),
                       {"j": job_id, "h": hospital_id, "t": topic, "k": idem,
                        "r": json.dumps(raw, ensure_ascii=False) if raw is not None else None,
                        "m": membership_id, "pv": prompt_version})
        try:
            # script(topic별) 확보 → 다음 version_no
            sc = cn.execute(text("select id from scripts where hospital_id=:h and topic=:t order by created_at limit 1"),
                            {"h": hospital_id, "t": topic}).scalar()
            if not sc:
                sc = uuid.uuid4()
                cn.execute(text("insert into scripts(id,hospital_id,topic) values(:s,:h,:t)"),
                           {"s": sc, "h": hospital_id, "t": topic})
            maxno = cn.execute(text("select coalesce(max(version_no),0) from script_versions "
                                    "where hospital_id=:h and script_id=:s"), {"h": hospital_id, "s": sc}).scalar()
            v = uuid.uuid4()
            cn.execute(text("insert into script_versions(id,hospital_id,script_id,version_no,source,creation_reason) "
                            "values(:v,:h,:s,:n,'ai','생성 파이프라인')"),
                       {"v": v, "h": hospital_id, "s": sc, "n": maxno + 1})
            cn.execute(text("insert into version_approval_states(id,hospital_id,version_id,status) "
                            "values(:i,:h,:v,'none')"), {"i": uuid.uuid4(), "h": hospital_id, "v": v})
            nb = nc = 0
            for i, b in enumerate(script_list):
                say = (b.get("say") or "").strip()
                if not say:
                    continue
                bid = uuid.uuid4()
                cn.execute(text("insert into script_blocks(id,hospital_id,version_id,stable_block_key,order_index,"
                                "block_type,scene,text,metadata) values(:b,:h,:v,:k,:o,:bt,:sc,:tx,cast(:md as jsonb))"),
                           {"b": bid, "h": hospital_id, "v": v, "k": f"blk_{i+1}", "o": i,
                            "bt": block_type_of(b.get("block") or ""), "sc": (b.get("scene") or "")[:2000], "tx": say,
                            "md": json.dumps({"block_label": b.get("block") or "", "tags": b.get("tags") or []}, ensure_ascii=False)})
                nb += 1
                ci = 0
                for s0, s1, st in segment(say):
                    sid = uuid.uuid4()
                    cn.execute(text("insert into script_sentences(id,hospital_id,version_id,block_id,sentence_index,"
                                    "text,start_offset,end_offset,offset_unit,segmenter_version) "
                                    "values(:s,:h,:v,:b,:i,:tx,:a,:z,'codepoint',:sv)"),
                               {"s": sid, "h": hospital_id, "v": v, "b": bid, "i": ci, "tx": st,
                                "a": s0, "z": s1, "sv": SEGMENTER_VERSION})
                    if is_claim(st):
                        cn.execute(text("insert into claims(id,hospital_id,version_id,sentence_id,claim_index,"
                                        "claim_text,claim_type,detection_method) values(:c,:h,:v,:s,0,:tx,:ct,'llm')"),
                                   {"c": uuid.uuid4(), "h": hospital_id, "v": v, "s": sid,
                                    "tx": st[:2000], "ct": claim_type_of(st)})
                        nc += 1
                    ci += 1
            cn.execute(text("update scripts set current_version_id=:v, updated_at=now() where id=:s and hospital_id=:h"),
                       {"v": v, "s": sc, "h": hospital_id})
            cn.execute(text("update generation_jobs set status='completed', version_id=:v, updated_at=now() where id=:j"),
                       {"v": v, "j": job_id})
            return {"version_id": v, "status": "completed", "reused": False, "blocks": nb, "claims": nc}
        except Exception as e:
            try:
                cn.execute(text("update generation_jobs set status='failed', error_message=:e, updated_at=now() where id=:j"),
                           {"e": str(e)[:500], "j": job_id})
            except Exception:
                pass          # aborted tx면 롤백되어 job 흔적 없음(원인 에러를 가리지 않게)
            raise
