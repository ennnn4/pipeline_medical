"""Step 2.5 승인 기반 — 작성자 채우기 준비(generation_job_id) + superseded 분리 + 자기승인 설정 + revoked 상태.

core 테이블 additive. downgrade는 추가 컬럼/제약만 되돌림(데이터 보존).
"""
from alembic import op
from store.approval_foundation import STMTS

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    for s in STMTS:
        op.execute(s)


def downgrade():
    op.execute("ALTER TABLE version_approval_states DROP CONSTRAINT IF EXISTS fk_approval_states_superseded_by;")
    op.execute("ALTER TABLE script_versions DROP CONSTRAINT IF EXISTS fk_versions_generation_job;")
    op.execute("ALTER TABLE version_approval_states DROP CONSTRAINT IF EXISTS ck_version_approval_states_revoked_fields;")
