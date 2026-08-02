"""scene_images 채택(adoption) — 장면 이미지 DB 저장.

기존 운영 DB에는 store/seed_images.py의 런타임 DDL로 이미 존재할 수 있어,
CREATE TABLE IF NOT EXISTS + 정책 재적용(idempotent) 방식으로 안전 채택.
신규 DB에서는 처음부터 정상 생성. 0001은 수정하지 않음.
"""
from alembic import op
from store.seed_images import DDL as SCENE_DDL, _policies as scene_policies

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

def upgrade():
    op.execute(SCENE_DDL)              # CREATE TABLE IF NOT EXISTS scene_images
    for stmt in scene_policies():      # ENABLE/FORCE RLS + tenant/definer 정책 + grant (모두 idempotent)
        op.execute(stmt)

def downgrade():
    op.execute("DROP TABLE IF EXISTS scene_images")
