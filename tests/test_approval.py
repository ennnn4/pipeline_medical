"""승인 — app_rw 경로(fn_approve_version). 역할검사·미검증 게이트·stale."""
import uuid, pytest
from sqlalchemy import text
from store.testkit import sqlstate, new_version, new_block, new_sentence, new_claim
from store.repositories import tenant_conn, approve_version, is_stale

def _grant_approver(owner, h, m):
    with owner.begin() as cn:
        cn.execute(text("insert into membership_roles(id,hospital_id,membership_id,role) values(:i,:h,:m,'approver')"),
                   {"i": uuid.uuid4(), "h": h, "m": m})

def _version_with_claim(owner, h, support="direct", kind="automated", actor=None, key="a:1"):
    with owner.begin() as cn:
        sc, v = new_version(cn, h)
        b = new_block(cn, h, v); s = new_sentence(cn, h, v, b); c = new_claim(cn, h, v, s)
        cn.execute(text("insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,"
                        "support_level,verification_status,medical_risk,created_by_membership_id) "
                        "values(:i,:h,:c,:k,:key,:sl,'verified','low',:a)"),
                   {"i": uuid.uuid4(), "h": h, "c": c, "k": kind, "key": key, "sl": support, "a": actor})
    return sc, v, b, c

def test_approve_via_app_rw_then_valid(owner, rw, tenant):
    h, m = tenant["hospital_id"], tenant["membership_id"]
    _, v, b, c = _version_with_claim(owner, h)
    _grant_approver(owner, h, m)
    with tenant_conn(rw, h) as cn:
        approve_version(cn, h, v, m, "policy-1")           # app_rw + RLS 경로
    with tenant_conn(rw, h) as cn:
        assert is_stale(cn, h, v, "policy-1") is False

def test_approve_requires_approver_role(owner, rw, tenant):
    h, m = tenant["hospital_id"], tenant["membership_id"]
    _, v, b, c = _version_with_claim(owner, h)             # 역할 미부여
    with pytest.raises(Exception) as ei:
        with tenant_conn(rw, h) as cn:
            approve_version(cn, h, v, m, "policy-1")
    assert sqlstate(ei.value) == "42501"

def test_approve_blocks_unverified_claim(owner, rw, tenant):
    h, m = tenant["hospital_id"], tenant["membership_id"]
    _, v, b, c = _version_with_claim(owner, h, support="unverified", kind="migration")  # 미검증
    _grant_approver(owner, h, m)
    with pytest.raises(Exception) as ei:
        with tenant_conn(rw, h) as cn:
            approve_version(cn, h, v, m, "policy-1")
    assert sqlstate(ei.value) == "23514"                   # 게이트 차단

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
    _, v, b, c = _version_with_claim(owner, h); _grant_approver(owner, h, m)
    with tenant_conn(rw, h) as cn:
        approve_version(cn, h, v, m, "policy-1")
    with owner.begin() as cn:                              # owner만 불변 변조 가능(app_rw는 봉쇄)
        cn.execute(text("update script_blocks set text='TAMPERED' where id=:b"), {"b": b})
    with tenant_conn(rw, h) as cn:
        assert is_stale(cn, h, v, "policy-1") is True

def test_policy_change_stale(owner, rw, tenant):
    h, m = tenant["hospital_id"], tenant["membership_id"]
    _, v, b, c = _version_with_claim(owner, h); _grant_approver(owner, h, m)
    with tenant_conn(rw, h) as cn:
        approve_version(cn, h, v, m, "policy-1")
    with tenant_conn(rw, h) as cn:
        assert is_stale(cn, h, v, "policy-2") is True

def test_new_assessment_changes_effective_stale(owner, rw, tenant):
    h, m = tenant["hospital_id"], tenant["membership_id"]
    _, v, b, c = _version_with_claim(owner, h, support="direct", kind="automated"); _grant_approver(owner, h, m)
    with tenant_conn(rw, h) as cn:
        approve_version(cn, h, v, m, "policy-1")
    with owner.begin() as cn:
        cn.execute(text("insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,"
                        "support_level,verification_status,medical_risk,created_by_membership_id) "
                        "values(:i,:h,:c,'human_review','hr:1','partial','verified','high',:m)"),
                   {"i": uuid.uuid4(), "h": h, "c": c, "m": m})
    with tenant_conn(rw, h) as cn:
        assert is_stale(cn, h, v, "policy-1") is True
