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
from store.repositories import tenant_conn, Conflict
from nlp.segment import segment, SEGMENTER_VERSION
from store.migrate import block_type_of, is_claim, claim_type_of

CLAIM_DETECTOR_VERSION = "heuristic-ko-1"      # is_claim 휴리스틱 버전(감사·재현용)

class InvalidJobState(Exception):
    """job이 적재 가능한 상태가 아님(pending/failed/cancelled/stale/completed 등)."""

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

# 기존 테이블(구 스키마) 안전 adoption — 조건부(존재검사) DDL이라 예외 없음(트랜잭션 abort 방지).
# 광범위한 except pass 금지: 예상못한 권한/타입/문법 오류를 숨기지 않는다(GPT 리뷰).
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
    # 자료 스냅샷 봉인 메타(P2-1b) — 어떤 자료 세트로 생성했는지 봉인 시점·개수·매니페스트 해시(sha256 canonical)
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS material_snapshot_at timestamptz;",
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS material_snapshot_count int;",
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS material_snapshot_hash text;",
    # 실행권 소유자 토큰(P2-1 마감) — CAS로 획득한 워커만 이후 상태전이(stale 워커의 늦은 완료 차단)
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS worker_token text;",
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS claimed_at timestamptz;",
    # 구 스키마에 idempotency_key(NOT NULL)가 있을 때만 완화 — 신규 DB엔 없으니 조건부(오류 없음)
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns "
    "WHERE table_schema='public' AND table_name='generation_jobs' AND column_name='idempotency_key') "
    "THEN ALTER TABLE generation_jobs ALTER COLUMN idempotency_key DROP NOT NULL; END IF; END $$;",
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
        # 유니크 인덱스 전, 기존 데이터에 중복 키가 있으면 명확한 메시지로 실패(단순 unique violation 방지)
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM generation_jobs WHERE request_idempotency_key IS NOT NULL "
        "GROUP BY hospital_id, request_idempotency_key HAVING count(*) > 1) "
        "THEN RAISE EXCEPTION 'generation_jobs에 중복 request_idempotency_key가 있어 유니크 인덱스 생성 불가'; END IF; END $$;",
        # request_idempotency_key 유니크 — 이름 같아도 정의 다를 수 있으니 DROP 후 non-partial 재생성(ON CONFLICT 매칭)
        "DROP INDEX IF EXISTS uq_genjobs_reqkey;",
        "CREATE UNIQUE INDEX uq_genjobs_reqkey ON generation_jobs(hospital_id, request_idempotency_key);",
        # 상태값 화이트리스트 CHECK — 앱 검증 외 DB 최종 방어선(오타·미래 코드 실수 차단). 멱등 add.
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_genjobs_status') THEN "
        "ALTER TABLE generation_jobs ADD CONSTRAINT chk_genjobs_status CHECK "
        "(status IN ('pending','generating','generated','ingesting','completed','failed','cancelled','stale')) NOT VALID; "
        "END IF; END $$;",
        "ALTER TABLE generation_jobs VALIDATE CONSTRAINT chk_genjobs_status;",
        # request key NOT NULL — 혹시 남은 NULL(구식 adoption)은 랜덤 UUID로 채운 뒤 강제.
        "UPDATE generation_jobs SET request_idempotency_key = gen_random_uuid()::text WHERE request_idempotency_key IS NULL;",
        "ALTER TABLE generation_jobs ALTER COLUMN request_idempotency_key SET NOT NULL;",
        # 병원당 동시 '실행 중' job 1개(P2-1 마감·GPT §d). 병원 간 병렬은 허용. pending/완료/실패는 제외.
        # 인덱스 생성 전, 기존 중복 active(크래시 잔여)는 병원별 최신 1개만 남기고 stale 정리.
        "UPDATE generation_jobs SET status='stale', updated_at=now() "
        "WHERE status IN ('generating','generated','ingesting') AND id NOT IN ("
        "  SELECT DISTINCT ON (hospital_id) id FROM generation_jobs "
        "  WHERE status IN ('generating','generated','ingesting') ORDER BY hospital_id, updated_at DESC);",
        "DROP INDEX IF EXISTS uq_genjobs_one_active;",
        "CREATE UNIQUE INDEX uq_genjobs_one_active ON generation_jobs(hospital_id) "
        "WHERE status IN ('generating','generated','ingesting');",
    ]

