"""fn_provision_hospital — 병원 프로비저닝 SECURITY DEFINER 함수.

과거 SQL 고정(앱 코드 import 금지). CREATE OR REPLACE라 재적용 안전. downgrade는 함수만 제거.
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_FN = """
CREATE OR REPLACE FUNCTION public.fn_provision_hospital(p_slug text, p_name text, p_owner_user uuid)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_hid uuid; v_mid uuid;
BEGIN
  IF p_slug IS NULL OR btrim(p_slug) = '' THEN RAISE EXCEPTION 'slug required' USING ERRCODE = '22023'; END IF;
  SELECT id INTO v_hid FROM public.hospitals WHERE slug = p_slug;
  IF v_hid IS NOT NULL THEN RETURN v_hid; END IF;
  v_hid := gen_random_uuid();
  INSERT INTO public.hospitals(id, slug, name) VALUES (v_hid, p_slug, COALESCE(NULLIF(btrim(p_name), ''), p_slug));
  IF p_owner_user IS NOT NULL THEN
    v_mid := gen_random_uuid();
    INSERT INTO public.hospital_memberships(id, hospital_id, user_id) VALUES (v_mid, v_hid, p_owner_user);
    INSERT INTO public.membership_roles(id, hospital_id, membership_id, role) VALUES (gen_random_uuid(), v_hid, v_mid, 'admin');
  END IF;
  INSERT INTO public.audit_events(id, hospital_id, action, entity_type, entity_id)
    VALUES (gen_random_uuid(), v_hid, 'hospital.provision', 'hospital', v_hid);
  RETURN v_hid;
END $$;
"""

def upgrade():
    op.execute(_FN)
    for stmt in [
        "GRANT SELECT, INSERT ON hospitals TO app_owner;",
        "REVOKE ALL ON FUNCTION public.fn_provision_hospital(text,text,uuid) FROM PUBLIC;",
        "GRANT EXECUTE ON FUNCTION public.fn_provision_hospital(text,text,uuid) TO app_rw;",
        "ALTER FUNCTION public.fn_provision_hospital(text,text,uuid) OWNER TO app_owner;",
    ]:
        op.execute(stmt)

def downgrade():
    op.execute("DROP FUNCTION IF EXISTS public.fn_provision_hospital(text,text,uuid)")
