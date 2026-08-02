"""generation_jobs 채택(adoption) — 생성 작업/상태/idempotency.

기존 운영 DB에 구 스키마(idempotency_key 기반)로 존재할 수 있어, CREATE IF NOT EXISTS +
신규 컬럼 ADD IF NOT EXISTS(_ADOPT) + 정책/유니크 재적용으로 안전 채택. 0001은 수정하지 않음.
"""
from alembic import op
from store.ingest import _DDL as GEN_DDL, _ADOPT as GEN_ADOPT, _gen_policies

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

def upgrade():
    op.execute(GEN_DDL)                     # CREATE TABLE IF NOT EXISTS generation_jobs(신 스키마)
    for stmt in GEN_ADOPT:                  # 구 테이블 컬럼 채택(ADD COLUMN IF NOT EXISTS 등)
        try:
            op.execute(stmt)
        except Exception:
            pass
    for stmt in _gen_policies():            # RLS 정책 + grant + request_idempotency_key 유니크
        op.execute(stmt)

def downgrade():
    op.execute("DROP TABLE IF EXISTS generation_jobs")
