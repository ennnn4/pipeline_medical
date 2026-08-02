"""Step 4 approval service — 작성자≠승인자·자기승인 override·reject·revoke·current·상태전이."""
import uuid, pytest
from sqlalchemy import text
from store.testkit import new_version, new_block, new_sentence, new_claim
from store.repositories import tenant_conn
from services.context import ActorContext
from services import approvals as ap
from services.exceptions import Forbidden, VersionConflict, InvalidStateTransition


def _member(owner, h, role=None):
    mid, uid = uuid.uuid4(), uuid.uuid4()
    with owner.begin() as cn:
        cn.execute(text("insert into users(id,email) values(:u,:e)"), {"u": uid, "e": uid.hex + "@t.c"})
        cn.execute(text("insert into hospital_memberships(id,hospital_id,user_id) values(:m,:h,:u)"), {"m": mid, "h": h, "u": uid})
        if role:
            cn.execute(text("insert into membership_roles(id,hospital_id,membership_id,role) values(:i,:h,:m,:r)"),
                       {"i": uuid.uuid4(), "h": h, "m": mid, "r": role})
    return mid, uid


def _approvable(owner, h, author_mid=None, verified=True):
    """current version(editor면 작성자 지정) + gate 통과용 사람 accepted 판정(비-migration도 승인 가능)."""
    with owner.begin() as cn:
        sc, v = new_version(cn, h, source="editor" if author_mid else "migration")
        if author_mid:
            cn.execute(text("update script_versions set created_by_membership_id=:m where id=:v"), {"m": author_mid, "v": v})
        cn.execute(text("update scripts set current_version_id=:v where id=:s"), {"v": v, "s": sc})
        b = new_block(cn, h, v); s = new_sentence(cn, h, v, b); c = new_claim(cn, h, v, s)
        rev, revu = uuid.uuid4(), uuid.uuid4()   # 판정 남길 리뷰어(human_review는 actor 필수)
        cn.execute(text("insert into users(id,email) values(:u,:e)"), {"u": revu, "e": revu.hex + "@t.c"})
        cn.execute(text("insert into hospital_memberships(id,hospital_id,user_id) values(:m,:h,:u)"), {"m": rev, "h": h, "u": revu})
        if verified:
            cn.execute(text("insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,"
                            "support_level,verification_status,medical_risk,human_decision,decision_reason,created_by_membership_id) "
                            "values(:i,:h,:c,'human_review','hr',:sl,'verified','low','accepted',:dr,:by)"),
                       {"i": uuid.uuid4(), "h": h, "c": c, "sl": "direct", "dr": "확정", "by": rev})
        else:
            cn.execute(text("insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,"
                            "support_level,verification_status,medical_risk) values(:i,:h,:c,'automated','a1','unverified','pending','low')"),
                       {"i": uuid.uuid4(), "h": h, "c": c})
    return sc, v, c


def _ctx(h, mid, uid, roles):
    return ActorContext(user_id=str(uid), hospital_id=str(h), membership_id=str(mid), roles=frozenset(roles))


def _status(owner, v):
    with owner.connect() as cn:
        return cn.execute(text("select status from version_approval_states where version_id=:v"), {"v": v}).scalar()


def test_approve_by_other_approver(rw, owner, tenant):
    h = tenant["hospital_id"]
    author, _ = _member(owner, h)
    appr_m, appr_u = _member(owner, h, "approver")
    _, v, _ = _approvable(owner, h, author_mid=author)
    ap.approve(rw, _ctx(h, appr_m, appr_u, {"approver"}), v)
    assert _status(owner, v) == "approved"


def test_author_cannot_approve_own(rw, owner, tenant):
    h = tenant["hospital_id"]
    author_m, author_u = _member(owner, h, "approver")
    _, v, _ = _approvable(owner, h, author_mid=author_m)
    with pytest.raises(Forbidden):                        # 작성자==승인자 → 42501
        ap.approve(rw, _ctx(h, author_m, author_u, {"approver"}), v)


def test_self_approve_denied_without_flag(rw, owner, tenant):
    h = tenant["hospital_id"]
    author_m, author_u = _member(owner, h, "admin")
    _, v, _ = _approvable(owner, h, author_mid=author_m)
    with pytest.raises(Forbidden):                        # allow_self_approval=false(기본)
        ap.self_approve(rw, _ctx(h, author_m, author_u, {"admin"}), v, reason="긴급")


def test_self_approve_allowed_with_flag_admin_reason(rw, owner, tenant):
    h = tenant["hospital_id"]
    author_m, author_u = _member(owner, h, "admin")
    _, v, _ = _approvable(owner, h, author_mid=author_m)
    with owner.begin() as cn:
        cn.execute(text("update hospitals set allow_self_approval=true where id=:h"), {"h": h})
    ap.self_approve(rw, _ctx(h, author_m, author_u, {"admin"}), v, reason="원장 단독 운영 예외")
    assert _status(owner, v) == "approved"


