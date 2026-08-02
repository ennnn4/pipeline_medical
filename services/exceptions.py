"""service 계층 타입 예외 — 문자열로 흩뿌리지 않고 명시적 타입으로 구분(GPT 계약).

라우트는 이 예외를 잡아 HTTP로 매핑(http_status). SQL 오류(SQLSTATE)는 service 경계에서
이 타입으로 번역해 상위가 DB 세부에 의존하지 않게 한다."""


class ServiceError(Exception):
    """service 계층 공통 베이스. http_status로 라우트가 응답 코드 결정."""
    http_status = 400
    code = "service_error"

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        # 모든 service 예외의 단일 병목 — 유형·코드별 발생을 구조화 로깅(사후 집계용).
        try:
            from services.observability import emit
            emit("service_error", code=self.code, status=self.http_status,
                 type=type(self).__name__, msg=(str(args[0])[:200] if args else None))
        except Exception:
            pass


class Unauthorized(ServiceError):
    http_status = 401
    code = "unauthorized"


class NotFound(ServiceError):
    http_status = 404
    code = "not_found"


class Forbidden(ServiceError):
    http_status = 403
    code = "forbidden"


class VersionConflict(ServiceError):
    http_status = 409
    code = "version_conflict"


class InvalidStateTransition(ServiceError):
    http_status = 409
    code = "invalid_state_transition"


class ApprovalPrerequisiteFailed(ServiceError):
    http_status = 422
    code = "approval_prerequisite_failed"


class SelfApprovalNotAllowed(ServiceError):
    http_status = 403
    code = "self_approval_not_allowed"


class SnapshotIntegrityError(ServiceError):
    http_status = 409
    code = "snapshot_integrity_error"


class HospitalBusy(ServiceError):
    http_status = 409
    code = "hospital_busy"


# PostgreSQL SQLSTATE → service 예외(라우트가 DB 세부에 의존하지 않도록 경계에서 번역)
_SQLSTATE_MAP = {
    "42501": Forbidden,
    "23514": ApprovalPrerequisiteFailed,
    "P0002": NotFound,
    "P2013": InvalidStateTransition,   # decided_version_frozen(승인/철회 version 내용·근거 동결)
    "P2014": VersionConflict,          # version supersede 충돌(다른 버전이 이미 supersede)
    "P2015": VersionConflict,          # 승인/반려 대상이 current version 아님
}


def from_sqlstate(sqlstate, message=""):
    """SQLSTATE 코드를 해당 service 예외로 번역(없으면 ServiceError)."""
    cls = _SQLSTATE_MAP.get(sqlstate, ServiceError)
    try:                                    # 원시 SQLSTATE 차원(P2013/P2014/P2015 구분 집계)
        from services.observability import emit
        emit("sqlstate", sqlstate=sqlstate, code=cls.code)
    except Exception:
        pass
    return cls(message or sqlstate)
