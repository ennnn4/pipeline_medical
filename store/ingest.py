"""생성 결과(package.json) → PostgreSQL 적재 — 통합 1단계 (GPT P0 검토 반영판).

GPT P0 반영:
 - 대본 식별은 topic이 아니라 **script_id(UUID)**. target_script_id=None→새 대본, 있으면 새 immutable 버전.
 - **request_idempotency_key**(더블클릭/재전송 방지, UNIQUE) 와 **content_hash**(감사·동일결과 확인) 분리.
 - **generation_job을 run.py 실행 전에** pending 생성(TX1 커밋) → generating/generated → ingest(TX2 completed)
   → 예외 시 별도 TX로 failed. 서버 급사 대비 stale 감지(heartbeat).
 - 구조 적재(1차 tx)와 LLM 근거검증(2차, evidence/llm_verify) 분리 유지.
 - app_rw + tenant_conn(RLS)로 적재. 병원은 이미 존재해야 함.

상태: pending→generating→generated→ingesting→completed / failed / cancelled / stale
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
  script_id uuid,
  topic text NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  phase text,
  request_idempotency_key text NOT NULL,
  content_hash text,
  version_id uuid,
  generation_reason text DEFAULT 'initial',
  raw_output jsonb,
  error_code text,
  error_message text,
  retry_count int NOT NULL DEFAULT 0,
  created_by_membership_id uuid,
  prompt_version text,
  started_at timestamptz,
  heartbeat_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
"""

# 기존 테이블(구 스키마) 안전 adoption용 ALTER (IF NOT EXISTS)
_ADOPT = [
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS script_id uuid;",
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS phase text;",
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS request_idempotency_key text;",
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS content_hash text;",
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS generation_reason text DEFAULT 'initial';",
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS error_code text;",
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS started_at timestamptz;",
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz;",
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS finished_at timestamptz;",
    # 구 스키마의 idempotency_key NOT NULL 완화(신규 insert는 request_idempotency_key만 사용)
    "ALTER TABLE generation_jobs ALTER COLUMN idempotency_key DROP NOT NULL;",
]

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
        # request_idempotency_key 유니크(더블클릭/재전송 방지). 부분 인덱스(널 허용).
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_genjobs_reqkey ON generation_jobs(hospital_id, request_idempotency_key) "
        "WHERE request_idempotency_key IS NOT NULL;",
    ]

def ensure_gen_schema(owner_engine):
    with owner_engine.begin() as cn:
        cn.execute(text(_DDL))
        for s in _ADOPT:
            try:
                cn.execute(text(s))
            except Exception:
                pass
        for s in _gen_policies():
            cn.execute(text(s))

