"""materials 채택 — 업로드 자료 영속 저장(bytea).

과거 SQL 고정(앱 코드 import 금지). CREATE IF NOT EXISTS + 정책 재적용. downgrade 비파괴.
(GPT P2: 대용량은 최종적으로 R2/S3 권장 + 병원별 quota)
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_T = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

def upgrade():
    op.execute("""
    CREATE TABLE IF NOT EXISTS materials (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      hospital_id uuid NOT NULL REFERENCES hospitals(id),
      filename text NOT NULL,
      mime text,
      size_bytes bigint NOT NULL,
      checksum text,
      data bytea NOT NULL,
      uploaded_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (hospital_id, filename)
    );""")
    for stmt in [
        "ALTER TABLE materials ENABLE ROW LEVEL SECURITY;",
        "ALTER TABLE materials FORCE ROW LEVEL SECURITY;",
        "DROP POLICY IF EXISTS mat_rw ON materials;",
        f"CREATE POLICY mat_rw ON materials TO app_rw USING (hospital_id = {_T}) WITH CHECK (hospital_id = {_T});",
        "DROP POLICY IF EXISTS mat_def ON materials;",
        "CREATE POLICY mat_def ON materials TO app_owner USING (true) WITH CHECK (true);",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON materials TO app_rw;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON materials TO app_owner;",
    ]:
        op.execute(stmt)

def downgrade():
    raise RuntimeError("비가역 adoption 마이그레이션: 운영 데이터 보호를 위해 삭제하지 않음")
