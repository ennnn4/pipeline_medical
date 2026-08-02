"""material_versions 채택(P2-1) — 자료 immutable 버전 + 백필 + 불변 트리거.

파일 교체 시 원본 bytes를 UPDATE로 덮지 않고 새 version을 남겨 생성 job이 정확한 원본을 재현.
CREATE IF NOT EXISTS + ADD COLUMN IF NOT EXISTS. 백필 멱등. downgrade 비파괴(RuntimeError).
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_T = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

_MV = """
CREATE TABLE IF NOT EXISTS material_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  material_id uuid REFERENCES materials(id) ON DELETE SET NULL,
  hospital_id uuid NOT NULL REFERENCES hospitals(id),
  filename text NOT NULL,
  mime text,
  size_bytes bigint NOT NULL,
  checksum text,
  data bytea NOT NULL,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE materials ADD COLUMN IF NOT EXISTS current_version_id uuid;
ALTER TABLE generation_job_materials ADD COLUMN IF NOT EXISTS material_version_id uuid;
"""

_FREEZE = """
CREATE OR REPLACE FUNCTION public.fn_freeze_material_version() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'material_versions는 불변입니다(삭제 불가)' USING ERRCODE = '0A000';
  END IF;
  IF NEW.id IS DISTINCT FROM OLD.id
     OR NEW.hospital_id IS DISTINCT FROM OLD.hospital_id
     OR NEW.filename IS DISTINCT FROM OLD.filename
     OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
     OR NEW.checksum IS DISTINCT FROM OLD.checksum
     OR NEW.data IS DISTINCT FROM OLD.data
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'material_versions 내용은 불변입니다' USING ERRCODE = '0A000';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_freeze_material_version ON material_versions;
CREATE TRIGGER trg_freeze_material_version BEFORE UPDATE OR DELETE ON material_versions
  FOR EACH ROW EXECUTE FUNCTION public.fn_freeze_material_version();
"""

_BACKFILL = [
    "INSERT INTO material_versions(id, material_id, hospital_id, filename, mime, size_bytes, checksum, data, created_at) "
    "SELECT gen_random_uuid(), m.id, m.hospital_id, m.filename, m.mime, m.size_bytes, m.checksum, m.data, m.uploaded_at "
    "FROM materials m WHERE m.current_version_id IS NULL",
    "UPDATE materials m SET current_version_id = v.id FROM material_versions v "
    "WHERE v.material_id = m.id AND m.current_version_id IS NULL",
]

def upgrade():
    op.execute(_MV)
    for stmt in [
        "ALTER TABLE material_versions ENABLE ROW LEVEL SECURITY;",
        "ALTER TABLE material_versions FORCE ROW LEVEL SECURITY;",
        "DROP POLICY IF EXISTS mv_rw ON material_versions;",
        f"CREATE POLICY mv_rw ON material_versions TO app_rw USING (hospital_id = {_T}) WITH CHECK (hospital_id = {_T});",
        "DROP POLICY IF EXISTS mv_def ON material_versions;",
        "CREATE POLICY mv_def ON material_versions TO app_owner USING (true) WITH CHECK (true);",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON material_versions TO app_rw;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON material_versions TO app_owner;",
    ]:
        op.execute(stmt)
    for stmt in _BACKFILL:       # 기존 자료 백필(멱등) — freeze 전에
        op.execute(stmt)
    op.execute(_FREEZE)

def downgrade():
    raise RuntimeError("비가역 adoption 마이그레이션: 운영 자료 이력 보호를 위해 삭제하지 않음")
