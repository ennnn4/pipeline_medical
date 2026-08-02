"""P2-1 마감 — 봉인 원자성(material_snapshot_at 기반 seal) + worker_token + 병원당 active 유니크.

GPT 조건부 승인 마감: seal을 봉인시각에도 연결, 실행권 소유 토큰, 동시 실행 병원당 1개.
비파괴 additive. downgrade는 index/트리거만 되돌림.
"""
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_COLS = [
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS worker_token text;",
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS claimed_at timestamptz;",
]

_ACTIVE_IDX = [
    "UPDATE generation_jobs SET status='stale', updated_at=now() "
    "WHERE status IN ('generating','generated','ingesting') AND id NOT IN ("
    "  SELECT DISTINCT ON (hospital_id) id FROM generation_jobs "
    "  WHERE status IN ('generating','generated','ingesting') ORDER BY hospital_id, updated_at DESC);",
    "DROP INDEX IF EXISTS uq_genjobs_one_active;",
    "CREATE UNIQUE INDEX uq_genjobs_one_active ON generation_jobs(hospital_id) "
    "WHERE status IN ('generating','generated','ingesting');",
]

_SEAL = """
CREATE OR REPLACE FUNCTION public.fn_seal_job_materials() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_status text; v_sealed timestamptz; v_jid uuid;
BEGIN
  v_jid := CASE WHEN TG_OP = 'DELETE' THEN OLD.job_id ELSE NEW.job_id END;
  SELECT status, material_snapshot_at INTO v_status, v_sealed FROM generation_jobs WHERE id = v_jid;
  IF v_sealed IS NOT NULL OR (v_status IS NOT NULL AND v_status <> 'pending') THEN
    RAISE EXCEPTION 'job 스냅샷은 봉인 후 변경 불가(sealed_at=%, status=%)', v_sealed, v_status USING ERRCODE = '0A000';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;
DROP TRIGGER IF EXISTS trg_seal_job_materials ON generation_job_materials;
CREATE TRIGGER trg_seal_job_materials BEFORE INSERT OR UPDATE OR DELETE ON generation_job_materials
  FOR EACH ROW EXECUTE FUNCTION public.fn_seal_job_materials();
"""

def upgrade():
    for s in _COLS:
        op.execute(s)
    for s in _ACTIVE_IDX:
        op.execute(s)
    op.execute(_SEAL)

def downgrade():
    op.execute("DROP INDEX IF EXISTS uq_genjobs_one_active;")
