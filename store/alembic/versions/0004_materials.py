"""materials 채택 — 업로드 자료 영속 저장(bytea).

Render 무료 티어 임시 디스크 문제 해결. store/materials.py 런타임 DDL과 동일 스키마.
CREATE IF NOT EXISTS + 정책 재적용으로 기존/신규 DB 모두 안전. 0001 불변.
(GPT P2: 대용량은 최종적으로 R2/S3 권장)
"""
from alembic import op
from store.materials import _DDL as MAT_DDL, _policies as mat_policies

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

def upgrade():
    op.execute(MAT_DDL)
    for stmt in mat_policies():
        op.execute(stmt)

def downgrade():
    op.execute("DROP TABLE IF EXISTS materials")
