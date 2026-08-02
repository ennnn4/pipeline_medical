"""approval service — 승인/자기승인/반려/철회. 라우트 공유.

권한·상태전이·잠금·작성자≠승인자·자기승인 override는 DB definer 함수(store/approval_fns)가 강제.
service는 actor context로 tenant_conn 열고 함수 호출 + SQLSTATE→타입 예외 번역."""
import uuid
from sqlalchemy.exc import DBAPIError
from store.repositories import tenant_conn
from store import repositories as repo
from services import permissions
from services.exceptions import from_sqlstate, InvalidStateTransition

DEFAULT_POLICY = "policy-1"


def _sqlstate(exc):
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", None)
    if args and isinstance(args[0], dict):
        return args[0].get("C")
    return getattr(orig, "sqlstate", None)


def _as_uuid(v):
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


def _call(engine, ctx, fn, *args):
    try:
        with tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id,
                         request_id=ctx.request_id) as conn:
            return fn(conn, _as_uuid(ctx.hospital_id), *args)
    except DBAPIError as e:
        raise from_sqlstate(_sqlstate(e), str(e))


def _ok(event, ctx, self_approval=None):
    # 성공 이벤트(GPT): "오류 없음"과 "실제 사용됨" 구분. 내용·사유·slug·이메일 미포함, 병원은 해시.
    try:
        from services.observability import emit, hid
        emit(event, hospital=hid(ctx.hospital_id), self_approval=self_approval,
             request_id=getattr(ctx, "request_id", None))
    except Exception:
        pass


def approve(engine, ctx, version_id, policy=DEFAULT_POLICY):
    """정상 승인 — 작성자≠승인자·current·evidence gate는 DB가 강제(approver/admin)."""
    permissions.require(ctx, permissions.REVIEW_ROLES)
    r = _call(engine, ctx, repo.approve_version, _as_uuid(version_id), policy)
    _ok("approval_succeeded", ctx, self_approval=False)
    return r


def self_approve(engine, ctx, version_id, reason, policy=DEFAULT_POLICY):
    """자기승인(작성자==승인자) — allow_self_approval + admin + 사유. DB가 강제."""
    permissions.require(ctx, {"admin"})
    if not (reason or "").strip():
        raise InvalidStateTransition("자기승인에는 사유가 필요합니다")
    r = _call(engine, ctx, repo.self_approve_version, _as_uuid(version_id), policy, reason.strip())
    _ok("approval_succeeded", ctx, self_approval=True)
    return r


def reject(engine, ctx, version_id, reason):
    """반려(none/pending→rejected, current·사유)."""
    permissions.require(ctx, permissions.REVIEW_ROLES)
    if not (reason or "").strip():
        raise InvalidStateTransition("반려에는 사유가 필요합니다")
    r = _call(engine, ctx, repo.reject_version, _as_uuid(version_id), reason.strip())
    _ok("approval_rejected", ctx)
    return r


def revoke(engine, ctx, version_id, reason):
    """승인 철회(approved→revoked, admin·사유, 비-current 과거승인도 허용)."""
    permissions.require(ctx, {"admin"})
    if not (reason or "").strip():
        raise InvalidStateTransition("철회에는 사유가 필요합니다")
    r = _call(engine, ctx, repo.revoke_version, _as_uuid(version_id), reason.strip())
    _ok("approval_revoked", ctx)
    return r
