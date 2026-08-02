"""권한 검증 — 역할 게이트. service가 actor context로 호출(라우트에서 재구현 금지)."""
from services.exceptions import Forbidden

EDIT_ROLES = {"editor", "approver", "admin"}
REVIEW_ROLES = {"approver", "admin"}      # 검수·승인·반려
IMAGE_ROLES = {"editor", "approver", "admin"}


def require(ctx, allowed):
    """ctx가 allowed 역할 중 하나라도 없으면 Forbidden. (테넌트 소유권은 tenant_conn RLS가 별도 강제)"""
    if not ctx.has_role(*allowed):
        raise Forbidden(f"이 작업에는 {'/'.join(sorted(allowed))} 권한이 필요합니다")
