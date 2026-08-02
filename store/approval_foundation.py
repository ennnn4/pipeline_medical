"""승인 기반(Step 2.5) — 작성자/생성유형·superseded 분리·자기승인 설정의 멱등 DDL.

core 테이블(script_versions/version_approval_states/hospitals) additive 변경.
schema.py(create_all 경로)에도 반영돼 있고, 여기서 기존 라이브 DB에 ALTER로 적용(멱등)."""
from sqlalchemy import text

STMTS = [
    # 자기승인 정책(inv12) — 기본 금지
    "ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS allow_self_approval boolean NOT NULL DEFAULT false;",
    # AI 버전의 생성 job 링크(작성자 유형)
    "ALTER TABLE script_versions ADD COLUMN IF NOT EXISTS generation_job_id uuid;",
    # superseded 분리(승인 상태가 아닌 수명주기) — inv14
    "ALTER TABLE version_approval_states ADD COLUMN IF NOT EXISTS superseded_by_version_id uuid;",
    "ALTER TABLE version_approval_states ADD COLUMN IF NOT EXISTS superseded_at timestamptz;",
    # status CHECK 확장(revoked 추가) — 기존 값 부분집합이라 즉시 valid
    "ALTER TABLE version_approval_states DROP CONSTRAINT IF EXISTS ck_version_approval_states_status;",
    "ALTER TABLE version_approval_states ADD CONSTRAINT ck_version_approval_states_status "
    "CHECK (status IN ('none','approved','rejected','revoked'));",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_version_approval_states_revoked_fields') THEN "
    "ALTER TABLE version_approval_states ADD CONSTRAINT ck_version_approval_states_revoked_fields "
    "CHECK (status <> 'revoked' OR (approver_membership_id IS NOT NULL AND decided_at IS NOT NULL)); END IF; END $$;",
    # superseded_by 복합 FK(테넌트)
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_approval_states_superseded_by') THEN "
    "ALTER TABLE version_approval_states ADD CONSTRAINT fk_approval_states_superseded_by "
    "FOREIGN KEY (hospital_id, superseded_by_version_id) REFERENCES script_versions(hospital_id, id) NOT VALID; END IF; END $$;",
    "ALTER TABLE version_approval_states VALIDATE CONSTRAINT fk_approval_states_superseded_by;",
    # generation_job_id 복합 FK(테넌트) — generation_jobs 있을 때만
    "DO $$ BEGIN IF to_regclass('public.generation_jobs') IS NOT NULL AND NOT EXISTS "
    "(SELECT 1 FROM pg_constraint WHERE conname='fk_versions_generation_job') THEN "
    "ALTER TABLE script_versions ADD CONSTRAINT fk_versions_generation_job "
    "FOREIGN KEY (hospital_id, generation_job_id) REFERENCES generation_jobs(hospital_id, id) NOT VALID; END IF; END $$;",
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_versions_generation_job' AND NOT convalidated) THEN "
    "ALTER TABLE script_versions VALIDATE CONSTRAINT fk_versions_generation_job; END IF; END $$;",
]


def ensure_approval_foundation(owner_engine):
    with owner_engine.begin() as cn:
        for s in STMTS:
            cn.execute(text(s))
