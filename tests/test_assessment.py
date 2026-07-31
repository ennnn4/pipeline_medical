"""claim_effective_assessment — 사람 판정 우선, migration 제외, tie-break, idempotency."""
import uuid, pytest
from sqlalchemy import text
from store.testkit import sqlstate, new_version, new_block, new_sentence, new_claim

def _claim(owner, h):
    with owner.begin() as cn:
        _, v = new_version(cn, h)
        b = new_block(cn, h, v); s = new_sentence(cn, h, v, b); c = new_claim(cn, h, v, s)
    return c

def _add(owner, h, c, kind, support, key, risk="medium", actor=None, created=None):
    with owner.begin() as cn:
        cn.execute(text(
            "insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,support_level,"
            "verification_status,medical_risk,created_by_membership_id,created_at) "
            f"values(:i,:h,:c,:k,:key,:sl,'verified',:r,:a,{created or 'now()'})"),
            {"i": uuid.uuid4(), "h": h, "c": c, "k": kind, "key": key, "sl": support, "r": risk, "a": actor})

def _eff(owner, c):
    with owner.connect() as cn:
        return cn.execute(text("select support_level, assessment_kind from claim_effective_assessment where claim_id=:c"),
                          {"c": c}).first()

def test_migration_only_no_effective(owner, tenant):
    c = _claim(owner, tenant["hospital_id"])
    _add(owner, tenant["hospital_id"], c, "migration", "unverified", "m:1")
    assert _eff(owner, c) is None                    # migration은 승인근거 아님

def test_automated_only(owner, tenant):
    h = tenant["hospital_id"]; c = _claim(owner, h)
    _add(owner, h, c, "automated", "partial", "a:1")
    assert tuple(_eff(owner, c)) == ("partial", "automated")

def test_human_beats_newer_automated(owner, tenant):
    h = tenant["hospital_id"]; c = _claim(owner, h); m = tenant["membership_id"]
    _add(owner, h, c, "human_review", "direct", "h:1", actor=m, created="now()-interval '1 hour'")
    _add(owner, h, c, "automated", "unsupported", "a:1", created="now()")   # 더 최신
    assert tuple(_eff(owner, c)) == ("direct", "human_review")

def test_override_beats_human(owner, tenant):
    h = tenant["hospital_id"]; c = _claim(owner, h); m = tenant["membership_id"]
    _add(owner, h, c, "human_review", "partial", "h:1", actor=m)
    _add(owner, h, c, "override", "direct", "o:1", actor=m)
    assert tuple(_eff(owner, c)) == ("direct", "override")

def test_latest_human_supersedes_old_human(owner, tenant):
    h = tenant["hospital_id"]; c = _claim(owner, h); m = tenant["membership_id"]
    _add(owner, h, c, "human_review", "partial", "h:1", actor=m, created="now()-interval '2 hour'")
    _add(owner, h, c, "human_review", "direct", "h:2", actor=m, created="now()")
    assert tuple(_eff(owner, c)) == ("direct", "human_review")   # 최신 human

def test_idempotency_duplicate_blocked(owner, tenant):
    h = tenant["hospital_id"]; c = _claim(owner, h)
    _add(owner, h, c, "automated", "partial", "dup:1")
    with pytest.raises(Exception) as ei:
        _add(owner, h, c, "automated", "direct", "dup:1")       # 같은 idempotency_key
    assert sqlstate(ei.value) == "23505"
