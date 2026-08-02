"""병원 프로비저닝 — SECURITY DEFINER 함수로 hospital+creator membership+role+audit를 한 트랜잭션에 생성.

GPT P1 반영:
 - app_rw에 hospitals INSERT 직접 권한을 주지 않고, 제한된 SECURITY DEFINER 함수(app_owner 소유)로만.
 - slug 충돌 시 기존 hospital_id 반환(idempotent). 호출자가 임의 owner를 지정 못하게 인자 제한.
 - REVOKE PUBLIC EXECUTE, app_rw만 EXECUTE. 함수 owner=로그인 불가 app_owner, search_path 고정.

비파괴 additive(ensure_provision). 앱은 provision_hospital(app_rw로 함수 호출)만 사용.
"""
from sqlalchemy import text

_FN = """
CREATE OR REPLACE FUNCTION public.fn_provision_hospital(p_slug text, p_name text, p_owner_user uuid)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_hid uuid; v_mid uuid;
BEGIN
  IF p_slug IS NULL OR btrim(p_slug) = '' THEN
    RAISE EXCEPTION 'slug required' USING ERRCODE = '22023';
  END IF;
  SELECT id INTO v_hid FROM public.hospitals WHERE slug = p_slug;
  IF v_hid IS NOT NULL THEN
    RETURN v_hid;                     -- slug 충돌: 기존 병원 반환(멱등)
  END IF;
  v_hid := gen_random_uuid();
  INSERT INTO public.hospitals(id, slug, name) VALUES (v_hid, p_slug, COALESCE(NULLIF(btrim(p_name), ''), p_slug));
  IF p_owner_user IS NOT NULL THEN    -- creator를 admin membership으로(요청자만; 임의 owner 지정 불가)
    v_mid := gen_random_uuid();
    INSERT INTO public.hospital_memberships(id, hospital_id, user_id) VALUES (v_mid, v_hid, p_owner_user);
    INSERT INTO public.membership_roles(id, hospital_id, membership_id, role)
      VALUES (gen_random_uuid(), v_hid, v_mid, 'admin');
  END IF;
  INSERT INTO public.audit_events(id, hospital_id, action, entity_type, entity_id)
    VALUES (gen_random_uuid(), v_hid, 'hospital.provision', 'hospital', v_hid);
  RETURN v_hid;
END $$;
"""

_GRANTS = [
    "GRANT SELECT, INSERT ON hospitals TO app_owner;",   # definer 함수(app_owner 소유)가 slug 조회+생성
    "REVOKE ALL ON FUNCTION public.fn_provision_hospital(text,text,uuid) FROM PUBLIC;",
    "GRANT EXECUTE ON FUNCTION public.fn_provision_hospital(text,text,uuid) TO app_rw;",
    "ALTER FUNCTION public.fn_provision_hospital(text,text,uuid) OWNER TO app_owner;",
]

def ensure_provision(owner_engine):
    with owner_engine.begin() as cn:
        cn.execute(text(_FN))
        for s in _GRANTS:
            cn.execute(text(s))

def provision_hospital(engine, slug, name, owner_user=None):
    """app_rw로 SECURITY DEFINER 함수 호출 → hospital_id 반환(기존이면 그대로)."""
    with engine.connect() as cn:
        with cn.begin():
            return cn.execute(text("select public.fn_provision_hospital(:s,:n,:u)"),
                              {"s": slug, "n": name, "u": owner_user}).scalar()
