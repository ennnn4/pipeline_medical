"""승인 — app_rw + fn_approve_version. 승인자=세션 membership(위조 불가), 게이트, stale."""
import uuid, pytest
from sqlalchemy import text
from store.testkit import sqlstate, new_version, new_block, new_sentence, new_claim
from store.repositories import tenant_conn, approve_version, is_stale

def _grant(owner, h, m, role):
    with owner.begin() as cn:
        cn.execute(text("insert into membership_roles(id,hospital_id,membership_id,role) values(:i,:h,:m,:r)"),
                   {"i": uuid.uuid4(), "h": h, "m": m, "r": role})

def _membership(owner, h):
    u, m = uuid.uuid4(), uuid.uuid4()
    with owner.begin() as cn:
        cn.execute(text("insert into users(id,email) values(:u,:e)"), {"u": u, "e": u.hex + "@x.c"})
        cn.execute(text("insert into hospital_memberships(id,hospital_id,user_id) values(:m,:h,:u)"), {"m": m, "h": h, "u": u})
    return m

def _version_with_claim(owner, h, support="direct", kind="automated", actor=None, key="a:1"):
    with owner.begin() as cn:
        sc, v = new_version(cn, h)
        cn.execute(text("update scripts set current_version_id=:v where id=:s"), {"v": v, "s": sc})  # 승인 대상=current
        b = new_block(cn, h, v); s = new_sentence(cn, h, v, b); c = new_claim(cn, h, v, s)
        cn.execute(text("insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,"
                        "support_level,verification_status,medical_risk,created_by_membership_id) "
                        "values(:i,:h,:c,:k,:key,:sl,'verified','low',:a)"),
                   {"i": uuid.uuid4(), "h": h, "c": c, "k": kind, "key": key, "sl": support, "a": actor})
    return sc, v, b, c

def test_approve_via_app_rw_then_valid(owner, rw, tenant):
    h, m = tenant["hospital_id"], tenant["membership_id"]
    _, v, b, c = _version_with_claim(owner, h); _grant(owner, h, m, "approver")
    with tenant_conn(rw, h, m) as cn:                      # 세션 membership=승인자
        approve_version(cn, h, v, "policy-1")
    with tenant_conn(rw, h, m) as cn:
        assert is_stale(cn, h, v, "policy-1") is False

def test_approve_requires_approver_role(owner, rw, tenant):
    h, m = tenant["hospital_id"], tenant["membership_id"]   # 역할 미부여
    _, v, b, c = _version_with_claim(owner, h)
    with pytest.raises(Exception) as ei:
        with tenant_conn(rw, h, m) as cn:
            approve_version(cn, h, v, "policy-1")
    assert sqlstate(ei.value) == "42501"

def test_approver_impersonation_blocked(owner, rw, tenant):
    """editor 세션은 approver 신원을 위조해 승인할 수 없다(함수가 세션 membership만 신뢰)."""
    h = tenant["hospital_id"]
    approver = _membership(owner, h); _grant(owner, h, approver, "approver")
    editor = _membership(owner, h); _grant(owner, h, editor, "editor")
    _, v, b, c = _version_with_claim(owner, h)
    with pytest.raises(Exception) as ei:
        with tenant_conn(rw, h, editor) as cn:             # editor 세션 → approver id 넘길 방법 없음
            approve_version(cn, h, v, "policy-1")
    assert sqlstate(ei.value) == "42501"

def test_archived_membership_cannot_approve(owner, rw, tenant):
    """퇴사/archived된 membership은 role이 남아도 승인 불가(active 검사)."""
    h, m = tenant["hospital_id"], tenant["membership_id"]
    _, v, b, c = _version_with_claim(owner, h); _grant(owner, h, m, "approver")
    with owner.begin() as cn:
        cn.execute(text("update hospital_memberships set archived_at=now() where id=:m"), {"m": m})
    with pytest.raises(Exception) as ei:
        with tenant_conn(rw, h, m) as cn:
            approve_version(cn, h, v, "policy-1")
    assert sqlstate(ei.value) == "42501"

def test_approve_blocks_unverified_claim(owner, rw, tenant):
    h, m = tenant["hospital_id"], tenant["membership_id"]
    _, v, b, c = _version_with_claim(owner, h, support="unverified", kind="migration"); _grant(owner, h, m, "approver")
    with pytest.raises(Exception) as ei:
        with tenant_conn(rw, h, m) as cn:
            approve_version(cn, h, v, "policy-1")
    assert sqlstate(ei.value) == "23514"

def test_approved_missing_fields_23514(owner, tenant):
    h = tenant["hospital_id"]
    with owner.begin() as cn:
        _, v = new_version(cn, h)
    with pytest.raises(Exception) as ei:
        with owner.begin() as cn:
            cn.execute(text("update version_approval_states set status='approved' where hospital_id=:h and version_id=:v"),
                       {"h": h, "v": v})
    assert sqlstate(ei.value) == "23514"

def test_content_tamper_stale(owner, rw, tenant):
    h, m = tenant["hospital_id"], tenant["membership_id"]
    _, v, b, c = _version_with_claim(owner, h); _grant(owner, h, m, "approver")
    with tenant_conn(rw, h, m) as cn:
        approve_version(cn, h, v, "policy-1")
    with owner.begin() as cn:
        cn.execute(text("update script_blocks set text='TAMPERED' where id=:b"), {"b": b})
    with tenant_conn(rw, h, m) as cn:
        assert is_stale(cn, h, v, "policy-1") is True

def test_policy_change_stale(owner, rw, tenant):
    h, m = tenant["hospital_id"], tenant["membership_id"]
    _, v, b, c = _version_with_claim(owner, h); _grant(owner, h, m, "approver")
    with tenant_conn(rw, h, m) as cn:
        approve_version(cn, h, v, "policy-1")
    with tenant_conn(rw, h, m) as cn:
        assert is_stale(cn, h, v, "policy-2") is True

def test_approved_version_assessment_frozen(owner, rw, tenant):
    """Step3: 승인된 version의 근거(claim_assessments) 변경은 DB 트리거가 차단(inv13 동결)."""
    h, m = tenant["hospital_id"], tenant["membership_id"]
    _, v, b, c = _version_with_claim(owner, h); _grant(owner, h, m, "approver")
    with tenant_conn(rw, h, m) as cn:
        approve_version(cn, h, v, "policy-1")
    with pytest.raises(Exception) as ei:            # 승인 후 assessment INSERT → 동결(2BP01)
        with owner.begin() as cn:
            cn.execute(text("insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,"
                            "support_level,verification_status,medical_risk,created_by_membership_id) "
                            "values(:i,:h,:c,'human_review','hr:1','partial','verified','high',:m)"),
                       {"i": uuid.uuid4(), "h": h, "c": c, "m": m})
    assert sqlstate(ei.value) == "P2013"

def test_cross_hospital_approval_blocked(owner, rw, tenant):
    """A 세션이 B의 version을 승인 시도 → 차단(복합 WHERE not-found)."""
    hA, mA = tenant["hospital_id"], tenant["membership_id"]; _grant(owner, hA, mA, "approver")
    hB = uuid.uuid4()
    with owner.begin() as cn:
        cn.execute(text("insert into hospitals(id,slug,name) values(:h,:s,'B')"), {"h": hB, "s": "b" + hB.hex[:8]})
        _, vB = new_version(cn, hB)
    with pytest.raises(Exception):
        with tenant_conn(rw, hA, mA) as cn:
            approve_version(cn, hA, vB, "policy-1")
