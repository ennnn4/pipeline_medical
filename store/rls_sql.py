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

# 불변 테이블: app_rw는 INSERT/SELECT만(UPDATE/DELETE 봉쇄) — '주석뿐 불변'을 DB 권한으로 강제
IMMUTABLE_TABLES = ["script_versions", "script_blocks", "script_sentences", "claims",
                    "claim_assessments", "source_versions"]

LOCKDOWN = (
    [f"REVOKE UPDATE, DELETE ON {t} FROM app_rw;" for t in IMMUTABLE_TABLES]
    # 승인 상태: 직접 UPDATE/DELETE 봉쇄. INSERT는 status='none'만(승인 행 위조 방지) → 승인은 fn_approve만.
    + ["REVOKE UPDATE, DELETE ON version_approval_states FROM app_rw;",
       "DROP POLICY IF EXISTS p_tenant ON version_approval_states;",
       f"CREATE POLICY vas_sel ON version_approval_states FOR SELECT TO app_rw USING (hospital_id = {TENANT_SETTING});",
       f"CREATE POLICY vas_ins ON version_approval_states FOR INSERT TO app_rw "
       f"WITH CHECK (hospital_id = {TENANT_SETTING} AND status = 'none');"]
)

# 승인: 역할검사 + 미검증/미지원 claim 게이트 + 상태UPDATE + audit(동일 함수=원자). SECURITY DEFINER.
FN_APPROVE = """
CREATE OR REPLACE FUNCTION public.fn_approve_version(
  p_hospital uuid, p_version uuid, p_policy text, p_content_hash text, p_assessment_hash text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_approver uuid; v_hospital uuid;
BEGIN
  -- 승인자는 파라미터 신뢰 금지 → 세션 컨텍스트(앱이 인증된 membership으로 설정)에 결합
  v_approver := NULLIF(current_setting('app.membership_id', true), '')::uuid;
  v_hospital := NULLIF(current_setting('app.hospital_id', true), '')::uuid;
  IF v_approver IS NULL OR v_hospital IS NULL OR v_hospital <> p_hospital THEN
    RAISE EXCEPTION 'session identity required / hospital mismatch' USING ERRCODE='42501';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.membership_roles
                 WHERE hospital_id=p_hospital AND membership_id=v_approver AND role IN ('approver','admin')) THEN
    RAISE EXCEPTION 'approver role required' USING ERRCODE='42501';
  END IF;
  IF EXISTS (SELECT 1 FROM public.claims c
             WHERE c.hospital_id=p_hospital AND c.version_id=p_version
               AND NOT EXISTS (SELECT 1 FROM public.claim_effective_assessment e
                               WHERE e.hospital_id=c.hospital_id AND e.claim_id=c.id
                                 AND e.verification_status='verified'
                                 AND e.support_level NOT IN ('unverified','unsupported'))) THEN
    RAISE EXCEPTION 'unverified or unsupported claim blocks approval' USING ERRCODE='23514';
  END IF;
  UPDATE public.version_approval_states SET status='approved', approver_membership_id=v_approver,
    assessment_set_hash=p_assessment_hash, version_content_hash=p_content_hash,
    compliance_policy_version=p_policy, decided_at=now(), updated_at=now()
  WHERE hospital_id=p_hospital AND version_id=p_version;
  IF NOT FOUND THEN RAISE EXCEPTION 'approval state row not found' USING ERRCODE='P0002'; END IF;
  INSERT INTO public.audit_events(id,hospital_id,actor_membership_id,action,entity_type,entity_id,after_hash)
    VALUES(gen_random_uuid(), p_hospital, v_approver, 'approval.approve', 'version', p_version, p_assessment_hash);
END $$;
"""
FN_REVOKE_LINK = """
CREATE OR REPLACE FUNCTION public.fn_revoke_review_link(p_hospital uuid, p_link_id uuid)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n integer;
BEGIN
  UPDATE public.review_links SET revoked_at=now() WHERE hospital_id=p_hospital AND id=p_link_id AND revoked_at IS NULL;
  GET DIAGNOSTICS n = ROW_COUNT;
  UPDATE public.review_sessions SET revoked_at=now()
    WHERE hospital_id=p_hospital AND review_link_id=p_link_id AND revoked_at IS NULL;   -- 링크 폐기 시 세션도 폐기
  RETURN n;
END $$;
"""
FN_GRANTS = [
    "DROP FUNCTION IF EXISTS public.fn_approve_version(uuid,uuid,uuid,text,text,text);",  # 구 6인자(p_approver 신뢰) 제거
    "REVOKE ALL ON FUNCTION public.fn_approve_version(uuid,uuid,text,text,text) FROM PUBLIC;",
    "GRANT EXECUTE ON FUNCTION public.fn_approve_version(uuid,uuid,text,text,text) TO app_rw;",
    "REVOKE ALL ON FUNCTION public.fn_revoke_review_link(uuid,uuid) FROM PUBLIC;",
    "GRANT EXECUTE ON FUNCTION public.fn_revoke_review_link(uuid,uuid) TO app_rw;",
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
    # 불변 테이블/승인 상태 DML 봉쇄 + 승인·폐기 전용 함수(blanket GRANT 이후에 REVOKE)
    sts += LOCKDOWN
    sts.append(FN_APPROVE); sts.append(FN_REVOKE_LINK); sts += FN_GRANTS
    return sts

def apply(engine):
    with engine.begin() as cn:
        for s in statements():
            cn.execute(text(s))
