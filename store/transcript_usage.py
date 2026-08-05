"""Supadata 크레딧 사용량(전역 quota) + 관리자 문의 — 저장/집계(스펙 5·6·9·10절).

핵심:
 - 무료 계정은 서비스 전체 공유 → quota는 '전역'(병원별 100 아님). 월 합계는 SECURITY DEFINER 함수로
   RLS 우회 없이 전역 SUM(app_rw는 EXECUTE만). 기록(INSERT)은 tenant_conn(자기 병원, RLS 준수).
 - 이중집계 방지: unique(provider, request_id, operation) → ON CONFLICT DO NOTHING(polling·retry 안전).
 - 크레딧은 '실제 소비'(x-billable-requests) 우선, 없으면 보수적 추정(estimated=true).
"""
import json
from datetime import datetime, timezone
from sqlalchemy import text
from store.repositories import tenant_conn

_TENANT_SET = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"


def billing_month(dt=None):
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


_DDL_USAGE = """
CREATE TABLE IF NOT EXISTS transcript_provider_usage (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider text NOT NULL,
  billing_month text NOT NULL,             -- 'YYYY-MM'(UTC)
  hospital_id uuid REFERENCES hospitals(id),
  project_id uuid,
  benchmark_video_id uuid,
  request_id text NOT NULL,
  provider_job_id text,
  operation text NOT NULL,                 -- transcript_fetch | transcript_poll | ai_generate
  mode text,                               -- native | auto
  status text,
  credits_used int NOT NULL DEFAULT 0,
  credits_estimated boolean NOT NULL DEFAULT false,
  response_status int,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_tpu_idem UNIQUE (provider, request_id, operation)
);
"""

_DDL_ADMIN = """
CREATE TABLE IF NOT EXISTS admin_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hospital_id uuid NOT NULL REFERENCES hospitals(id),
  requester_membership_id uuid,
  request_type text NOT NULL,              -- transcript_quota_upgrade
  provider text,
  billing_month text,
  credits_used int,
  status text NOT NULL DEFAULT 'open',     -- open | resolved
  created_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz,
  CONSTRAINT ck_ar_status CHECK (status IN ('open','resolved'))
);
"""

# 전역 월 크레딧 합계(SECURITY DEFINER, owner 소유 → 전역 SUM. app_rw는 EXECUTE만).
_FN_CREDITS = """
CREATE OR REPLACE FUNCTION public.fn_transcript_credits_month(p_month text)
RETURNS integer LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  SELECT COALESCE(SUM(credits_used), 0)::int
  FROM transcript_provider_usage WHERE billing_month = p_month;
$$;
"""

# 관리자(platform_operator) 전용 전역 집계/문의 — app.user_id GUC로 운영자 확인(아니면 42501).
_OP_ASSERT = ("IF NOT EXISTS (SELECT 1 FROM platform_access_grants pag "
              "WHERE pag.user_id = NULLIF(current_setting('app.user_id', true),'')::uuid "
              "AND pag.status='active') "
              "THEN RAISE EXCEPTION 'not a platform operator' USING ERRCODE='42501'; END IF;")

_FN_SUMMARY = f"""
CREATE OR REPLACE FUNCTION public.fn_transcript_usage_summary(p_month text)
RETURNS TABLE(total_requests bigint, credits bigint, success bigint, failed bigint, manual bigint, quota bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  {_OP_ASSERT}
  RETURN QUERY SELECT count(*)::bigint,
    COALESCE(sum(credits_used),0)::bigint,
    count(*) FILTER (WHERE status='available')::bigint,
    count(*) FILTER (WHERE status IN ('provider_failed','config_error','rate_limited'))::bigint,
    count(*) FILTER (WHERE status='manual_required')::bigint,
    count(*) FILTER (WHERE status='quota_exhausted')::bigint
  FROM transcript_provider_usage WHERE billing_month = p_month;
END $$;
"""

