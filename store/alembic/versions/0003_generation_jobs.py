"""generation_jobs 채택(adoption) — 생성 작업/상태/idempotency.

과거 SQL 고정(앱 코드 import 금지). 기존 구 스키마(idempotency_key 기반)도 조건부 DDL로 안전 채택
(예외 무시 없음 — 조건부라 신규 DB에서도 실패 안 함). downgrade 비파괴.
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_T = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

def upgrade():
    op.execute("""
    CREATE TABLE IF NOT EXISTS generation_jobs (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      hospital_id uuid NOT NULL REFERENCES hospitals(id),
      script_id uuid,
      topic text NOT NULL,
      status text NOT NULL DEFAULT 'pending',
      phase text,
      request_idempotency_key text NOT NULL,
      content_hash text,
      version_id uuid,
      generation_reason text DEFAULT 'initial',
      raw_output jsonb,
      error_code text,
      error_message text,
      retry_count int NOT NULL DEFAULT 0,
      created_by_membership_id uuid,
      prompt_version text,
      started_at timestamptz,
      heartbeat_at timestamptz,
      finished_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    );""")
    for stmt in [
        "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS script_id uuid;",
        "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS phase text;",
        "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS request_idempotency_key text;",
        "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS content_hash text;",
        "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS generation_reason text DEFAULT 'initial';",
        "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS error_code text;",
        "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS started_at timestamptz;",
        "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz;",
        "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS finished_at timestamptz;",
        # 구 idempotency_key(NOT NULL)가 있을 때만 완화 — 조건부라 신규 DB에서 오류 없음
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='generation_jobs' AND column_name='idempotency_key') "
        "THEN ALTER TABLE generation_jobs ALTER COLUMN idempotency_key DROP NOT NULL; END IF; END $$;",
        "ALTER TABLE generation_jobs ENABLE ROW LEVEL SECURITY;",
        "ALTER TABLE generation_jobs FORCE ROW LEVEL SECURITY;",
        "DROP POLICY IF EXISTS gj_rw ON generation_jobs;",
        f"CREATE POLICY gj_rw ON generation_jobs TO app_rw USING (hospital_id = {_T}) WITH CHECK (hospital_id = {_T});",
        "DROP POLICY IF EXISTS gj_def ON generation_jobs;",
        "CREATE POLICY gj_def ON generation_jobs TO app_owner USING (true) WITH CHECK (true);",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON generation_jobs TO app_rw;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON generation_jobs TO app_owner;",
        # 기존 데이터 중복 키 사전검사(명확한 실패 메시지)
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM generation_jobs WHERE request_idempotency_key IS NOT NULL "
        "GROUP BY hospital_id, request_idempotency_key HAVING count(*) > 1) "
        "THEN RAISE EXCEPTION 'generation_jobs에 중복 request_idempotency_key가 있어 유니크 인덱스 생성 불가'; END IF; END $$;",
        "DROP INDEX IF EXISTS uq_genjobs_reqkey;",
        "CREATE UNIQUE INDEX uq_genjobs_reqkey ON generation_jobs(hospital_id, request_idempotency_key);",
    ]:
        op.execute(stmt)

def downgrade():
    raise RuntimeError("비가역 adoption 마이그레이션: 운영 데이터 보호를 위해 삭제하지 않음")
