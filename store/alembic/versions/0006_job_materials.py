"""generation_job_materials 채택 — 생성 시점 자료 스냅샷(재현·책임소재).

과거 SQL 고정. CREATE IF NOT EXISTS + 정책 재적용. downgrade 비파괴.
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_T = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

def upgrade():
    op.execute("""
    CREATE TABLE IF NOT EXISTS generation_job_materials (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      hospital_id uuid NOT NULL REFERENCES hospitals(id),
      job_id uuid NOT NULL,
      material_id uuid,
      filename text NOT NULL,
      checksum text,
      size_bytes bigint,
      snapshot_at timestamptz NOT NULL DEFAULT now()
    );""")
    for stmt in [
        "ALTER TABLE generation_job_materials ENABLE ROW LEVEL SECURITY;",
        "ALTER TABLE generation_job_materials FORCE ROW LEVEL SECURITY;",
        "DROP POLICY IF EXISTS gjm_rw ON generation_job_materials;",
        f"CREATE POLICY gjm_rw ON generation_job_materials TO app_rw USING (hospital_id = {_T}) WITH CHECK (hospital_id = {_T});",
        "DROP POLICY IF EXISTS gjm_def ON generation_job_materials;",
        "CREATE POLICY gjm_def ON generation_job_materials TO app_owner USING (true) WITH CHECK (true);",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON generation_job_materials TO app_rw;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON generation_job_materials TO app_owner;",
    ]:
        op.execute(stmt)

def downgrade():
    raise RuntimeError("비가역 adoption 마이그레이션: 운영 데이터 보호를 위해 삭제하지 않음")
