"""권한 검증 — 역할 게이트. service가 actor context로 호출(라우트에서 재구현 금지)."""
from services.exceptions import Forbidden

# platform_operator(대행사 운영자, GPT): 편집·이미지·근거 accepted/rejected·export까지.
# 최종승인·자기승인·철회(REVIEW_ROLES)와 waived/not_applicable(admin)에는 포함하지 않음 — 병원 approver 몫.
EDIT_ROLES = {"editor", "approver", "admin", "platform_operator"}
REVIEW_ROLES = {"approver", "admin"}                        # 최종 승인·반려·철회(platform_operator 제외)
EVIDENCE_REVIEW_ROLES = {"approver", "admin", "platform_operator"}   # 근거 confirm/reject만(waive/na는 admin)
IMAGE_ROLES = {"editor", "approver", "admin", "platform_operator"}


def require(ctx, allowed):
    """ctx가 allowed 역할 중 하나라도 없으면 Forbidden. (테넌트 소유권은 tenant_conn RLS가 별도 강제)"""
    if not ctx.has_role(*allowed):
        raise Forbidden(f"이 작업에는 {'/'.join(sorted(allowed))} 권한이 필요합니다")
