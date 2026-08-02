"""generation_jobs 제약 강화(P2-6) — status CHECK 화이트리스트 + request key NOT NULL.

앱 검증 외 DB 최종 방어선. NOT VALID 후 VALIDATE(기존 데이터 안전). 멱등.
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

def upgrade():
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_genjobs_status') THEN "
        "ALTER TABLE generation_jobs ADD CONSTRAINT chk_genjobs_status CHECK "
        "(status IN ('pending','generating','generated','ingesting','completed','failed','cancelled','stale')) NOT VALID; "
        "END IF; END $$;")
    op.execute("ALTER TABLE generation_jobs VALIDATE CONSTRAINT chk_genjobs_status;")
    op.execute("UPDATE generation_jobs SET request_idempotency_key = gen_random_uuid()::text "
               "WHERE request_idempotency_key IS NULL;")
    op.execute("ALTER TABLE generation_jobs ALTER COLUMN request_idempotency_key SET NOT NULL;")

def downgrade():
    op.execute("ALTER TABLE generation_jobs DROP CONSTRAINT IF EXISTS chk_genjobs_status;")
