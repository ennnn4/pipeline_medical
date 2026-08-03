"""platform operator(대행사 전 병원 접근) — GPT 승인 설계 검증.

접근: 일반 병원 격리 유지 + platform grant 있는 유저만 멤버십 게이트 우회(자동 프로비저닝).
권한: 편집·이미지·근거 accepted/rejected·export는 되고 최종승인·자기승인·철회·waive/na는 불가.
철회: grant revoke 즉시 접근 불가, 병원이 직접 준 역할은 유지."""
import uuid, pytest
from sqlalchemy import text
from services.context import ActorContext
from services.exceptions import Forbidden
from services import permissions
from store.platform_ops import grant_platform_operator, revoke_platform_operator, ensure_platform_admin_user


def _user(owner):
    uid = uuid.uuid4()
    with owner.begin() as cn:
        cn.execute(text("insert into users(id,email) values(:u,:e)"), {"u": uid, "e": uid.hex + "@t.c"})
    return uid


def _hospital(owner, slug=None):
    hid = uuid.uuid4(); slug = slug or ("h" + hid.hex[:10])
    with owner.begin() as cn:
        cn.execute(text("insert into hospitals(id,slug,name,status) values(:h,:s,'T','active')"), {"h": hid, "s": slug})
    return hid, slug


def _member(owner, hid, uid, role=None):
    mid = uuid.uuid4()
    with owner.begin() as cn:
        cn.execute(text("insert into hospital_memberships(id,hospital_id,user_id) values(:m,:h,:u)"),
                   {"m": mid, "h": hid, "u": uid})
        if role:
            cn.execute(text("insert into membership_roles(id,hospital_id,membership_id,role) values(:i,:h,:m,:r)"),
                       {"i": uuid.uuid4(), "h": hid, "m": mid, "r": role})
    return mid


# ── 접근 ──
def test_non_member_without_grant_forbidden(rw, owner):
    uid = _user(owner); hid, slug = _hospital(owner)
    with pytest.raises(Forbidden):
        ActorContext.resolve(rw, uid, slug)


def test_platform_operator_accesses_any_hospital(rw, owner):
    uid = _user(owner); grant_platform_operator(owner, uid)
    _, slugA = _hospital(owner); _, slugB = _hospital(owner)
    a = ActorContext.resolve(rw, uid, slugA)
    b = ActorContext.resolve(rw, uid, slugB)
    assert a.access_origin == "platform_operator" and "platform_operator" in a.roles
    assert b.access_origin == "platform_operator" and a.hospital_id != b.hospital_id


def test_inactive_grant_no_access(rw, owner):
    uid = _user(owner); grant_platform_operator(owner, uid)
    _, slug = _hospital(owner)
    ActorContext.resolve(rw, uid, slug)               # 접근되어 멤버십 자동 생성됨
    revoke_platform_operator(owner, uid, "종료")
    with pytest.raises(Forbidden):                    # 철회 즉시 접근 불가(leftover 멤버십 있어도)
        ActorContext.resolve(rw, uid, slug)


# ── 자동 membership ──
def test_auto_membership_idempotent(rw, owner):
    uid = _user(owner); grant_platform_operator(owner, uid)
    hid, slug = _hospital(owner)
    m1 = ActorContext.resolve(rw, uid, slug).membership_id
    m2 = ActorContext.resolve(rw, uid, slug).membership_id
    assert m1 == m2
    with owner.connect() as cn:
        n_mem = cn.execute(text("select count(*) from hospital_memberships where hospital_id=:h and user_id=:u"),
                           {"h": hid, "u": uid}).scalar()
        n_role = cn.execute(text("select count(*) from membership_roles where hospital_id=:h and membership_id=:m "
                                 "and role='platform_operator'"), {"h": hid, "m": m1}).scalar()
    assert n_mem == 1 and n_role == 1


def test_existing_membership_reused_roles_preserved(rw, owner):
    uid = _user(owner); hid, slug = _hospital(owner)
    mid = _member(owner, hid, uid, role="editor")     # 이미 병원이 직접 editor로 등록
    grant_platform_operator(owner, uid)
    ctx = ActorContext.resolve(rw, uid, slug)
    assert ctx.membership_id == str(mid)              # 기존 멤버십 재사용(새로 안 만듦)
    assert "editor" in ctx.roles and "platform_operator" in ctx.roles   # 기존 역할 보존 + platform 추가
    assert ctx.access_origin == "hospital_membership"  # 병원 직접 역할이 있으니 hospital-native


# ── 권한(capability) ──
def _op_ctx(rw, owner):
    uid = _user(owner); grant_platform_operator(owner, uid)
    _, slug = _hospital(owner)
    return ActorContext.resolve(rw, uid, slug)


def test_platform_operator_can_edit_image_evidence(rw, owner):
    ctx = _op_ctx(rw, owner)
    permissions.require(ctx, permissions.EDIT_ROLES)            # 편집 OK
    permissions.require(ctx, permissions.IMAGE_ROLES)          # 이미지 OK
    permissions.require(ctx, permissions.EVIDENCE_REVIEW_ROLES)  # 근거 confirm/reject OK


def test_platform_operator_cannot_approve_or_waive(rw, owner):
    ctx = _op_ctx(rw, owner)
    with pytest.raises(Forbidden):
        permissions.require(ctx, permissions.REVIEW_ROLES)     # 최종 승인·반려·철회 불가
    with pytest.raises(Forbidden):
        permissions.require(ctx, {"admin"})                    # waive/not_applicable(admin) 불가


# ── 철회 시 역할 구분 ──
def test_revoke_keeps_hospital_role(rw, owner):
    uid = _user(owner); hid, slug = _hospital(owner)
    _member(owner, hid, uid, role="editor")           # 병원이 직접 준 editor
    grant_platform_operator(owner, uid)
    ctx1 = ActorContext.resolve(rw, uid, slug)
    assert {"editor", "platform_operator"} <= set(ctx1.roles)
    revoke_platform_operator(owner, uid, "계약종료")
    ctx2 = ActorContext.resolve(rw, uid, slug)        # 여전히 editor라 접근됨
    assert "editor" in ctx2.roles and "platform_operator" not in ctx2.roles   # platform만 무효, 병원역할 유지


# ── 프로비저닝(admin@ourmarketing.com) ──
def test_ensure_platform_admin_user_end_to_end(rw, owner):
    email = "ops" + uuid.uuid4().hex[:8] + "@ourmarketing.com"
    uid = ensure_platform_admin_user(owner, email, "s3cret-pw", name="운영자")
    _, slug = _hospital(owner)
    with owner.connect() as cn:
        assert cn.execute(text("select pw_hash is not null from users where id=:i"), {"i": uid}).scalar()
        assert cn.execute(text("select status from platform_access_grants where user_id=:u"), {"u": uid}).scalar() == "active"
    ctx = ActorContext.resolve(rw, uid, slug)         # 이 유저로 어느 병원이든 접근
    assert ctx.access_origin == "platform_operator" and "platform_operator" in ctx.roles


def test_app_rw_cannot_write_platform_grants(rw, owner):
    # 쓰기는 definer 함수만. app_rw 직접 INSERT 회수(읽기만 허용).
    uid = _user(owner)
    from sqlalchemy import text as _t
    with pytest.raises(Exception):
        with rw.begin() as cn:
            cn.execute(_t("insert into platform_access_grants(user_id) values(:u)"), {"u": uid})