def ensure_gen_schema(owner_engine):
    with owner_engine.begin() as cn:
        cn.execute(text(_DDL))
        for s in _ADOPT:          # 조건부 DDL — 예외 무시 안 함(문제 시 즉시 드러남)
            cn.execute(text(s))
        for s in _gen_policies():
            cn.execute(text(s))

def _content_hash(script_list):
    canon = json.dumps(script_list, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()

# ── job 생명주기 (각자 별도 트랜잭션 = 실패 추적 보존) ──
def _norm_uuid(v):
    return None if v is None else str(uuid.UUID(str(v)))

def create_job(engine, hospital_id, topic, request_key, target_script_id=None,
               reason="initial", membership_id=None, prompt_version=None):
    """run.py 실행 '전에' pending job을 원자적으로 만들고 COMMIT.
    동일 request_key 동시 요청도 job 하나만 생성(ON CONFLICT DO NOTHING) — 경합 안전.
    같은 key인데 요청 내용(topic/script_id/reason/prompt)이 다르면 Conflict.
    request_key·script_id는 canonical UUID로 정규화(대소문자 차이로 다른 값 저장 방지)."""
    try:
        request_key = str(uuid.UUID(str(request_key)))     # canonical, 형식 강제(서버 신뢰 안 함)
    except (TypeError, ValueError):
        raise ValueError("request_idempotency_key는 UUID여야 함")
    try:
        norm_sc = _norm_uuid(target_script_id)
    except (TypeError, ValueError):
        raise ValueError("target_script_id는 UUID여야 함")
    with tenant_conn(engine, hospital_id, membership_id=membership_id) as cn:
        row = cn.execute(text(
            "insert into generation_jobs(id,hospital_id,script_id,topic,status,phase,"
            "request_idempotency_key,generation_reason,created_by_membership_id,prompt_version) "
            "values(:j,:h,:sc,:t,'pending','created',:k,:rs,:m,:pv) "
            "on conflict (hospital_id, request_idempotency_key) do nothing "
            "returning id, status"),
            {"j": uuid.uuid4(), "h": hospital_id, "sc": norm_sc, "t": topic, "k": request_key,
             "rs": reason, "m": membership_id, "pv": prompt_version}).first()
        if row:
            return {"job_id": row.id, "status": row.status, "reused": False}
        ex = cn.execute(text("select id, status, topic, script_id, generation_reason, prompt_version "
                             "from generation_jobs where hospital_id=:h and request_idempotency_key=:k"),
                        {"h": hospital_id, "k": request_key}).first()
        if ex is None:
            raise Conflict("idempotency 충돌 행을 조회할 수 없음")
        ex_sc = None if ex.script_id is None else str(ex.script_id)
        same = (ex.topic == topic and ex_sc == norm_sc
                and ex.generation_reason == reason and ex.prompt_version == prompt_version)
        if not same:      # None↔UUID 포함 완전 비교(새 대본 vs 재생성 오인 방지)
            raise Conflict("동일 request_key로 다른 요청 내용")
        return {"job_id": ex.id, "status": ex.status, "reused": True}

def mark_job(engine, hospital_id, job_id, status, allowed_from=None, phase=None, error_code=None,
             error_message=None, version_id=None, content_hash=None, script_id=None, membership_id=None,
             finished=False, started=False, worker_token=None):
    """job 상태 전이(별도 트랜잭션). allowed_from(집합)이 주어지면 그 상태에서만 전이(compare-and-set)
    → 정상 완료(completed) job을 늦은 예외가 failed로 덮는 것 방지. 전이 안 되면 False."""
    sets = ["status=:st", "updated_at=now()", "heartbeat_at=now()"]
    p = {"st": status, "j": job_id, "h": hospital_id}
    if phase is not None: sets.append("phase=:ph"); p["ph"] = phase
    if error_code is not None: sets.append("error_code=:ec"); p["ec"] = error_code
    if error_message is not None: sets.append("error_message=:em"); p["em"] = error_message[:800]
    if version_id is not None: sets.append("version_id=:v"); p["v"] = version_id
    if content_hash is not None: sets.append("content_hash=:ch"); p["ch"] = content_hash
    if script_id is not None: sets.append("script_id=:sc"); p["sc"] = script_id
    if started: sets.append("started_at=coalesce(started_at, now())")   # 최초만 기록
    if finished: sets.append("finished_at=now()")
    where = "id=:j and hospital_id=:h"
    if allowed_from:
        where += " and status = any(:af)"; p["af"] = list(allowed_from)
    if worker_token is not None:      # 실행권 소유자만 상태전이(stale 워커의 늦은 완료 차단)
        where += " and worker_token = :wt"; p["wt"] = worker_token
    with tenant_conn(engine, hospital_id, membership_id=membership_id) as cn:
        row = cn.execute(text(f"update generation_jobs set {', '.join(sets)} where {where} returning id"), p).first()
        return row is not None

def claim_job(engine, hospital_id, job_id, worker_token, membership_id=None):
    """실행권 원자적 획득(P2-1 마감): pending & 스냅샷 봉인완료(material_snapshot_at) →
    generating + worker_token 기록. 병원당 active job 유니크 위반이면 hospital_busy.
    반환 (acquired: bool, reason: str|None). reason ∈ hospital_busy/not_found/not_sealed/not_pending."""
    from sqlalchemy.exc import IntegrityError
    try:
        with tenant_conn(engine, hospital_id, membership_id=membership_id) as cn:
            row = cn.execute(text(
                "update generation_jobs set status='generating', phase='run.py', worker_token=:w, "
                "claimed_at=now(), started_at=coalesce(started_at, now()), heartbeat_at=now(), updated_at=now() "
                "where id=:j and hospital_id=:h and status='pending' and material_snapshot_at is not null "
                "returning id"), {"w": worker_token, "j": job_id, "h": hospital_id}).first()
            if row:
                return (True, None)
            st = cn.execute(text("select status, material_snapshot_at from generation_jobs "
                                 "where id=:j and hospital_id=:h"), {"j": job_id, "h": hospital_id}).first()
            if st is None:
                return (False, "not_found")
            if st.material_snapshot_at is None:
                return (False, "not_sealed")
            return (False, "not_pending")
    except IntegrityError:
        return (False, "hospital_busy")   # 병원당 active job 부분유니크 위반(다른 job 실행 중)

def heartbeat_job(engine, hospital_id, job_id, worker_token):
    """실행 중 워커가 살아있음을 표시(heartbeat_at 갱신) — reap_stale이 살아있는 워커를 stale로 오판하지
    않게. worker_token 일치할 때만(실행권 소유자). 반환: 갱신 여부."""
    with tenant_conn(engine, hospital_id) as cn:
        r = cn.execute(text("update generation_jobs set heartbeat_at=now(), updated_at=now() "
                            "where id=:j and hospital_id=:h and worker_token=:w "
                            "and status in ('generating','generated','ingesting') returning id"),
                       {"j": job_id, "h": hospital_id, "w": worker_token}).first()
        return r is not None

def reap_stale(engine, hospital_id, older_than_sec=1800):
    """heartbeat가 오래 멈춘 active(generating/generated/ingesting) job → stale.
    worker_token을 무효화(NULL)해 늦은 워커가 뒤늦게 상태전이/적재하지 못하게 함(GPT)."""
    with tenant_conn(engine, hospital_id) as cn:
        r = cn.execute(text("update generation_jobs set status='stale', worker_token=null, updated_at=now() "
                        "where hospital_id=:h and status in ('generating','generated','ingesting') "
                        "and coalesce(heartbeat_at, started_at, created_at) < now() - (:s || ' seconds')::interval"),
                   {"h": hospital_id, "s": str(older_than_sec)})
    if r.rowcount:                          # 죽은 워커 회수 건수(관측) — 정상 0
        try:
            from services.observability import emit
            emit("reap_stale", reaped=r.rowcount)
        except Exception:
            pass

# ── 콘텐츠 적재 (별도 트랜잭션, job 잠금 + job.script_id 기준) ──
def ingest_content(engine, hospital_id, job_id, script_list, membership_id=None):
    """생성 결과를 job.script_id(있으면 그 대본의 새 버전, 없으면 새 대본)의 새 immutable 버전으로 적재.
    topic·script_id 모두 job에서 읽음(호출자가 job과 다른 값을 넘길 여지 원천 차단).
    - job 행 FOR UPDATE 잠금 → 같은 job 이중 적재/중복 버전 방지.
    - 적재 가능 상태(generated/ingesting)만 허용, 아니면 InvalidJobState.
    - script 행 FOR UPDATE → version_no 경합 방지. 빈 결과(유효 블록 0)는 거부(ValueError).
    반환: {"version_id","blocks","claims","content_hash","reused"}."""
    if not isinstance(script_list, list):
        raise ValueError("script_list must be a list")
    if not script_list:
        raise ValueError("script_list가 비어 있음")
    for idx, block in enumerate(script_list):     # 비정상 원소(None 등)는 조용히 버리지 않고 명시 거부
        if not isinstance(block, dict):
            raise ValueError(f"script_list[{idx}]는 객체여야 함")
    valid = [b for b in script_list if str(b.get("say") or "").strip()]
    if not valid:
        raise ValueError("생성된 유효 대본 블록이 없음")
    ch = _content_hash(script_list)
    with tenant_conn(engine, hospital_id, membership_id=membership_id) as cn:
        job = cn.execute(text("select status, script_id, topic, version_id, content_hash, "
                              "created_by_membership_id from generation_jobs "
                              "where id=:j and hospital_id=:h for update"),
                         {"j": job_id, "h": hospital_id}).first()
        if not job:
            raise InvalidJobState("job 없음")
        if job.version_id and job.content_hash == ch:     # 같은 job·같은 content 재적재 방지
            return {"version_id": job.version_id, "blocks": 0, "claims": 0, "content_hash": ch, "reused": True}
        if job.status not in ("generated", "ingesting"):
            raise InvalidJobState(f"적재 불가 상태: {job.status}")
        # script 식별·topic = job 값만 사용(외부 인자 없음). version_no 경합 방지 위해 script 잠금.
        if job.script_id:
            sc = cn.execute(text("select id from scripts where id=:s and hospital_id=:h for update"),
                            {"s": job.script_id, "h": hospital_id}).scalar()
            if not sc:
                raise ValueError("job.script_id가 이 병원에 없음")
        else:
            sc = uuid.uuid4()
            cn.execute(text("insert into scripts(id,hospital_id,topic) values(:s,:h,:t)"),
                       {"s": sc, "h": hospital_id, "t": job.topic})
        maxno = cn.execute(text("select coalesce(max(version_no),0) from script_versions "
                                "where hospital_id=:h and script_id=:s"), {"h": hospital_id, "s": sc}).scalar()
        v = uuid.uuid4()
        cn.execute(text("insert into script_versions(id,hospital_id,script_id,version_no,source,creation_reason,"
                        "generation_job_id,created_by_membership_id) "
                        "values(:v,:h,:s,:n,'ai','생성 파이프라인',:j,:by)"),
                   {"v": v, "h": hospital_id, "s": sc, "n": maxno + 1,
                    "j": job_id, "by": job.created_by_membership_id})   # 작성자=생성 요청자(job)
        cn.execute(text("insert into version_approval_states(id,hospital_id,version_id,status) values(:i,:h,:v,'none')"),
                   {"i": uuid.uuid4(), "h": hospital_id, "v": v})
        nb = ncl = 0
        for i, b in enumerate(script_list):
            say = str(b.get("say") or "").strip()
            if not say:
                continue
            scene = str(b.get("scene") or "")[:2000]
            block_label = str(b.get("block") or "")
            tags = b.get("tags") or []
            if not isinstance(tags, list):
                raise ValueError(f"script_list[{i}].tags는 배열이어야 함")
            bid = uuid.uuid4()
            cn.execute(text("insert into script_blocks(id,hospital_id,version_id,stable_block_key,order_index,"
                            "block_type,scene,text,metadata) values(:b,:h,:v,:k,:o,:bt,:sc,:tx,cast(:md as jsonb))"),
                       {"b": bid, "h": hospital_id, "v": v, "k": f"blk_{i+1}", "o": i,
                        "bt": block_type_of(block_label), "sc": scene, "tx": say,
                        "md": json.dumps({"block_label": block_label, "tags": tags}, ensure_ascii=False)})
            nb += 1
            ci = 0
            for s0, s1, st in segment(say):
                sid = uuid.uuid4()
                cn.execute(text("insert into script_sentences(id,hospital_id,version_id,block_id,sentence_index,"
                                "text,start_offset,end_offset,offset_unit,segmenter_version) "
                                "values(:s,:h,:v,:b,:i,:tx,:a,:z,'codepoint',:sv)"),
                           {"s": sid, "h": hospital_id, "v": v, "b": bid, "i": ci, "tx": st,
                            "a": s0, "z": s1, "sv": SEGMENTER_VERSION})
                if is_claim(st):     # 휴리스틱(정규식+키워드) 탐지 → detection_method='regex'(실제와 일치)
                    cn.execute(text("insert into claims(id,hospital_id,version_id,sentence_id,claim_index,"
                                    "claim_text,claim_type,detection_method) values(:c,:h,:v,:s,0,:tx,:ct,'regex')"),
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