def _content_hash(script_list):
    canon = json.dumps(script_list, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()

# ── job 생명주기 (각자 별도 트랜잭션 = 실패 추적 보존) ──
def create_job(engine, hospital_id, topic, request_key, target_script_id=None,
               reason="initial", membership_id=None, prompt_version=None):
    """run.py 실행 '전에' pending job을 만들고 COMMIT. 동일 request_key 재요청은 기존 job 반환.
    반환: {"job_id", "status", "reused"}."""
    with tenant_conn(engine, hospital_id, membership_id=membership_id) as cn:
        ex = cn.execute(text("select id, status from generation_jobs "
                             "where hospital_id=:h and request_idempotency_key=:k"),
                        {"h": hospital_id, "k": request_key}).first()
        if ex:
            return {"job_id": ex.id, "status": ex.status, "reused": True}
        jid = uuid.uuid4()
        cn.execute(text("insert into generation_jobs(id,hospital_id,script_id,topic,status,phase,"
                        "request_idempotency_key,generation_reason,created_by_membership_id,prompt_version) "
                        "values(:j,:h,:sc,:t,'pending','created',:k,:rs,:m,:pv)"),
                   {"j": jid, "h": hospital_id, "sc": target_script_id, "t": topic, "k": request_key,
                    "rs": reason, "m": membership_id, "pv": prompt_version})
    return {"job_id": jid, "status": "pending", "reused": False}

def mark_job(engine, hospital_id, job_id, status, phase=None, error_code=None, error_message=None,
             version_id=None, content_hash=None, script_id=None, membership_id=None, finished=False, started=False):
    """job 상태를 별도 트랜잭션으로 갱신(콘텐츠 적재 성패와 독립)."""
    sets = ["status=:st", "updated_at=now()", "heartbeat_at=now()"]
    p = {"st": status, "j": job_id, "h": hospital_id}
    if phase is not None: sets.append("phase=:ph"); p["ph"] = phase
    if error_code is not None: sets.append("error_code=:ec"); p["ec"] = error_code
    if error_message is not None: sets.append("error_message=:em"); p["em"] = error_message[:800]
    if version_id is not None: sets.append("version_id=:v"); p["v"] = version_id
    if content_hash is not None: sets.append("content_hash=:ch"); p["ch"] = content_hash
    if script_id is not None: sets.append("script_id=:sc"); p["sc"] = script_id
    if started: sets.append("started_at=now()")
    if finished: sets.append("finished_at=now()")
    with tenant_conn(engine, hospital_id, membership_id=membership_id) as cn:
        cn.execute(text(f"update generation_jobs set {', '.join(sets)} where id=:j and hospital_id=:h"), p)

def reap_stale(engine, hospital_id, older_than_sec=1800):
    """heartbeat가 오래 멈춘 generating/ingesting job → stale."""
    with tenant_conn(engine, hospital_id) as cn:
        cn.execute(text("update generation_jobs set status='stale', updated_at=now() "
                        "where hospital_id=:h and status in ('generating','ingesting') "
                        "and coalesce(heartbeat_at, started_at, created_at) < now() - (:s || ' seconds')::interval"),
                   {"h": hospital_id, "s": str(older_than_sec)})

# ── 콘텐츠 적재 (별도 트랜잭션, script_id 기반) ──
def ingest_content(engine, hospital_id, job_id, topic, script_list, target_script_id=None, membership_id=None):
    """생성 결과를 script(target_script_id or 새로)/새 immutable 버전/블록/문장/claim(휴리스틱)으로 적재.
    성공 시 job completed + version_id + content_hash. 반환: {"version_id","blocks","claims","content_hash","reused"}."""
    ch = _content_hash(script_list)
    with tenant_conn(engine, hospital_id, membership_id=membership_id) as cn:
        # 동일 content가 이미 이 job에 적재됐으면 재적재 방지
        done = cn.execute(text("select version_id, content_hash from generation_jobs where id=:j and hospital_id=:h"),
                          {"j": job_id, "h": hospital_id}).first()
        if done and done.version_id and done.content_hash == ch:
            return {"version_id": done.version_id, "blocks": 0, "claims": 0, "content_hash": ch, "reused": True}
        # script 식별: target_script_id(있으면 그 대본의 새 버전) or 새 대본
        if target_script_id:
            sc = cn.execute(text("select id from scripts where id=:s and hospital_id=:h"),
                            {"s": target_script_id, "h": hospital_id}).scalar()
            if not sc:
                raise ValueError("target_script_id가 이 병원에 없음")
        else:
            sc = uuid.uuid4()
            cn.execute(text("insert into scripts(id,hospital_id,topic) values(:s,:h,:t)"),
                       {"s": sc, "h": hospital_id, "t": topic})
        maxno = cn.execute(text("select coalesce(max(version_no),0) from script_versions "
                                "where hospital_id=:h and script_id=:s"), {"h": hospital_id, "s": sc}).scalar()
        v = uuid.uuid4()
        cn.execute(text("insert into script_versions(id,hospital_id,script_id,version_no,source,creation_reason) "
                        "values(:v,:h,:s,:n,'ai','생성 파이프라인')"),
                   {"v": v, "h": hospital_id, "s": sc, "n": maxno + 1})
        cn.execute(text("insert into version_approval_states(id,hospital_id,version_id,status) values(:i,:h,:v,'none')"),
                   {"i": uuid.uuid4(), "h": hospital_id, "v": v})
        nb = ncl = 0
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
                    ncl += 1
                ci += 1
        cn.execute(text("update scripts set current_version_id=:v, updated_at=now() where id=:s and hospital_id=:h"),
                   {"v": v, "s": sc, "h": hospital_id})
        cn.execute(text("update generation_jobs set status='completed', phase='ingested', script_id=:sc, "
                        "version_id=:v, content_hash=:ch, finished_at=now(), heartbeat_at=now(), updated_at=now() "
                        "where id=:j and hospital_id=:h"),
                   {"sc": sc, "v": v, "ch": ch, "j": job_id, "h": hospital_id})
        return {"version_id": v, "blocks": nb, "claims": ncl, "content_hash": ch, "reused": False}
