"""evidence service — 명시 version_id·current 재검사·approved 동결·소속검증·권한."""
import uuid, pytest
from sqlalchemy import text
from store.testkit import new_version, new_block, new_sentence, new_claim
from store.repositories import tenant_conn, approve_version
from services.context import ActorContext
from services import evidence as ev
from services.exceptions import Forbidden, VersionConflict, InvalidStateTransition, NotFound


def _seed_claim(owner, h):
    """current version + 블록·문장·claim 1개(미검증) 구성. 편집 시 재분할되도록 실제 텍스트."""
    with owner.begin() as cn:
        sc, v = new_version(cn, h)
        cn.execute(text("update scripts set current_version_id=:v where id=:s"), {"v": v, "s": sc})
        b = new_block(cn, h, v, 0, key="blk_1")
        cn.execute(text("update script_blocks set text='이명장애척도가 54점에서 2점으로 개선됐습니다.' where id=:b"), {"b": b})
        s = new_sentence(cn, h, v, b, 0)
        cn.execute(text("update script_sentences set text='이명장애척도가 54점에서 2점으로 개선됐습니다.' where id=:s"), {"s": s})
        cid = new_claim(cn, h, v, s, 0)
    return sc, v, cid


def _grant(owner, h, m, role):
    with owner.begin() as cn:
        exists = cn.execute(text("select 1 from membership_roles where hospital_id=:h and membership_id=:m and role=:r"),
                            {"h": h, "m": m, "r": role}).scalar()
        if not exists:
            cn.execute(text("insert into membership_roles(id,hospital_id,membership_id,role) values(:i,:h,:m,:r)"),
                       {"i": uuid.uuid4(), "h": h, "m": m, "r": role})


def _ctx(owner, tenant, roles):
    # DB membership_roles에도 부여(전용 definer 함수가 실제 역할을 검증하므로 ctx.roles와 일치)
    for r in roles:
        _grant(owner, tenant["hospital_id"], tenant["membership_id"], r)
    return ActorContext(user_id=str(tenant["user_id"]), hospital_id=str(tenant["hospital_id"]),
                        membership_id=str(tenant["membership_id"]), roles=frozenset(roles))


def test_assess_requires_review_role(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v, cid = _seed_claim(owner, h)
    ctx = _ctx(owner, tenant, {"editor"})                       # editor는 검수 권한 없음
    with pytest.raises(Forbidden):
        ev.assess_claim(rw, ctx, sc, v, cid, "confirm")


def test_assess_confirm_appends_human_review(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v, cid = _seed_claim(owner, h)
    ctx = _ctx(owner, tenant, {"approver"})
    ev.assess_claim(rw, ctx, sc, v, cid, "confirm")
    with owner.connect() as cn:
        n = cn.execute(text("select count(*) from claim_assessments where claim_id=:c and assessment_kind='human_review'"),
                       {"c": cid}).scalar()
    assert n == 1


def test_assess_rejects_stale_version(rw, owner, tenant):
    """current가 바뀐 뒤 과거 version_id로 검수하면 VersionConflict."""
    h = tenant["hospital_id"]; sc, v, cid = _seed_claim(owner, h)
    ctx = _ctx(owner, tenant, {"approver", "editor"})
    # v 기반 편집으로 current를 v2로 이동
    from services import scripts as svc
    with owner.connect() as cn:
        bk = cn.execute(text("select stable_block_key from script_blocks where version_id=:v limit 1"), {"v": v}).scalar()
    svc.edit_blocks(rw, ctx, sc, v, {bk: "편집된 문장으로 바뀜."})
    with pytest.raises(VersionConflict):
        ev.assess_claim(rw, ctx, sc, v, cid, "confirm")   # 과거 v로 검수 → 차단


def test_assess_frozen_after_approval(rw, owner, tenant):
    h, m = tenant["hospital_id"], tenant["membership_id"]; sc, v, cid = _seed_claim(owner, h)
    _grant(owner, h, m, "approver")
    ctx = _ctx(owner, tenant, {"approver"})
    ev.assess_claim(rw, ctx, sc, v, cid, "confirm")        # 검증됨으로 만들고
    with tenant_conn(rw, h, m) as cn:
        approve_version(cn, h, v, "policy-1")              # 승인
    with pytest.raises(InvalidStateTransition):            # 승인된 버전 근거 변경 금지
        ev.assess_claim(rw, ctx, sc, v, cid, "reject")


def test_assess_rejects_foreign_claim(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v, _ = _seed_claim(owner, h)
    ctx = _ctx(owner, tenant, {"approver"})
    with pytest.raises(NotFound):                          # 이 버전 소속 아닌 claim
        ev.assess_claim(rw, ctx, sc, v, uuid.uuid4(), "confirm")


def test_waive_requires_admin_and_reason(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v, cid = _seed_claim(owner, h)
    # approver는 waive 불가(admin capability)
    with pytest.raises(Forbidden):
        ev.assess_claim(rw, _ctx(owner, tenant, {"approver"}), sc, v, cid, "waive", reason="예외 사유")
    # admin이라도 사유 없으면 불가
    with pytest.raises(InvalidStateTransition):
        ev.assess_claim(rw, _ctx(owner, tenant, {"admin"}), sc, v, cid, "waive", reason="   ")
    # admin + 사유 → 성공, human_decision=waived
    r = ev.assess_claim(rw, _ctx(owner, tenant, {"admin"}), sc, v, cid, "waive", reason="근거요건 예외 승인")
    assert r["human_decision"] == "waived"
    with owner.connect() as cn:
        hd = cn.execute(text("select human_decision, decision_reason from claim_assessments "
                             "where claim_id=:c order by created_at desc limit 1"), {"c": cid}).first()
    assert hd.human_decision == "waived" and hd.decision_reason == "근거요건 예외 승인"


def test_review_seq_increments(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v, cid = _seed_claim(owner, h)
    ctx = _ctx(owner, tenant, {"approver"})
    ev.assess_claim(rw, ctx, sc, v, cid, "reject", reason="1차 반려")
    ev.assess_claim(rw, ctx, sc, v, cid, "confirm")
    with owner.connect() as cn:
        seqs = [r[0] for r in cn.execute(text("select review_seq from claim_assessments where claim_id=:c "
                                              "and assessment_kind='human_review' order by review_seq"), {"c": cid})]
    assert seqs == [1, 2]                                    # claim별 결정적 순번


def test_confirm_sets_accepted_decision(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v, cid = _seed_claim(owner, h)
    r = ev.assess_claim(rw, _ctx(owner, tenant, {"approver"}), sc, v, cid, "confirm")
    assert r["human_decision"] == "accepted"
    with owner.connect() as cn:
        hd = cn.execute(text("select human_decision, verification_status from claim_assessments "
                             "where claim_id=:c order by created_at desc limit 1"), {"c": cid}).first()
    assert hd.human_decision == "accepted" and hd.verification_status == "verified"
