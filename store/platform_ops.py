"""플랫폼 운영자(대행사) 접근 — 전 병원 접근권을 '멤버십 자동 프로비저닝'으로 부여(GPT 승인 설계).

핵심(GPT 최종판정):
 - RLS 약화 금지. platform operator도 대상 병원의 '실제 membership'을 획득해, 이후 모든 service/DB가
   일반 membership과 동일하게 동작(app.membership_id·created_by·approver·audit·역할검사 불변식 보존).
 - 자동 부여 역할은 admin이 아니라 별도 platform_operator — 편집·조회·근거 accepted/rejected·이미지·
   export까지만. 최종승인·자기승인·철회·waived/not_applicable·사용자/병원설정 관리는 불가(병원 approver 몫).
 - 권한 저장은 users 플래그가 아니라 platform_access_grants(부여자·상태·철회이력·scope 감사).
 - 멤버십 자동 생성은 app_rw 직접 INSERT가 아니라 SECURITY DEFINER 함수(app_owner 소유). 동시성 안전.
 - platform 부여 role은 grant_source='platform'+platform_grant_id로 표시. 유효성은 '연결된 grant가 active'
   인지로 판정 → grant 철회 시 그 role만 즉시 무효(병원이 직접 준 역할은 유지).
 - DB role(app_rw 등)과 애플리케이션 platform 권한은 분리(재사용 금지).
"""
from sqlalchemy import text

_DDL = [
    # membership_roles: 부여 출처 구분 + 철회시각 + platform grant 연결(additive)
    "ALTER TABLE membership_roles ADD COLUMN IF NOT EXISTS grant_source text NOT NULL DEFAULT 'hospital';",
    "ALTER TABLE membership_roles ADD COLUMN IF NOT EXISTS platform_grant_id uuid;",
    "ALTER TABLE membership_roles ADD COLUMN IF NOT EXISTS revoked_at timestamptz;",
    # role CHECK에 platform_operator 추가(기존 제약 교체, 멱등). 네이밍 컨벤션 적용 이름·원시 이름 모두 드롭.
    "DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='ck_membership_roles_role2') THEN "
    "ALTER TABLE membership_roles DROP CONSTRAINT IF EXISTS role; "
    "ALTER TABLE membership_roles DROP CONSTRAINT IF EXISTS ck_membership_roles_role; "
    "ALTER TABLE membership_roles ADD CONSTRAINT ck_membership_roles_role2 "
    "CHECK (role IN ('editor','reviewer','approver','admin','platform_operator')); END IF; END $$;",
    "DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='ck_membership_roles_grant_source') THEN "
    "ALTER TABLE membership_roles ADD CONSTRAINT ck_membership_roles_grant_source "
    "CHECK (grant_source IN ('hospital','platform')); END IF; END $$;",
    # platform_access_grants(전역 테이블) — 부여 이력·상태·scope
    "CREATE TABLE IF NOT EXISTS platform_access_grants ("
    " id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
    " user_id uuid NOT NULL REFERENCES users(id),"
    " platform_role text NOT NULL DEFAULT 'operator',"
    " scope text NOT NULL DEFAULT 'all_active_hospitals',"
    " status text NOT NULL DEFAULT 'active',"
    " granted_at timestamptz NOT NULL DEFAULT now(),"
    " granted_by_user_id uuid REFERENCES users(id),"
    " revoked_at timestamptz, revoke_reason text,"
    " CONSTRAINT ck_pag_role CHECK (platform_role IN ('operator')),"
    " CONSTRAINT ck_pag_scope CHECK (scope IN ('all_active_hospitals')),"
    " CONSTRAINT ck_pag_status CHECK (status IN ('active','revoked')));",
    # 유저당 활성 grant 1개(동시성 안전한 재부여·재조회 기준)
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_pag_active ON platform_access_grants(user_id) WHERE status='active';",
    # membership_roles.platform_grant_id → grants (테이블 존재 후)
    "DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='fk_membership_roles_grant') THEN "
    "ALTER TABLE membership_roles ADD CONSTRAINT fk_membership_roles_grant "
    "FOREIGN KEY (platform_grant_id) REFERENCES platform_access_grants(id) ON DELETE RESTRICT NOT VALID; END IF; END $$;",
]