def test_approve_non_current_blocked(rw, owner, tenant):
    h = tenant["hospital_id"]
    appr_m, appr_u = _member(owner, h, "approver")
    sc, v, _ = _approvable(owner, h)
    with owner.begin() as cn:      # current를 다른 버전으로 이동 → v는 비-current
        v2 = uuid.uuid4()
        cn.execute(text("insert into script_versions(id,hospital_id,script_id,version_no,source) values(:v,:h,:s,2,'migration')"),
                   {"v": v2, "h": h, "s": sc})
        cn.execute(text("update scripts set current_version_id=:v2 where id=:s"), {"v2": v2, "s": sc})
    with pytest.raises(VersionConflict):                  # P2015
        ap.approve(rw, _ctx(h, appr_m, appr_u, {"approver"}), v)


def test_reject_then_not_approvable(rw, owner, tenant):
    h = tenant["hospital_id"]
    appr_m, appr_u = _member(owner, h, "approver")
    _, v, _ = _approvable(owner, h)
    ctx = _ctx(h, appr_m, appr_u, {"approver"})
    ap.reject(rw, ctx, v, reason="근거 불충분")
    assert _status(owner, v) == "rejected"
    with pytest.raises(InvalidStateTransition):           # rejected는 재승인 불가(none/pending 아님)
        ap.approve(rw, ctx, v)


def test_revoke_requires_admin_and_approved(rw, owner, tenant):
    h = tenant["hospital_id"]
    appr_m, appr_u = _member(owner, h, "approver")
    admin_m, admin_u = _member(owner, h, "admin")
    _, v, _ = _approvable(owner, h)
    ap.approve(rw, _ctx(h, appr_m, appr_u, {"approver"}), v)
    with pytest.raises(Forbidden):                        # approver는 revoke 불가
        ap.revoke(rw, _ctx(h, appr_m, appr_u, {"approver"}), v, reason="철회")
    ap.revoke(rw, _ctx(h, admin_m, admin_u, {"admin"}), v, reason="중대 오류 발견")
    assert _status(owner, v) == "revoked"


def test_reject_requires_reason(rw, owner, tenant):
    h = tenant["hospital_id"]
    appr_m, appr_u = _member(owner, h, "approver")
    _, v, _ = _approvable(owner, h)
    with pytest.raises(InvalidStateTransition):
        ap.reject(rw, _ctx(h, appr_m, appr_u, {"approver"}), v, reason="   ")


def test_new_editor_version_requires_human_signoff(rw, owner, tenant):
    """비-migration(editor) version은 automated verified만으론 승인 불가 — 사람 판정 필수(legacy 제한)."""
    from services.exceptions import ApprovalPrerequisiteFailed
    h = tenant["hospital_id"]
    author, _ = _member(owner, h)
    appr_m, appr_u = _member(owner, h, "approver")
    with owner.begin() as cn:
        sc, v = new_version(cn, h, source="editor")
        cn.execute(text("update script_versions set created_by_membership_id=:m where id=:v"), {"m": author, "v": v})
        cn.execute(text("update scripts set current_version_id=:v where id=:s"), {"v": v, "s": sc})
        b = new_block(cn, h, v); s = new_sentence(cn, h, v, b); c = new_claim(cn, h, v, s)
        cn.execute(text("insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,"
                        "support_level,verification_status,medical_risk) values(:i,:h,:c,'automated','a1','direct','verified','low')"),
                   {"i": uuid.uuid4(), "h": h, "c": c})
    with pytest.raises(ApprovalPrerequisiteFailed):
        ap.approve(rw, _ctx(h, appr_m, appr_u, {"approver"}), v)


def test_revoke_records_actor_and_reason(rw, owner, tenant):
    h = tenant["hospital_id"]
    appr_m, appr_u = _member(owner, h, "approver")
    admin_m, admin_u = _member(owner, h, "admin")
    _, v, _ = _approvable(owner, h)
    ap.approve(rw, _ctx(h, appr_m, appr_u, {"approver"}), v)
    ap.revoke(rw, _ctx(h, admin_m, admin_u, {"admin"}), v, reason="환자정보 노출 발견")
    with owner.connect() as cn:
        r = cn.execute(text("select status, approver_membership_id, revoked_by_membership_id, revoke_reason "
                            "from version_approval_states where version_id=:v"), {"v": v}).first()
    assert r.status == "revoked" and str(r.approver_membership_id) == str(appr_m)   # 원 승인자 유지
    assert str(r.revoked_by_membership_id) == str(admin_m) and r.revoke_reason == "환자정보 노출 발견"
