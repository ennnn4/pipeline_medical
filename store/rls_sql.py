"""
RLS·역할·SECURITY DEFINER 함수·view의 raw SQL (SQLAlchemy 모델로 표현 불가한 부분).
Alembic revision과 테스트가 apply()로 실행. 모두 idempotent.

핵심(GPT 검토 반영):
- 테넌트 정책은 NULLIF(current_setting('app.hospital_id',true),'')::uuid — 빈문자열 22P02 회피(실 PG 테스트로 발견).
- exchange_review_token: SECURITY DEFINER, 고정 search_path, app_rw엔 review_links 직접 SELECT 미부여·EXECUTE만.
- claim_effective_assessment: security_invoker view, 사람 판정(override>human_review>automated) 우선, migration 제외.
- style_rules: 작업별 정책(SELECT=global+현재병원 / IUD=현재병원, global 금지).
- users: app_rw 직접 SELECT 미부여 → auth 함수로만.
"""
from sqlalchemy import text
from store.schema import TENANT_TABLES

TENANT_SETTING = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

ROLES = [
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_owner') THEN CREATE ROLE app_owner NOLOGIN; END IF; END $$;",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_rw') THEN CREATE ROLE app_rw LOGIN; END IF; END $$;",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_auth') THEN CREATE ROLE app_auth NOLOGIN; END IF; END $$;",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='platform_admin') THEN CREATE ROLE platform_admin NOLOGIN; END IF; END $$;",
]

# review_links는 토큰으로만 조회(부트스트랩) → app_rw에 직접 SELECT 미부여(직접 SELECT는 42501).
#   관리(생성·폐기)는 INSERT/UPDATE/DELETE, 조회는 exchange_review_token()(SECURITY DEFINER)로만.
NO_SELECT_TABLES = {"review_links"}

def tenant_policy(tbl):
    grant = "INSERT, UPDATE, DELETE" if tbl in NO_SELECT_TABLES else "SELECT, INSERT, UPDATE, DELETE"
    out = [
        f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;",
        f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY;",
        f"DROP POLICY IF EXISTS p_tenant ON {tbl};",
        f"CREATE POLICY p_tenant ON {tbl} TO app_rw "
        f"USING (hospital_id = {TENANT_SETTING}) WITH CHECK (hospital_id = {TENANT_SETTING});",
    ]
    if tbl in NO_SELECT_TABLES:
        out.append(f"REVOKE SELECT ON {tbl} FROM app_rw;")   # 명시적 SELECT 박탈(직접조회=42501)
    out.append(f"GRANT {grant} ON {tbl} TO app_rw;")
    return out

# style_rules: 작업별 정책
STYLE_RULES_POLICY = [
    "ALTER TABLE style_rules ENABLE ROW LEVEL SECURITY;",
    "ALTER TABLE style_rules FORCE ROW LEVEL SECURITY;",
    "DROP POLICY IF EXISTS sr_sel ON style_rules;",
    "DROP POLICY IF EXISTS sr_ins ON style_rules;",
    "DROP POLICY IF EXISTS sr_upd ON style_rules;",
    "DROP POLICY IF EXISTS sr_del ON style_rules;",
    f"CREATE POLICY sr_sel ON style_rules FOR SELECT TO app_rw "
    f"USING (scope='global' OR hospital_id = {TENANT_SETTING});",
    f"CREATE POLICY sr_ins ON style_rules FOR INSERT TO app_rw "
    f"WITH CHECK (scope<>'global' AND hospital_id = {TENANT_SETTING});",
    f"CREATE POLICY sr_upd ON style_rules FOR UPDATE TO app_rw "
    f"USING (scope<>'global' AND hospital_id = {TENANT_SETTING}) "
    f"WITH CHECK (scope<>'global' AND hospital_id = {TENANT_SETTING});",
    f"CREATE POLICY sr_del ON style_rules FOR DELETE TO app_rw "
    f"USING (scope<>'global' AND hospital_id = {TENANT_SETTING});",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON style_rules TO app_rw;",
]