# ── 멤버십 자동 프로비저닝(전용 SECURITY DEFINER, app_owner). app.user_id GUC에서 주체 결정 ──
_FN_ENSURE = """
CREATE OR REPLACE FUNCTION public.fn_ensure_platform_operator_membership(p_hospital uuid)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE v_user uuid; v_grant uuid; v_mid uuid; v_active boolean;
BEGIN
  v_user := NULLIF(current_setting('app.user_id', true), '')::uuid;    -- 서버가 세션에서 설정(SDR)
  IF v_user IS NULL THEN RAISE EXCEPTION 'no user context' USING ERRCODE = '42501'; END IF;
  SELECT id INTO v_grant FROM public.platform_access_grants
    WHERE user_id = v_user AND status = 'active' LIMIT 1;             -- 활성 platform grant 필수
  IF v_grant IS NULL THEN RAISE EXCEPTION 'no active platform grant' USING ERRCODE = '42501'; END IF;
  SELECT (status = 'active') INTO v_active FROM public.hospitals WHERE id = p_hospital;
  IF NOT COALESCE(v_active, false) THEN RAISE EXCEPTION 'hospital not active' USING ERRCODE = 'P0002'; END IF;
  -- 기존 membership 재사용(병원이 직접 준 역할 보존). 없으면 생성(동시성: unique(hospital,user) 재조회).
  SELECT id INTO v_mid FROM public.hospital_memberships
    WHERE hospital_id = p_hospital AND user_id = v_user AND archived_at IS NULL;
  IF v_mid IS NULL THEN
    v_mid := gen_random_uuid();
    BEGIN
      INSERT INTO public.hospital_memberships(id, hospital_id, user_id) VALUES (v_mid, p_hospital, v_user);
    EXCEPTION WHEN unique_violation THEN
      SELECT id INTO v_mid FROM public.hospital_memberships
        WHERE hospital_id = p_hospital AND user_id = v_user AND archived_at IS NULL;
    END;
  END IF;
  -- platform_operator role만 추가/갱신(기존 role 덮어쓰지 않음). 활성 grant에 연결.
  INSERT INTO public.membership_roles(id, hospital_id, membership_id, role, grant_source, platform_grant_id)
    VALUES (gen_random_uuid(), p_hospital, v_mid, 'platform_operator', 'platform', v_grant)
  ON CONFLICT (hospital_id, membership_id, role)
    DO UPDATE SET grant_source = 'platform', platform_grant_id = v_grant, revoked_at = NULL;
  INSERT INTO public.audit_events(id, hospital_id, action, entity_type, entity_id)
    VALUES (gen_random_uuid(), p_hospital, 'platform.operator_access', 'membership', v_mid);
  RETURN v_mid;
END $$;
"""

# ── grant/revoke(고권한, owner 실행 전용 — app_rw EXECUTE 없음) ──
_FN_GRANT = """
CREATE OR REPLACE FUNCTION public.fn_grant_platform_operator(p_user uuid, p_granted_by uuid)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE v_id uuid;
BEGIN
  IF p_user IS NULL THEN RAISE EXCEPTION 'user required' USING ERRCODE = '22023'; END IF;
  SELECT id INTO v_id FROM public.platform_access_grants WHERE user_id = p_user AND status = 'active' LIMIT 1;
  IF v_id IS NOT NULL THEN RETURN v_id; END IF;                       -- 멱등(이미 활성)
  v_id := gen_random_uuid();
  INSERT INTO public.platform_access_grants(id, user_id, platform_role, scope, status, granted_by_user_id)
    VALUES (v_id, p_user, 'operator', 'all_active_hospitals', 'active', p_granted_by);
  RETURN v_id;
END $$;
"""

_FN_REVOKE = """
CREATE OR REPLACE FUNCTION public.fn_revoke_platform_operator(p_user uuid, p_reason text)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE v_n integer;
BEGIN
  UPDATE public.platform_access_grants SET status = 'revoked', revoked_at = now(),
    revoke_reason = NULLIF(btrim(coalesce(p_reason, '')), '')
    WHERE user_id = p_user AND status = 'active';
  GET DIAGNOSTICS v_n = ROW_COUNT;
  -- 자동 부여된 platform role을 즉시 무효화(revoked_at). 병원이 직접 준 역할(grant_source='hospital')은 유지.
  UPDATE public.membership_roles SET revoked_at = now()
    WHERE grant_source = 'platform' AND revoked_at IS NULL
      AND platform_grant_id IN (SELECT id FROM public.platform_access_grants
                                WHERE user_id = p_user AND status = 'revoked');
  RETURN v_n;
END $$;
"""

