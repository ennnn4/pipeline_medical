"""P0 초기 스키마 — 25테이블 + 3열복합FK(현재버전 use_alter) + RLS/함수/뷰/역할.

Revision ID: 0001
Revises:
"""
from alembic import op
from store.schema import metadata
from store import rls_sql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# scripts↔script_versions 순환 → use_alter 상당: 테이블 생성 후 별도 추가/삭제
CURRENT_VERSION_FK = (
    "ALTER TABLE scripts ADD CONSTRAINT fk_scripts_current_version "
    "FOREIGN KEY (hospital_id, id, current_version_id) "
    "REFERENCES script_versions (hospital_id, script_id, id)"
)

def upgrade():
    bind = op.get_bind()
    metadata.create_all(bind)                 # 25테이블(순환 FK 제외)
    op.execute(CURRENT_VERSION_FK)            # 순환 FK를 테이블 생성 후 추가
    for stmt in rls_sql.statements():         # 역할·RLS·정책·함수·뷰
        op.execute(stmt)

def downgrade():
    op.execute("DROP VIEW IF EXISTS claim_effective_assessment")
    op.execute("DROP VIEW IF EXISTS claim_latest_assessment")
    op.execute("DROP FUNCTION IF EXISTS exchange_review_token(bytea)")
    op.execute("DROP FUNCTION IF EXISTS lookup_user_for_login(text)")
    op.execute("DROP FUNCTION IF EXISTS get_current_user(uuid)")
    op.execute("ALTER TABLE scripts DROP CONSTRAINT IF EXISTS fk_scripts_current_version")
    metadata.drop_all(op.get_bind())
