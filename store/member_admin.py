"""멤버 관리(대행사 전용) — 기존 계정에 병원 역할(editor/approver/admin) 부여·제거.

보안(provision·platform_ops 패턴 준수):
 - membership_roles/hospital_memberships는 app_rw 직접쓰기 금지(rls_sql) → 전부 SECURITY DEFINER 함수로만.
 - 모든 함수가 app.user_id GUC(서버가 세션에서 설정)로 '활성 platform_access_grant(대행사 운영자)'를 검증.
   운영자 아니면 42501. 계정 생성·비밀번호는 다루지 않음(이미 존재하는 계정에 역할만).
 - 부여 가능 역할: editor/approver/admin (platform_operator는 platform grant 전용이라 여기서 제외).
 - 제거는 soft-revoke(revoked_at) — _ROLE_Q가 revoked_at IS NULL만 유효 처리하므로 즉시 무효 + 감사 보존.
"""
from sqlalchemy import text
from store.repositories import tenant_conn

ASSIGNABLE_ROLES = ("editor", "approver", "admin")

# 공통: app.user_id가 활성 운영자인지 검증하는 조각(각 함수 앞부분)
_ASSERT_OP = (
    "v_caller := NULLIF(current_setting('app.user_id', true), '')::uuid; "
    "IF v_caller IS NULL THEN RAISE EXCEPTION 'no user context' USING ERRCODE='42501'; END IF; "
    "SELECT pag.id INTO v_grant FROM public.platform_access_grants pag "   # 컬럼 한정(OUT param user_id 충돌 방지)
    "WHERE pag.user_id=v_caller AND pag.status='active' LIMIT 1; "
    "IF v_grant IS NULL THEN RAISE EXCEPTION 'not a platform operator' USING ERRCODE='42501'; END IF; ")

_FN_FIND = f"""
CREATE OR REPLACE FUNCTION public.fn_pop_find_user(p_email text)
RETURNS TABLE(user_id uuid, email text, name text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE v_caller uuid; v_grant uuid;
BEGIN
  {_ASSERT_OP}
  RETURN QUERY SELECT u.id, u.email, u.name FROM public.users u
    WHERE lower(u.email)=lower(btrim(p_email));
END $$;
"""

_FN_LIST = f"""
CREATE OR REPLACE FUNCTION public.fn_pop_list_members(p_hospital uuid)
RETURNS TABLE(user_id uuid, email text, name text, roles text[])
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE v_caller uuid; v_grant uuid;
BEGIN
  {_ASSERT_OP}
  RETURN QUERY
    SELECT u.id, u.email, u.name,
      COALESCE(array_agg(mr.role ORDER BY mr.role) FILTER (WHERE mr.role IS NOT NULL), '{{}}')
    FROM public.hospital_memberships hm
    JOIN public.users u ON u.id=hm.user_id
    LEFT JOIN public.membership_roles mr
      ON mr.membership_id=hm.id AND mr.hospital_id=hm.hospital_id AND mr.revoked_at IS NULL
    WHERE hm.hospital_id=p_hospital AND hm.archived_at IS NULL
    GROUP BY u.id, u.email, u.name ORDER BY u.email;
END $$;
"""

