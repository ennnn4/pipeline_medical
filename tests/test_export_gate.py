"""export gate — current이며 approved인 version만 payload 반환(inv14)."""
import uuid, pytest
from sqlalchemy import text
from store.testkit import new_version, new_block, new_sentence, new_claim
from services.context import ActorContext
from services import exports as ex
from services import approvals as ap
from services.exceptions import VersionConflict, ApprovalPrerequisiteFailed, InvalidStateTransition


def _member(owner, h, role=None):
    mid, uid = uuid.uuid4(), uuid.uuid4()
    with owner.begin() as cn:
        cn.execute(text("insert into users(id,email) values(:u,:e)"), {"u": uid, "e": uid.hex + "@t.c"})
        cn.execute(text("insert into hospital_memberships(id,hospital_id,user_id) values(:m,:h,:u)"), {"m": mid, "h": h, "u": uid})
        if role:
            cn.execute(text("insert into membership_roles(id,hospital_id,membership_id,role) values(:i,:h,:m,:r)"),
                       {"i": uuid.uuid4(), "h": h, "m": mid, "r": role})
    return mid, uid


def _seed(owner, h):
    with owner.begin() as cn:
        sc, v = new_version(cn, h)   # migration → automated verified로 gate 통과
        cn.execute(text("update scripts set current_version_id=:v where id=:s"), {"v": v, "s": sc})
        b = new_block(cn, h, v); s = new_sentence(cn, h, v, b); c = new_claim(cn, h, v, s)
        cn.execute(text("insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,"
                        "support_level,verification_status,medical_risk) values(:i,:h,:c,'automated','a1','direct','verified','low')"),
                   {"i": uuid.uuid4(), "h": h, "c": c})
    return sc, v


def _ctx(h, mid, uid, roles):
    return ActorContext(user_id=str(uid), hospital_id=str(h), membership_id=str(mid), roles=frozenset(roles))


def test_export_blocked_when_not_approved(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v = _seed(owner, h)
    m, u = _member(owner, h, "approver")
    with pytest.raises(ApprovalPrerequisiteFailed):        # 아직 미승인
        ex.prepare_export(rw, _ctx(h, m, u, {"approver"}), sc, v)


def test_export_ok_when_current_approved(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v = _seed(owner, h)
    m, u = _member(owner, h, "approver")
    ap.approve(rw, _ctx(h, m, u, {"approver"}), v)
    payload = ex.prepare_export(rw, _ctx(h, m, u, {"approver"}), sc, v)
    assert payload["version_id"] == str(v) and payload["content_hash"] and payload["blocks"]


def test_export_blocked_when_not_current(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v = _seed(owner, h)
    m, u = _member(owner, h, "approver")
    ap.approve(rw, _ctx(h, m, u, {"approver"}), v)
    with owner.begin() as cn:      # current를 다른 버전으로
        v2 = uuid.uuid4()
        cn.execute(text("insert into script_versions(id,hospital_id,script_id,version_no,source) values(:v,:h,:s,2,'migration')"),
                   {"v": v2, "h": h, "s": sc})
        cn.execute(text("update scripts set current_version_id=:v2 where id=:s"), {"v2": v2, "s": sc})
    with pytest.raises(VersionConflict):                   # approved지만 비-current
        ex.prepare_export(rw, _ctx(h, m, u, {"approver"}), sc, v)


def test_export_blocked_after_revoke(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v = _seed(owner, h)
    m, u = _member(owner, h, "approver")
    adm_m, adm_u = _member(owner, h, "admin")
    ap.approve(rw, _ctx(h, m, u, {"approver"}), v)
    ap.revoke(rw, _ctx(h, adm_m, adm_u, {"admin"}), v, reason="문제 발견")
    with pytest.raises(InvalidStateTransition):            # 철회된 버전 export 불가
        ex.prepare_export(rw, _ctx(h, m, u, {"approver"}), sc, v)
