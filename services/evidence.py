"""evidence service — claim 근거 검수(사람 판정). 라우트(대시보드·/studio) 공유.

GPT 규칙:
- 변경 대상 version_id를 명시적으로 받는다(암묵적 current 조회 금지).
- mutation 시 target == scripts.current_version_id 재검사(과거 화면에서의 지연 저장 차단).
- approved/revoked version의 근거는 변경 금지(InvalidStateTransition) — 새 version 필요.
- approval과 동일 잠금 순서: scripts FOR UPDATE → approval state → claim/assessment.
- 부분 성공 금지(단일 claim 처리, 실패 시 전체 rollback). audit=append-only assessment(actor·시각·사유).
"""
import uuid
from sqlalchemy import text
from store.repositories import tenant_conn
from services import permissions
from services.exceptions import NotFound, VersionConflict, InvalidStateTransition, Forbidden

# 사람 결정 → (support_level, verification_status, medical_risk, human_decision, 기본 사유)
# verification_status(검증결과)와 human_decision(사람 처리)은 별개 축(GPT).
_DECISION = {
    "confirm":        ("direct", "verified", "low", "accepted", "원장 확정"),
    "reject":         ("unsupported", "failed", "high", "rejected", "원장 반려"),
    "waive":          ("unverified", "failed", "medium", "waived", None),          # 근거 예외(사유 필수)
    "not_applicable": ("unverified", "pending", "low", "not_applicable", None),    # 검증 대상 아님(사유 필수)
}
_ELEVATED = {"waive", "not_applicable"}   # admin(waiver/not_applicable capability)만


def assess_claim(engine, ctx, script_id, version_id, claim_id, decision, reason=None):
    """claim에 사람 판정(human_review) append. confirm/reject=approver/admin, waive/not_applicable=admin.
    반환: {claim_id, version_id, decision}."""
    if decision not in _DECISION:
        raise InvalidStateTransition(f"허용되지 않은 결정: {decision}")
    permissions.require(ctx, {"admin"} if decision in _ELEVATED else permissions.REVIEW_ROLES)
    sup, vf, risk, human_decision, default_reason = _DECISION[decision]
    reason = (reason or default_reason or "").strip()
    if decision in _ELEVATED and not reason:
        raise InvalidStateTransition(f"{decision}에는 사유가 필요합니다")
    sid = script_id if isinstance(script_id, uuid.UUID) else uuid.UUID(str(script_id))
    vid = version_id if isinstance(version_id, uuid.UUID) else uuid.UUID(str(version_id))
    cid = claim_id if isinstance(claim_id, uuid.UUID) else uuid.UUID(str(claim_id))
    with tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id,
                     request_id=ctx.request_id) as conn:
        # 1) scripts 행 잠금(승인과 동일 순서) + current version 확인
        sc = conn.execute(text("select current_version_id from scripts where id=:s and hospital_id=:h for update"),
                          {"s": sid, "h": ctx.hospital_id}).first()
        if sc is None:
            raise NotFound("스크립트를 찾을 수 없습니다")
        if str(sc.current_version_id) != str(vid):
            raise VersionConflict("현재 버전이 변경되었습니다(다른 편집 선반영) — 최신 버전에서 검수하세요")
        # 2) approval 상태 — approved/revoked는 근거 변경 금지
        st = conn.execute(text("select status from version_approval_states where hospital_id=:h and version_id=:v"),
                          {"h": ctx.hospital_id, "v": vid}).scalar()
        if st in ("approved", "revoked"):
            raise InvalidStateTransition(f"{st} 버전의 근거는 변경할 수 없습니다 — 새 버전을 만들어 편집하세요")
        # 3) claim이 이 version 소속인지(다른 버전/병원 claim 차단)
        owns = conn.execute(text("select 1 from claims where hospital_id=:h and id=:c and version_id=:v"),
                            {"h": ctx.hospital_id, "c": cid, "v": vid}).scalar()
        if not owns:
            raise NotFound("이 버전에 속한 근거(claim)가 아닙니다")
        # 4) 사람 판정 append(불변) — effective view가 최신 human을 automated보다 우선
        conn.execute(text(
            "insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,"
            "support_level,verification_status,medical_risk,rationale,human_decision,decision_reason,"
            "created_by_membership_id) "
            "values(:i,:h,:c,'human_review',:ik,:sup,:vf,:risk,:ra,:hd,:dr,"
            "NULLIF(current_setting('app.membership_id', true), '')::uuid)"),
            {"i": uuid.uuid4(), "h": ctx.hospital_id, "c": cid, "ik": uuid.uuid4().hex,
             "sup": sup, "vf": vf, "risk": risk, "ra": reason[:2000],
             "hd": human_decision, "dr": reason[:2000]})
    return {"claim_id": str(cid), "version_id": str(vid), "decision": decision, "human_decision": human_decision}
