"""platform operator(대행사 전 병원 접근) — GPT 승인 설계 검증.

접근: 일반 병원 격리 유지 + platform grant 있는 유저만 멤버십 게이트 우회(자동 프로비저닝).
권한: 편집·이미지·근거 accepted/rejected·export는 되고 최종승인·자기승인·철회·waive/na는 불가.
철회: grant revoke 즉시 접근 불가, 병원이 직접 준 역할은 유지."""
import uuid, pytest
from sqlalchemy import text
from services.context import ActorContext
from services.exceptions import Forbidden
from services import permissions
from store.repositories import tenant_conn
from store.platform_ops import (grant_platform_operator, revoke_platform_operator,
                                ensure_platform_admin_user, seed_platform_operator)


def _version_with_claim(owner, hid):
    """편집 가능한(status none) version + claim 1개 생성 → (version_id, claim_id)."""
    sc, v, b, s, c = (uuid.uuid4() for _ in range(5))
    with owner.begin() as cn:
        cn.execute(text("insert into scripts(id,hospital_id,topic) values(:s,:h,'t')"), {"s": sc, "h": hid})
        cn.execute(text("insert into script_versions(id,hospital_id,script_id,version_no,source) "
                        "values(:v,:h,:s,1,'migration')"), {"v": v, "h": hid, "s": sc})
        cn.execute(text("insert into version_approval_states(id,hospital_id,version_id,status) "
                        "values(:i,:h,:v,'none')"), {"i": uuid.uuid4(), "h": hid, "v": v})
        cn.execute(text("insert into script_blocks(id,hospital_id,version_id,stable_block_key,order_index,block_type,text) "
                        "values(:b,:h,:v,'blk_1',0,'explanation','원문')"), {"b": b, "h": hid, "v": v})
        cn.execute(text("insert into script_sentences(id,hospital_id,version_id,block_id,sentence_index,text,"
                        "start_offset,end_offset,offset_unit,segmenter_version) "
                        "values(:s,:h,:v,:b,0,'원문',0,2,'codepoint','v1')"), {"s": s, "h": hid, "v": v, "b": b})
        cn.execute(text("insert into claims(id,hospital_id,version_id,sentence_id,claim_index,claim_text,claim_type,detection_method) "
                        "values(:c,:h,:v,:s,0,'원문','statistic','migration')"), {"c": c, "h": hid, "v": v, "s": s})
        cn.execute(text("update scripts set current_version_id=:v where id=:s"), {"v": v, "s": sc})
    return v, c


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


# ── GPT 머지 전 필수 4건 ──
def _add_assessment(rw, hid, op_mid, uid, cid, decision, verif, reason=None):
    with tenant_conn(rw, hid, membership_id=op_mid, user_id=uid) as cn:
        return cn.execute(text("select public.fn_add_human_assessment(:h,:c,:sup,:vf,'low',:d,:r)"),
                          {"h": hid, "c": cid, "sup": "direct" if verif == "verified" else "unverified",
                           "vf": verif, "d": decision, "r": reason}).scalar()


def test_db_backstop_rejects_after_revoke(rw, owner):
    # (1) resolve 성공 후 grant 철회 → 같은 요청경로의 DB 함수가 실행시점에 재확인해 거부(경쟁 방어).
    uid = _user(owner); grant_platform_operator(owner, uid); hid, slug = _hospital(owner)
    vid, cid = _version_with_claim(owner, hid)
    op_mid = ActorContext.resolve(rw, uid, slug).membership_id
    _add_assessment(rw, hid, op_mid, uid, cid, "accepted", "verified")   # 활성 grant → accepted OK
    revoke_platform_operator(owner, uid, "race")
    with pytest.raises(Exception):                                        # 철회 후 DB 함수가 거부
        _add_assessment(rw, hid, op_mid, uid, cid, "accepted", "verified")


def test_revoke_then_regrant_single_valid_role(rw, owner):
    # (2) grant A→revoke→grant B→접근: 유효 platform role 정확히 1개, B에 연결, membership 중복 없음.
    uid = _user(owner); hid, slug = _hospital(owner)
    gA = grant_platform_operator(owner, uid); ActorContext.resolve(rw, uid, slug)
    revoke_platform_operator(owner, uid, "A끝")
    gB = grant_platform_operator(owner, uid); ctx = ActorContext.resolve(rw, uid, slug)
    assert str(gA) != str(gB) and "platform_operator" in ctx.roles
    with owner.connect() as cn:
        rows = cn.execute(text("select platform_grant_id, revoked_at from membership_roles "
                               "where hospital_id=:h and membership_id=:m and role='platform_operator'"),
                          {"h": hid, "m": ctx.membership_id}).all()
        n_mem = cn.execute(text("select count(*) from hospital_memberships where hospital_id=:h and user_id=:u"),
                           {"h": hid, "u": uid}).scalar()
    assert len(rows) == 1 and str(rows[0][0]) == str(gB) and rows[0][1] is None   # B 연결·활성
    assert n_mem == 1


def test_revoke_blocks_other_hospital_keeps_native(rw, owner):
    # (3) 병원 직접 editor(A) + platform(B). 철회 → A는 editor로 유지, B는 접근 불가.
    uid = _user(owner)
    hidA, slugA = _hospital(owner); _member(owner, hidA, uid, role="editor")
    hidB, slugB = _hospital(owner)
    grant_platform_operator(owner, uid)
    ActorContext.resolve(rw, uid, slugA); ActorContext.resolve(rw, uid, slugB)
    revoke_platform_operator(owner, uid, "end")
    a = ActorContext.resolve(rw, uid, slugA)
    assert "editor" in a.roles and "platform_operator" not in a.roles
    with pytest.raises(Forbidden):
        ActorContext.resolve(rw, uid, slugB)


def test_db_boundary_platform_operator_capabilities(rw, owner):
    # (4) DB 직접 호출 관점 경계: accepted OK, waived는 admin 전용이라 거부(rejected는 사유필수 별개).
    uid = _user(owner); grant_platform_operator(owner, uid); hid, slug = _hospital(owner)
    vid, cid = _version_with_claim(owner, hid)
    op_mid = ActorContext.resolve(rw, uid, slug).membership_id
    _add_assessment(rw, hid, op_mid, uid, cid, "accepted", "verified")        # 허용
    with pytest.raises(Exception):
        _add_assessment(rw, hid, op_mid, uid, cid, "waived", "failed", "사유")  # admin 전용 → 거부


# ── seed 안전성(GPT: 철회된 grant를 배포가 되살리면 안 됨) ──
def test_seed_does_not_reactivate_revoked(owner):
    email = "seed" + uuid.uuid4().hex[:8] + "@ourmarketing.com"
    uid, created = seed_platform_operator(owner, email, "pw1")
    assert created is True
    revoke_platform_operator(owner, uid, "운영자 철회")
    uid2, created2 = seed_platform_operator(owner, email, "pw2")   # 재배포 시뮬
    assert str(uid2) == str(uid) and created2 is False            # 기존 유지·무변경
    with owner.connect() as cn:
        active = cn.execute(text("select count(*) from platform_access_grants "
                                 "where user_id=:u and status='active'"), {"u": uid}).scalar()
    assert active == 0                                            # 철회 유지(자동 재활성화 안 함)