_FN_LIST_REQ = f"""
CREATE OR REPLACE FUNCTION public.fn_admin_requests(p_status text)
RETURNS TABLE(id uuid, hospital_id uuid, hospital_name text, request_type text, billing_month text,
              credits_used int, status text, created_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  {_OP_ASSERT}
  RETURN QUERY SELECT a.id, a.hospital_id, h.name, a.request_type, a.billing_month,
    a.credits_used, a.status, a.created_at
  FROM admin_requests a JOIN hospitals h ON h.id = a.hospital_id
  WHERE (p_status IS NULL OR a.status = p_status) ORDER BY a.created_at DESC LIMIT 200;
END $$;
"""

_FN_RESOLVE = f"""
CREATE OR REPLACE FUNCTION public.fn_resolve_admin_request(p_id uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  {_OP_ASSERT}
  UPDATE admin_requests SET status='resolved', resolved_at=now() WHERE id = p_id AND status='open';
END $$;
"""


def _policies(tbl, prefix):
    return [
        f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;",
        f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY;",
        f"DROP POLICY IF EXISTS {prefix}_rw ON {tbl};",
        f"CREATE POLICY {prefix}_rw ON {tbl} TO app_rw "
        f"USING (hospital_id = {_TENANT_SET}) WITH CHECK (hospital_id = {_TENANT_SET});",
        f"DROP POLICY IF EXISTS {prefix}_def ON {tbl};",
        f"CREATE POLICY {prefix}_def ON {tbl} TO app_owner USING (true) WITH CHECK (true);",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO app_rw;",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO app_owner;",
    ]


def ensure_transcript_usage(owner_engine):
    with owner_engine.begin() as cn:
        cn.execute(text(_DDL_USAGE))
        cn.execute(text(_DDL_ADMIN))
        for s in _policies("transcript_provider_usage", "tpu"):
            cn.execute(text(s))
        for s in _policies("admin_requests", "ar"):
            cn.execute(text(s))
        cn.execute(text("CREATE INDEX IF NOT EXISTS ix_tpu_month ON transcript_provider_usage(billing_month);"))
        cn.execute(text("CREATE INDEX IF NOT EXISTS ix_ar_open ON admin_requests(hospital_id, status);"))
        for fn in (_FN_CREDITS, _FN_SUMMARY, _FN_LIST_REQ, _FN_RESOLVE):
            cn.execute(text(fn))
        for sig in ("fn_transcript_credits_month(text)", "fn_transcript_usage_summary(text)",
                    "fn_admin_requests(text)", "fn_resolve_admin_request(uuid)"):
            try:
                cn.execute(text(f"ALTER FUNCTION public.{sig} OWNER TO app_owner;"))
            except Exception:
                pass   # 로컬 시뮬 등 app_owner 없으면 무시
            cn.execute(text(f"GRANT EXECUTE ON FUNCTION public.{sig} TO app_rw;"))


# ── 사용량 기록/집계 ──
def record_usage(engine, hospital_id, *, request_id, operation, provider="supadata",
                 mode=None, status=None, credits_used=0, credits_estimated=False,
                 response_status=None, project_id=None, benchmark_video_id=None,
                 provider_job_id=None, month=None):
    """사용량 1건 기록(멱등). 같은 (provider,request_id,operation)은 이중집계 안 함."""
    with tenant_conn(engine, hospital_id) as cn:
        cn.execute(text(
            "insert into transcript_provider_usage(provider,billing_month,hospital_id,project_id,"
            "benchmark_video_id,request_id,provider_job_id,operation,mode,status,credits_used,"
            "credits_estimated,response_status) values(:p,:bm,:h,:pj,:bv,:rq,:jid,:op,:md,:st,:cu,:ce,:rs) "
            "on conflict (provider, request_id, operation) do nothing"),
            {"p": provider, "bm": month or billing_month(), "h": hospital_id, "pj": project_id,
             "bv": benchmark_video_id, "rq": request_id, "jid": provider_job_id, "op": operation,
             "md": mode, "st": status, "cu": int(credits_used or 0), "ce": bool(credits_estimated),
             "rs": response_status})


