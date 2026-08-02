"""scene_images 채택(adoption) — 장면 이미지 DB 저장.

과거 migration은 작성 당시 SQL로 '고정'한다(앱 코드 import 금지 — GPT 리뷰).
기존 운영 DB엔 런타임 DDL로 이미 존재할 수 있어 CREATE IF NOT EXISTS + 정책 재적용(idempotent).
downgrade는 파괴적이므로 제공하지 않음(테이블이 이 리비전보다 먼저 존재했을 수 있음).
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_T = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

def upgrade():
    op.execute("""
    CREATE TABLE IF NOT EXISTS scene_images (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      hospital_id uuid NOT NULL REFERENCES hospitals(id),
      topic text NOT NULL,
      block_key text NOT NULL,
      mime text NOT NULL DEFAULT 'image/jpeg',
      data bytea NOT NULL,
      prompt text,
      model text,
      updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (hospital_id, topic, block_key)
    );""")
    for stmt in [
        "ALTER TABLE scene_images ENABLE ROW LEVEL SECURITY;",
        "ALTER TABLE scene_images FORCE ROW LEVEL SECURITY;",
        "DROP POLICY IF EXISTS si_rw ON scene_images;",
        f"CREATE POLICY si_rw ON scene_images TO app_rw USING (hospital_id = {_T}) WITH CHECK (hospital_id = {_T});",
        "DROP POLICY IF EXISTS si_def ON scene_images;",
        "CREATE POLICY si_def ON scene_images TO app_owner USING (true) WITH CHECK (true);",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON scene_images TO app_rw;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON scene_images TO app_owner;",
    ]:
        op.execute(stmt)

def downgrade():
    raise RuntimeError("비가역 adoption 마이그레이션: 테이블이 이 리비전보다 먼저 존재했을 수 있어 삭제하지 않음")
