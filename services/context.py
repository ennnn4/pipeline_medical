"""ActorContext — 서버가 인증 세션에서 결정한 실행 주체(SDR 준수).

라우트가 session user_id + slug로 resolve하면 hospital_id·membership_id·roles를
DB에서 서버가 결정(요청 body의 membership/role은 신뢰하지 않는다). service는 이 컨텍스트만 받는다.
"""
from dataclasses import dataclass, field
from sqlalchemy import text
from store.repositories import tenant_conn
from services.exceptions import Unauthorized, NotFound, Forbidden


@dataclass(frozen=True)
class ActorContext:
    user_id: str
    hospital_id: str
    membership_id: str
    roles: frozenset = field(default_factory=frozenset)
    request_id: str = None

    def has_role(self, *roles):
        return bool(self.roles.intersection(roles))

    @classmethod
    def resolve(cls, engine, user_id, slug, request_id=None):
        """session user_id + 병원 slug → 서버 결정 컨텍스트. 없으면 Unauthorized/NotFound/Forbidden."""
        if not user_id:
            raise Unauthorized("로그인이 필요합니다")
        with engine.connect() as c0:      # hospitals는 RLS 밖(app_rw SELECT 허용)
            hid = c0.execute(text("select id from hospitals where slug=:s"), {"s": slug}).scalar()
        if not hid:
            raise NotFound("병원을 찾을 수 없습니다")
        with tenant_conn(engine, hid) as c1:   # membership·roles는 tenant 컨텍스트에서 서버 조회
            mid = c1.execute(text("select id from hospital_memberships "
                                  "where hospital_id=:h and user_id=:u and archived_at is null"),
                             {"h": hid, "u": user_id}).scalar()
            if not mid:
                raise Forbidden("이 병원에 대한 접근 권한이 없습니다")
            roles = {r[0] for r in c1.execute(text(
                "select role from membership_roles where hospital_id=:h and membership_id=:m"),
                {"h": hid, "m": mid})}
        return cls(user_id=str(user_id), hospital_id=str(hid), membership_id=str(mid),
                   roles=frozenset(roles), request_id=request_id)
