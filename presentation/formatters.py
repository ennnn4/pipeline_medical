"""화면용 소형 포매터 — 상태명 한글화·배지·csrf 필드. Flask 미의존(순수 함수)."""
from markupsafe import escape

SUPPORT_KO = {"direct": "직접근거", "partial": "부분근거", "inferred": "추론",
              "unsupported": "근거없음", "unverified": "미검증"}
KIND_KO = {"automated": "자동검증", "human_review": "원장검수", "override": "원장확정", "migration": "이관"}


def csrf_field(csrf):
    """CSRF 히든 필드. csrf=세션 토큰 문자열(앱이 주입)."""
    return f'<input type=hidden name=_csrf value="{escape(csrf or "")}">'


def verification_badge(vs):
    """근거 판정 상태 → (style, label)."""
    if vs == "verified":
        return "background:#e6f7f0;color:#12b886", "검증됨"
    if vs == "failed":
        return "background:#fdeaec;color:#f04452", "반려/실패"
    return "background:#f2f4f6;color:#8b95a1", "미검증"