# 리뷰 토큰 교환(SECURITY DEFINER) — app_rw엔 review_links 직접 SELECT 미부여
EXCHANGE_FN = """
CREATE OR REPLACE FUNCTION public.exchange_review_token(p_token_digest bytea)
RETURNS TABLE(hospital_id uuid, version_id uuid, link_id uuid, permission text)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public
AS $$
  SELECT rl.hospital_id, rl.version_id, rl.id, rl.permission
  FROM public.review_links rl
  WHERE rl.token_hash = p_token_digest
    AND rl.revoked_at IS NULL
    AND rl.expires_at > now()
$$;
"""
EXCHANGE_GRANTS = [
    "REVOKE ALL ON FUNCTION public.exchange_review_token(bytea) FROM PUBLIC;",
    "GRANT EXECUTE ON FUNCTION public.exchange_review_token(bytea) TO app_rw;",
]

# 최신(정보용)·유효(승인용) assessment view — security_invoker(RLS 우회 방지)
LATEST_VIEW = """
CREATE OR REPLACE VIEW public.claim_latest_assessment
WITH (security_invoker = true) AS
SELECT DISTINCT ON (hospital_id, claim_id) *
FROM public.claim_assessments
ORDER BY hospital_id, claim_id, created_at DESC, id DESC;
"""
# effective: 사람 판정 우선(override>human_review>automated), migration 제외
EFFECTIVE_VIEW = """
CREATE OR REPLACE VIEW public.claim_effective_assessment
WITH (security_invoker = true) AS
SELECT DISTINCT ON (hospital_id, claim_id) *
FROM public.claim_assessments
WHERE assessment_kind IN ('override','human_review','automated')
ORDER BY hospital_id, claim_id,
  CASE assessment_kind WHEN 'override' THEN 3 WHEN 'human_review' THEN 2 WHEN 'automated' THEN 1 ELSE 0 END DESC,
  created_at DESC, id DESC;
"""
VIEW_GRANTS = [
    "GRANT SELECT ON public.claim_latest_assessment TO app_rw;",
    "GRANT SELECT ON public.claim_effective_assessment TO app_rw;",
]

# users 인증 함수(app_auth 전용) — app_rw는 users 직접 SELECT 불가
AUTH_FNS = """
CREATE OR REPLACE FUNCTION public.lookup_user_for_login(p_email text)
RETURNS TABLE(id uuid, pw_hash text) LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog, public
AS $$ SELECT u.id, u.pw_hash FROM public.users u WHERE u.email = btrim(p_email) $$;

CREATE OR REPLACE FUNCTION public.get_current_user(p_user_id uuid)
RETURNS TABLE(id uuid, email text, name text) LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog, public
AS $$ SELECT u.id, u.email, u.name FROM public.users u WHERE u.id = p_user_id $$;
"""
AUTH_GRANTS = [
    "REVOKE ALL ON FUNCTION public.lookup_user_for_login(text) FROM PUBLIC;",
    "REVOKE ALL ON FUNCTION public.get_current_user(uuid) FROM PUBLIC;",
    "GRANT EXECUTE ON FUNCTION public.lookup_user_for_login(text) TO app_auth;",
    "GRANT EXECUTE ON FUNCTION public.get_current_user(uuid) TO app_auth;",
    # app_rw는 users 직접 권한 없음(GRANT 안 함)
]

def statements():
    """실행할 SQL 문장 순서 목록."""
    sts = list(ROLES)
    sts.append("GRANT USAGE ON SCHEMA public TO app_rw;")
    sts.append("GRANT USAGE ON SCHEMA public TO app_auth;")
    for t in TENANT_TABLES:
        sts += tenant_policy(t)
    sts += STYLE_RULES_POLICY
    sts.append(EXCHANGE_FN); sts += EXCHANGE_GRANTS
    sts.append(LATEST_VIEW); sts.append(EFFECTIVE_VIEW); sts += VIEW_GRANTS
    sts.append(AUTH_FNS); sts += AUTH_GRANTS
    # 참조 무결성 검사용 최소 조회 권한(FK가 걸린 부모 테이블 등은 정책으로 통제)
    sts.append("GRANT SELECT ON hospitals TO app_rw;")
    return sts

def apply(engine):
    with engine.begin() as cn:
        for s in statements():
            cn.execute(text(s))
