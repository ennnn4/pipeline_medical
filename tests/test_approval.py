"""승인 무결성·approval_stale·audit 동일 TX 롤백."""
import uuid, pytest
from sqlalchemy import text
from store.testkit import sqlstate, new_version, new_block, new_sentence, new_claim
from store import repositories as repo

def _setup(owner, h, support="direct", kind="automated", actor=None, key="a:1"):
    with owner.begin() as cn:
        sc, v = new_version(cn, h)
        b = new_block(cn, h, v); s = new_sentence(cn, h, v, b); c = new_claim(cn, h, v, s)
        cn.execute(text("insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,"
                        "support_level,verification_status,medical_risk,created_by_membership_id) "
                        "values(:i,:h,:c,:k,:key,:sl,'verified','low',:a)"),
                   {"i": uuid.uuid4(), "h": h, "c": c, "k": kind, "key": key, "sl": support, "a": actor})
    return sc, v, b, c

def test_approved_missing_fields_23514(owner, tenant):
    h = tenant["hospital_id"]
    with owner.begin() as cn:
        _, v = new_version(cn, h)
    with pytest.raises(Exception) as ei:
        with owner.begin() as cn:
            cn.execute(text("update version_approval_states set status='approved' where hospital_id=:h and version_id=:v"),
                       {"h": h, "v": v})
    assert sqlstate(ei.value) == "23514"

def test_approve_then_valid(owner, tenant):
    h = tenant["hospital_id"]; _, v, b, c = _setup(owner, h)
    repo.approve_version(owner, h, v, tenant["membership_id"], "policy-1")
    assert repo.is_stale(owner, h, v, "policy-1") is False

def test_content_tamper_stale(owner, tenant):
    h = tenant["hospital_id"]; _, v, b, c = _setup(owner, h)
    repo.approve_version(owner, h, v, tenant["membership_id"], "policy-1")
    with owner.begin() as cn:
        cn.execute(text("update script_blocks set text='TAMPERED' where id=:b"), {"b": b})
    assert repo.is_stale(owner, h, v, "policy-1") is True

def test_policy_change_stale(owner, tenant):
    h = tenant["hospital_id"]; _, v, b, c = _setup(owner, h)
    repo.approve_version(owner, h, v, tenant["membership_id"], "policy-1")
    assert repo.is_stale(owner, h, v, "policy-2") is True

def test_new_assessment_changes_effective_stale(owner, tenant):
    h = tenant["hospital_id"]; m = tenant["membership_id"]
    _, v, b, c = _setup(owner, h, support="direct", kind="automated")
    repo.approve_version(owner, h, v, m, "policy-1")
    with owner.begin() as cn:   # human_review가 automated 이겨 effective 바뀜 → stale
        cn.execute(text("insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,"
                        "support_level,verification_status,medical_risk,created_by_membership_id) "
                        "values(:i,:h,:c,'human_review','hr:1','partial','verified','high',:m)"),
                   {"i": uuid.uuid4(), "h": h, "c": c, "m": m})
    assert repo.is_stale(owner, h, v, "policy-1") is True

def test_new_assessment_effective_unchanged_not_stale(owner, tenant):
    h = tenant["hospital_id"]; m = tenant["membership_id"]
    _, v, b, c = _setup(owner, h, support="direct", kind="human_review", actor=m, key="hr:0")
    repo.approve_version(owner, h, v, m, "policy-1")
    with owner.begin() as cn:   # 더 최신 automated지만 human이 이겨 effective 불변 → not stale
        cn.execute(text("insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,"
                        "support_level,verification_status,medical_risk) "
                        "values(:i,:h,:c,'automated','au:1','unsupported','verified','high')"),
                   {"i": uuid.uuid4(), "h": h, "c": c})
    assert repo.is_stale(owner, h, v, "policy-1") is False

def test_audit_failure_rolls_back_approval(owner, tenant):
    h = tenant["hospital_id"]; _, v, b, c = _setup(owner, h)
    with pytest.raises(Exception):
        repo.approve_version(owner, h, v, tenant["membership_id"], "policy-1", _audit_fail=True)
    with owner.connect() as cn:
        st = cn.execute(text("select status from version_approval_states where hospital_id=:h and version_id=:v"),
                        {"h": h, "v": v}).scalar()
    assert st == "none"     # 승인 UPDATE도 audit과 함께 롤백
