"""P2-1b 스냅샷 무결성 — 복합 테넌트 FK + NOT NULL + 봉인 트리거 + 스냅샷 메타.

generation_job_materials를 material_versions/generation_jobs에 실제 FK로 결착(병원간 오연결 차단),
job이 pending 벗어나면 스냅샷 변경 금지(seal). 백필/정리 뒤 트리거 설치. downgrade 비파괴.
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_COLS = [
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS material_snapshot_at timestamptz;",
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS material_snapshot_count int;",
    "ALTER TABLE generation_jobs ADD COLUMN IF NOT EXISTS material_snapshot_hash text;",
]

_INTEGRITY = [
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_genjobs_hosp_id') THEN "
    "ALTER TABLE generation_jobs ADD CONSTRAINT uq_genjobs_hosp_id UNIQUE (hospital_id, id); END IF; END $$;",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_matver_hosp_id') THEN "
    "ALTER TABLE material_versions ADD CONSTRAINT uq_matver_hosp_id UNIQUE (hospital_id, id); END IF; END $$;",
    "UPDATE generation_job_materials g SET material_version_id = m.current_version_id "
    "FROM materials m WHERE g.material_id = m.id AND g.material_version_id IS NULL AND m.current_version_id IS NOT NULL;",
    "DELETE FROM generation_job_materials WHERE material_version_id IS NULL;",
    "DELETE FROM generation_job_materials a USING generation_job_materials b "
    "WHERE a.ctid < b.ctid AND a.job_id = b.job_id AND a.material_version_id = b.material_version_id;",
    "ALTER TABLE generation_job_materials ALTER COLUMN material_version_id SET NOT NULL;",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_gjm_job_ver') THEN "
    "ALTER TABLE generation_job_materials ADD CONSTRAINT uq_gjm_job_ver UNIQUE (job_id, material_version_id); END IF; END $$;",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_gjm_job_tenant') THEN "
    "ALTER TABLE generation_job_materials ADD CONSTRAINT fk_gjm_job_tenant "
    "FOREIGN KEY (hospital_id, job_id) REFERENCES generation_jobs(hospital_id, id) ON DELETE RESTRICT NOT VALID; END IF; END $$;",
    "ALTER TABLE generation_job_materials VALIDATE CONSTRAINT fk_gjm_job_tenant;",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_gjm_ver_tenant') THEN "
    "ALTER TABLE generation_job_materials ADD CONSTRAINT fk_gjm_ver_tenant "
    "FOREIGN KEY (hospital_id, material_version_id) REFERENCES material_versions(hospital_id, id) ON DELETE RESTRICT NOT VALID; END IF; END $$;",
    "ALTER TABLE generation_job_materials VALIDATE CONSTRAINT fk_gjm_ver_tenant;",
]

_SEAL = """
CREATE OR REPLACE FUNCTION public.fn_seal_job_materials() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_status text; v_jid uuid;
BEGIN
  v_jid := CASE WHEN TG_OP = 'DELETE' THEN OLD.job_id ELSE NEW.job_id END;
  SELECT status INTO v_status FROM generation_jobs WHERE id = v_jid;
  IF v_status IS NOT NULL AND v_status <> 'pending' THEN
    RAISE EXCEPTION 'job 스냅샷은 pending 이후 변경 불가(sealed, status=%)', v_status USING ERRCODE = '0A000';
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
    for s in _INTEGRITY:
        op.execute(s)
    op.execute(_SEAL)

def downgrade():
    raise RuntimeError("비가역 adoption 마이그레이션: 스냅샷 무결성 보호를 위해 되돌리지 않음")
