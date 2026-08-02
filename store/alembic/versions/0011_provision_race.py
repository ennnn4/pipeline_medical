"""fn_provision_hospital 경쟁조건 보강 — 동시 slug INSERT 충돌 시 unique_violation 재판정.

CREATE OR REPLACE. downgrade는 0007 버전(예외처리 없음)으로 복원.
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_FN = """
CREATE OR REPLACE FUNCTION public.fn_provision_hospital(p_slug text, p_name text, p_owner_user uuid)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_hid uuid; v_mid uuid; v_exists boolean;
BEGIN
  IF p_slug IS NULL OR btrim(p_slug) = '' THEN
    RAISE EXCEPTION 'slug required' USING ERRCODE = '22023';
  END IF;
  IF length(btrim(p_slug)) > 64 OR p_slug !~ '^[a-zA-Z0-9_-]+$' THEN
    RAISE EXCEPTION 'invalid slug' USING ERRCODE = '22023';
  END IF;
  SELECT id INTO v_hid FROM public.hospitals WHERE slug = p_slug;
  IF v_hid IS NOT NULL THEN
    IF p_owner_user IS NOT NULL THEN
      SELECT true INTO v_exists FROM public.hospital_memberships
        WHERE hospital_id = v_hid AND user_id = p_owner_user LIMIT 1;
    END IF;
    IF COALESCE(v_exists, false) THEN RETURN v_hid; END IF;
    RAISE EXCEPTION 'hospital slug already exists' USING ERRCODE = '23505';
  END IF;
  v_hid := gen_random_uuid();
  BEGIN
    INSERT INTO public.hospitals(id, slug, name) VALUES (v_hid, p_slug, COALESCE(NULLIF(btrim(p_name), ''), p_slug));
  EXCEPTION WHEN unique_violation THEN
    SELECT id INTO v_hid FROM public.hospitals WHERE slug = p_slug;
    IF p_owner_user IS NOT NULL THEN
      SELECT true INTO v_exists FROM public.hospital_memberships
        WHERE hospital_id = v_hid AND user_id = p_owner_user LIMIT 1;
    END IF;
    IF COALESCE(v_exists, false) THEN RETURN v_hid; END IF;
    RAISE EXCEPTION 'hospital slug already exists' USING ERRCODE = '23505';
  END;
  IF p_owner_user IS NOT NULL THEN
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

def upgrade():
    op.execute(_FN)

def downgrade():
    pass   # 함수는 0007에서 재적용됨(다운그레이드 시 상위 revision 함수만 유지되어도 무해)