_FN_SET = f"""
CREATE OR REPLACE FUNCTION public.fn_pop_set_member_role(p_hospital uuid, p_target_user uuid, p_role text, p_action text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE v_caller uuid; v_grant uuid; v_mid uuid; v_active boolean;
BEGIN
  {_ASSERT_OP}
  IF p_role NOT IN ('editor','approver','admin') THEN RAISE EXCEPTION 'invalid role' USING ERRCODE='22023'; END IF;
  IF p_action NOT IN ('add','remove') THEN RAISE EXCEPTION 'invalid action' USING ERRCODE='22023'; END IF;
  IF p_target_user IS NULL THEN RAISE EXCEPTION 'target user required' USING ERRCODE='22023'; END IF;
  SELECT (status='active') INTO v_active FROM public.hospitals WHERE id=p_hospital;
  IF NOT COALESCE(v_active,false) THEN RAISE EXCEPTION 'hospital not active' USING ERRCODE='P0002'; END IF;
  -- 대상 유저의 membership 확보(없으면 생성; 동시성 unique 재조회)
  SELECT id INTO v_mid FROM public.hospital_memberships
    WHERE hospital_id=p_hospital AND user_id=p_target_user AND archived_at IS NULL;
  IF v_mid IS NULL THEN
    v_mid := gen_random_uuid();
    BEGIN
      INSERT INTO public.hospital_memberships(id,hospital_id,user_id) VALUES (v_mid,p_hospital,p_target_user);
    EXCEPTION WHEN unique_violation THEN
      SELECT id INTO v_mid FROM public.hospital_memberships
        WHERE hospital_id=p_hospital AND user_id=p_target_user AND archived_at IS NULL;
    END;
  END IF;
  IF p_action='add' THEN
    INSERT INTO public.membership_roles(id,hospital_id,membership_id,role,grant_source)
      VALUES (gen_random_uuid(),p_hospital,v_mid,p_role,'hospital')
    ON CONFLICT (hospital_id,membership_id,role) DO UPDATE SET revoked_at=NULL, grant_source='hospital';
  ELSE
    UPDATE public.membership_roles SET revoked_at=now()
      WHERE hospital_id=p_hospital AND membership_id=v_mid AND role=p_role AND grant_source='hospital';
  END IF;
  INSERT INTO public.audit_events(id,hospital_id,action,entity_type,entity_id)
    VALUES (gen_random_uuid(),p_hospital,'member.role_'||p_action||':'||p_role,'membership',v_mid);
  RETURN v_mid::text;
END $$;
"""

_GRANTS = [
    "REVOKE ALL ON FUNCTION public.fn_pop_find_user(text) FROM PUBLIC;",
    "GRANT EXECUTE ON FUNCTION public.fn_pop_find_user(text) TO app_rw;",
    "ALTER FUNCTION public.fn_pop_find_user(text) OWNER TO app_owner;",
    "REVOKE ALL ON FUNCTION public.fn_pop_list_members(uuid) FROM PUBLIC;",
    "GRANT EXECUTE ON FUNCTION public.fn_pop_list_members(uuid) TO app_rw;",
    "ALTER FUNCTION public.fn_pop_list_members(uuid) OWNER TO app_owner;",
    "REVOKE ALL ON FUNCTION public.fn_pop_set_member_role(uuid,uuid,text,text) FROM PUBLIC;",
    "GRANT EXECUTE ON FUNCTION public.fn_pop_set_member_role(uuid,uuid,text,text) TO app_rw;",
    "ALTER FUNCTION public.fn_pop_set_member_role(uuid,uuid,text,text) OWNER TO app_owner;",
]

def ensure_member_admin(owner_engine):
    """멤버 관리 SECURITY DEFINER 함수 + 권한(멱등, 비파괴). deploy_bootstrap에서 호출."""
    with owner_engine.begin() as cn:
        cn.execute(text(_FN_FIND))
        cn.execute(text(_FN_LIST))
        cn.execute(text(_FN_SET))
        for s in _GRANTS:
            cn.execute(text(s))

# ── Python 헬퍼(app_rw 엔진 + app.user_id GUC를 tenant_conn으로 설정) ──
def find_user(engine, hospital_id, caller_user_id, email):
    with tenant_conn(engine, hospital_id, user_id=caller_user_id) as cn:
        r = cn.execute(text("select user_id, email, name from public.fn_pop_find_user(:e)"),
                       {"e": email}).first()
    return dict(user_id=r[0], email=r[1], name=r[2]) if r else None

def list_members(engine, hospital_id, caller_user_id):
    with tenant_conn(engine, hospital_id, user_id=caller_user_id) as cn:
        rows = cn.execute(text("select user_id, email, name, roles from public.fn_pop_list_members(:h)"),
                          {"h": str(hospital_id)}).all()
    return [dict(user_id=r[0], email=r[1], name=r[2], roles=list(r[3] or [])) for r in rows]

def set_member_role(engine, hospital_id, caller_user_id, target_user_id, role, action):
    with tenant_conn(engine, hospital_id, user_id=caller_user_id) as cn:
        return cn.execute(text("select public.fn_pop_set_member_role(:h,:u,:r,:a)"),
                          {"h": str(hospital_id), "u": str(target_user_id), "r": role, "a": action}).scalar()