def credits_used_this_month(engine, month=None):
    """전역 월 크레딧 합계(SECURITY DEFINER 함수 — app_rw도 전역 조회 가능)."""
    with engine.connect() as cn:
        return int(cn.execute(text("select public.fn_transcript_credits_month(:m)"),
                              {"m": month or billing_month()}).scalar() or 0)


def quota_status(engine, limit=None, warn_threshold=None, month=None):
    """{used, limit, remaining, pct, warning, exhausted}. limit/warn은 config에서 주입."""
    from services.supadata import SupadataConfig
    limit = SupadataConfig.monthly_credit_limit() if limit is None else limit
    warn_threshold = SupadataConfig.warning_threshold() if warn_threshold is None else warn_threshold
    used = credits_used_this_month(engine, month)
    remaining = max(0, limit - used)
    pct = round(used / limit * 100, 1) if limit else 100.0
    return {"used": used, "limit": limit, "remaining": remaining, "pct": pct,
            "warning": used >= warn_threshold, "exhausted": used >= limit}


# ── 관리자 문의(중복 방지) ──
def create_admin_request(engine, hospital_id, *, requester_membership_id=None,
                         request_type="transcript_quota_upgrade", provider="supadata",
                         credits_used=None, month=None):
    """열린 동일 문의가 있으면 그걸 반환(중복 클릭 방지), 없으면 생성. 반환: {id, created:bool}."""
    month = month or billing_month()
    with tenant_conn(engine, hospital_id) as cn:
        existing = cn.execute(text(
            "select id from admin_requests where hospital_id=:h and request_type=:t "
            "and billing_month=:m and status='open' limit 1"),
            {"h": hospital_id, "t": request_type, "m": month}).scalar()
        if existing:
            return {"id": str(existing), "created": False}
        rid = cn.execute(text(
            "insert into admin_requests(hospital_id,requester_membership_id,request_type,provider,"
            "billing_month,credits_used) values(:h,:m,:t,:p,:bm,:cu) returning id"),
            {"h": hospital_id, "m": requester_membership_id, "t": request_type, "p": provider,
             "bm": month, "cu": credits_used}).scalar()
    return {"id": str(rid), "created": True}


# ── 관리자(platform_operator) 전역 조회/처리 — app.user_id GUC로 운영자 확인 ──
def _op(engine, user_id):
    """app.user_id GUC만 설정한 트랜잭션(definer 함수가 운영자 확인)."""
    cn = engine.connect()
    tx = cn.begin()
    cn.execute(text("select set_config('app.user_id', :u, true)"), {"u": str(user_id)})
    return cn, tx

def usage_summary_global(engine, user_id, month=None):
    cn, tx = _op(engine, user_id)
    try:
        r = cn.execute(text("select * from public.fn_transcript_usage_summary(:m)"),
                       {"m": month or billing_month()}).first()
        return {"total": r.total_requests, "credits": r.credits, "success": r.success,
                "failed": r.failed, "manual": r.manual, "quota": r.quota} if r else {}
    finally:
        tx.commit(); cn.close()

def list_admin_requests_global(engine, user_id, status="open"):
    cn, tx = _op(engine, user_id)
    try:
        rows = cn.execute(text("select * from public.fn_admin_requests(:s)"), {"s": status}).all()
        return [{"id": str(r.id), "hospital_id": str(r.hospital_id), "hospital_name": r.hospital_name,
                 "billing_month": r.billing_month, "credits_used": r.credits_used, "status": r.status,
                 "created_at": r.created_at.isoformat()} for r in rows]
    finally:
        tx.commit(); cn.close()

def resolve_admin_request(engine, user_id, request_id):
    cn, tx = _op(engine, user_id)
    try:
        cn.execute(text("select public.fn_resolve_admin_request(:i)"), {"i": request_id})
    finally:
        tx.commit(); cn.close()
