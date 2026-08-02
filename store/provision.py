"""병원 프로비저닝 — SECURITY DEFINER 함수로 hospital+creator membership+role+audit를 한 트랜잭션에 생성.

GPT P1/P2 반영:
 - app_rw에 hospitals INSERT 직접 권한을 주지 않고, 제한된 SECURITY DEFINER 함수(app_owner 소유)로만.
 - slug 충돌 정책(P2, 5.1): 호출자가 그 병원의 활성 멤버면 기존 반환(멱등 재시도),
   관계없는 사용자면 Conflict(23505 유사) — 기존 병원 UUID/멤버십을 절대 넘겨주지 않음.
 - REVOKE PUBLIC EXECUTE, app_rw만 EXECUTE. 함수 owner=로그인 불가 app_owner, search_path 고정, 객체 schema-qualified.

비파괴 additive(ensure_provision). 앱은 provision_hospital(app_rw로 함수 호출)만 사용.
"""
from sqlalchemy import text

class ProvisionConflict(Exception):
    """이미 존재하는 slug인데 호출자가 그 병원의 멤버가 아님 — 기존 병원 노출 금지."""

_FN = """
CREATE OR REPLACE FUNCTION public.fn_provision_hospital(p_slug text, p_name text, p_owner_user uuid)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_hid uuid; v_mid uuid; v_exists boolean;
BEGIN
  IF p_slug IS NULL OR btrim(p_slug) = '' THEN
    RAISE EXCEPTION 'slug required' USING ERRCODE = '22023';
  END IF;
  IF length(btrim(p_slug)) > 64 OR p_slug !~ '^[a-zA-Z0-9_-]+$' THEN
    RAISE EXCEPTION 'invalid slug' USING ERRCODE = '22023';   -- 형식·길이 검증
  END IF;
  SELECT id INTO v_hid FROM public.hospitals WHERE slug = p_slug;
  IF v_hid IS NOT NULL THEN
    -- slug 충돌: 호출자가 그 병원의 활성 멤버일 때만 멱등 반환. 아니면 Conflict.
    IF p_owner_user IS NOT NULL THEN
      SELECT true INTO v_exists FROM public.hospital_memberships
        WHERE hospital_id = v_hid AND user_id = p_owner_user LIMIT 1;
    END IF;
    IF COALESCE(v_exists, false) THEN
      RETURN v_hid;                    -- 동일 사용자의 재시도 → 기존 병원(멱등)
    END IF;
    RAISE EXCEPTION 'hospital slug already exists' USING ERRCODE = '23505';  -- 관계없는 사용자 → 차단
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
    """app_rw로 SECURITY DEFINER 함수 호출 → hospital_id 반환(호출자가 멤버인 기존이면 그대로).
    관계없는 사용자가 기존 slug를 요청하면 ProvisionConflict."""
    from sqlalchemy.exc import DBAPIError
    try:
        with engine.connect() as cn:
            with cn.begin():
                return cn.execute(text("select public.fn_provision_hospital(:s,:n,:u)"),
                                  {"s": slug, "n": name, "u": owner_user}).scalar()
    except DBAPIError as e:
        code = getattr(getattr(e, "orig", None), "code", None) or ""
        if "23505" in str(e) or code == "23505":
            raise ProvisionConflict(slug) from e
        raise
