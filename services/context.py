"""ActorContext — 서버가 인증 세션에서 결정한 실행 주체(SDR 준수).

라우트가 session user_id + slug로 resolve하면 hospital_id·membership_id·roles를
DB에서 서버가 결정(요청 body의 membership/role은 신뢰하지 않는다). service는 이 컨텍스트만 받는다.
"""
from dataclasses import dataclass, field
from sqlalchemy import text
from store.repositories import tenant_conn
from services.exceptions import Unauthorized, NotFound, Forbidden


# platform 부여 role(grant_source='platform')은 '연결된 grant가 active'일 때만 유효(GPT) — grant 철회
# 즉시 무효. 병원이 직접 준 role(grant_source='hospital')은 항상 유효(revoked_at 없을 때).
_ROLE_Q = (
    "select mr.role from membership_roles mr "
    "left join platform_access_grants g on g.id = mr.platform_grant_id "
    "where mr.hospital_id=:h and mr.membership_id=:m and mr.revoked_at is null "
    "and (mr.grant_source <> 'platform' or g.status = 'active')")


@dataclass(frozen=True)
class ActorContext:
    user_id: str
    hospital_id: str
    membership_id: str
    roles: frozenset = field(default_factory=frozenset)
    request_id: str = None
    access_origin: str = "hospital_membership"   # 'hospital_membership' | 'platform_operator' — 관측/감사/UI 전용

    def has_role(self, *roles):
        return bool(self.roles.intersection(roles))

    @classmethod
    def resolve(cls, engine, user_id, slug, request_id=None):
        """session user_id + 병원 slug → 서버 결정 컨텍스트. 없으면 Unauthorized/NotFound/Forbidden.

        일반 멤버가 아니면 platform operator 경로 시도: active grant가 있으면 fn_ensure로 멤버십을
        자동 프로비저닝(GPT 승인 설계)하고 실제 membership으로 이후 로직을 일반과 동일하게 진행한다.
        platform status의 역할은 '멤버십 자동생성 자격 확인'까지 — 이후 권한은 role/capability로 정상 검사."""
        if not user_id:
            raise Unauthorized("로그인이 필요합니다")
        with engine.connect() as c0:      # hospitals는 RLS 밖(app_rw SELECT 허용)
            hid = c0.execute(text("select id from hospitals where slug=:s"), {"s": slug}).scalar()
        if not hid:
            raise NotFound("병원을 찾을 수 없습니다")
        with tenant_conn(engine, hid, user_id=user_id) as c1:
            has_grant = c1.execute(text("select exists(select 1 from platform_access_grants "
                                        "where user_id=:u and status='active')"), {"u": user_id}).scalar()
            mid = c1.execute(text("select id from hospital_memberships "
                                  "where hospital_id=:h and user_id=:u and archived_at is null"),
                             {"h": hid, "u": user_id}).scalar()
            if has_grant:
                # active grant → 멤버십 자동 보장 + platform_operator role 동기화(기존 멤버십·역할 보존)
                mid = c1.execute(text("select public.fn_ensure_platform_operator_membership(:h)"),
                                 {"h": hid}).scalar()
            elif not mid:
                raise Forbidden("이 병원에 대한 접근 권한이 없습니다")
            roles = {r[0] for r in c1.execute(text(_ROLE_Q), {"h": hid, "m": mid})}
            src = c1.execute(text("select coalesce(bool_or(grant_source='platform'),false), "
                                  "coalesce(bool_or(grant_source='hospital'),false) "
                                  "from membership_roles where hospital_id=:h and membership_id=:m"),
                             {"h": hid, "m": mid}).first()
            platform_only = bool(src[0]) and not bool(src[1])
            # 접근 근거가 platform뿐인데 active grant 없음(철회된 leftover) → 즉시 거부(GPT)
            if platform_only and not has_grant:
                raise Forbidden("이 병원에 대한 접근 권한이 없습니다")
        origin = "platform_operator" if platform_only else "hospital_membership"
        return cls(user_id=str(user_id), hospital_id=str(hid), membership_id=str(mid),
                   roles=frozenset(roles), request_id=request_id, access_origin=origin)
