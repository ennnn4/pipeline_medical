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
        cn.execute(text("insert into membership_roles(id,hospital_id,membership_id,role) values(:i,:h,:m,:r)"),
                   {"i": uuid.uuid4(), "h": h, "m": m, "r": role})


def _ctx(tenant, roles):
    return ActorContext(user_id=str(tenant["user_id"]), hospital_id=str(tenant["hospital_id"]),
                        membership_id=str(tenant["membership_id"]), roles=frozenset(roles))


def test_assess_requires_review_role(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v, cid = _seed_claim(owner, h)
    ctx = _ctx(tenant, {"editor"})                       # editor는 검수 권한 없음
    with pytest.raises(Forbidden):
        ev.assess_claim(rw, ctx, sc, v, cid, "confirm")


def test_assess_confirm_appends_human_review(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v, cid = _seed_claim(owner, h)
    ctx = _ctx(tenant, {"approver"})
    ev.assess_claim(rw, ctx, sc, v, cid, "confirm")
    with owner.connect() as cn:
        n = cn.execute(text("select count(*) from claim_assessments where claim_id=:c and assessment_kind='human_review'"),
                       {"c": cid}).scalar()
    assert n == 1


def test_assess_rejects_stale_version(rw, owner, tenant):
    """current가 바뀐 뒤 과거 version_id로 검수하면 VersionConflict."""
    h = tenant["hospital_id"]; sc, v, cid = _seed_claim(owner, h)
    ctx = _ctx(tenant, {"approver", "editor"})
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
    ctx = _ctx(tenant, {"approver"})
    ev.assess_claim(rw, ctx, sc, v, cid, "confirm")        # 검증됨으로 만들고
    with tenant_conn(rw, h, m) as cn:
        approve_version(cn, h, v, "policy-1")              # 승인
    with pytest.raises(InvalidStateTransition):            # 승인된 버전 근거 변경 금지
        ev.assess_claim(rw, ctx, sc, v, cid, "reject")


def test_assess_rejects_foreign_claim(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v, _ = _seed_claim(owner, h)
    ctx = _ctx(tenant, {"approver"})
    with pytest.raises(NotFound):                          # 이 버전 소속 아닌 claim
        ev.assess_claim(rw, ctx, sc, v, uuid.uuid4(), "confirm")