# ── 병원 목록(platform scope) — 크로스테넌트지만 최소 필드만. app.user_id의 활성 grant 필요 ──
_FN_LIST = """
CREATE OR REPLACE FUNCTION public.fn_list_platform_hospitals()
RETURNS TABLE(hospital_id uuid, slug text, name text, status text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE v_user uuid; v_grant uuid;
BEGIN
  v_user := NULLIF(current_setting('app.user_id', true), '')::uuid;
  IF v_user IS NULL THEN RAISE EXCEPTION 'no user context' USING ERRCODE = '42501'; END IF;
  SELECT id INTO v_grant FROM public.platform_access_grants
    WHERE user_id = v_user AND status = 'active' LIMIT 1;
  IF v_grant IS NULL THEN RAISE EXCEPTION 'no active platform grant' USING ERRCODE = '42501'; END IF;
  RETURN QUERY SELECT h.id, h.slug, h.name, h.status FROM public.hospitals h
    WHERE h.status = 'active' ORDER BY h.name;   -- 대본·자료·사용자는 반환하지 않음(최소 노출)
END $$;
"""

_GRANTS = [
    # ensure·list는 app_rw가 실행(resolve·병원선택). grant/revoke는 owner 전용(app_rw EXECUTE 없음).
    "REVOKE ALL ON FUNCTION public.fn_ensure_platform_operator_membership(uuid) FROM PUBLIC;",
    "GRANT EXECUTE ON FUNCTION public.fn_ensure_platform_operator_membership(uuid) TO app_rw;",
    "ALTER FUNCTION public.fn_ensure_platform_operator_membership(uuid) OWNER TO app_owner;",
    "REVOKE ALL ON FUNCTION public.fn_list_platform_hospitals() FROM PUBLIC;",
    "GRANT EXECUTE ON FUNCTION public.fn_list_platform_hospitals() TO app_rw;",
    "ALTER FUNCTION public.fn_list_platform_hospitals() OWNER TO app_owner;",
    "REVOKE ALL ON FUNCTION public.fn_grant_platform_operator(uuid,uuid) FROM PUBLIC;",
    "ALTER FUNCTION public.fn_grant_platform_operator(uuid,uuid) OWNER TO app_owner;",
    "REVOKE ALL ON FUNCTION public.fn_revoke_platform_operator(uuid,text) FROM PUBLIC;",
    "ALTER FUNCTION public.fn_revoke_platform_operator(uuid,text) OWNER TO app_owner;",
    # platform_access_grants: app_rw는 읽기만(resolve의 role 유효성 판정용). 쓰기는 definer만.
    "GRANT SELECT ON platform_access_grants TO app_rw;",
    "GRANT SELECT, INSERT, UPDATE ON platform_access_grants TO app_owner;",
    "GRANT SELECT ON platform_access_grants TO app_owner;",
]


def ensure_platform_ops(owner_engine):
    """비파괴 additive — 스키마 컬럼/제약/테이블 + definer 함수 + 권한. reseed 안전(deploy_bootstrap에서 호출)."""
    with owner_engine.begin() as cn:
        for s in _DDL:
            cn.execute(text(s))
        cn.execute(text(_FN_ENSURE))
        cn.execute(text(_FN_GRANT))
        cn.execute(text(_FN_REVOKE))
        cn.execute(text(_FN_LIST))
        for s in _GRANTS:
            cn.execute(text(s))


# ── Python 헬퍼(owner 엔진으로 부여/철회; app은 resolve가 ensure만 호출) ──
def grant_platform_operator(owner_engine, user_id, granted_by=None):
    with owner_engine.begin() as cn:
        return cn.execute(text("select public.fn_grant_platform_operator(:u,:g)"),
                          {"u": str(user_id), "g": str(granted_by) if granted_by else None}).scalar()


def revoke_platform_operator(owner_engine, user_id, reason=""):
    with owner_engine.begin() as cn:
        return cn.execute(text("select public.fn_revoke_platform_operator(:u,:r)"),
                          {"u": str(user_id), "r": reason}).scalar()


def ensure_platform_admin_user(owner_engine, email, password, name=None):
    """platform operator 계정(admin@ourmarketing.com 등) 생성/비번갱신 + active grant 부여(owner 실행).

    이메일 기반 PG 로그인(_pg_login)으로 대시보드+스튜디오 단일 로그인. 반환 user_id.
    개별 운영자별 계정 권장(GPT) — 공유 계정은 감사 추적이 약함. reseed 후 재실행 필요(데이터)."""
    from werkzeug.security import generate_password_hash
    pwh = generate_password_hash(password)
    with owner_engine.begin() as cn:
        uid = cn.execute(text("select id from users where email=:e"), {"e": email}).scalar()
        if uid:
            cn.execute(text("update users set pw_hash=:p, name=coalesce(:n, name) where id=:i"),
                       {"p": pwh, "n": name, "i": uid})
        else:
            uid = cn.execute(text("insert into users(id,email,name,pw_hash) "
                                  "values(gen_random_uuid(),:e,:n,:p) returning id"),
                             {"e": email, "n": name or email, "p": pwh}).scalar()
        cn.execute(text("select public.fn_grant_platform_operator(:u, NULL)"), {"u": str(uid)})
    return uid
