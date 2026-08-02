"""3단계 편집 오케스트레이션 — 새 버전·재분할·claim 재추출(unverified)·편집이력·미승인."""
import uuid, pytest
from sqlalchemy import text
from store.testkit import sqlstate, new_version
from store.repositories import tenant_conn, apply_block_edit, is_stale, approve_version

def _seed(owner, h):
    with owner.begin() as cn:
        sc, v = new_version(cn, h)
        cn.execute(text("update scripts set current_version_id=:v where id=:s"), {"v": v, "s": sc})
        for key, oi, tx in [("blk_1", 0, "안녕하세요, 한의사 송정현입니다."),
                            ("blk_2", 1, "이명이 있으면 목을 함께 봅니다.")]:
            cn.execute(text("insert into script_blocks(id,hospital_id,version_id,stable_block_key,order_index,block_type,text) "
                            "values(:b,:h,:v,:k,:o,'explanation',:tx)"),
                       {"b": uuid.uuid4(), "h": h, "v": v, "k": key, "o": oi, "tx": tx})
    return sc, v

def _grant_approver(owner, h, m):
    with owner.begin() as cn:
        cn.execute(text("insert into membership_roles(id,hospital_id,membership_id,role) values(:i,:h,:m,'approver')"),
                   {"i": uuid.uuid4(), "h": h, "m": m})

def test_apply_block_edit_creates_unapproved_version(owner, rw, tenant):
    h, m = tenant["hospital_id"], tenant["membership_id"]
    sc, v1 = _seed(owner, h)
    with tenant_conn(rw, h, m) as cn:
        res = apply_block_edit(cn, h, sc, v1, {"blk_2": "이명장애척도가 54점에서 2점으로 개선됐습니다. 부작용은 없었습니다."})
    v2 = res["version_id"]
    with owner.connect() as cn:
        assert cn.execute(text("select current_version_id from scripts where id=:s"), {"s": sc}).scalar() == v2
        blks = dict(cn.execute(text("select stable_block_key, text from script_blocks where version_id=:v"), {"v": v2}).all())
        assert blks["blk_1"] == "안녕하세요, 한의사 송정현입니다."          # 미변경 블록 복제
        assert "개선" in blks["blk_2"]                                  # 변경 반영
        # 변경 블록 재분할 → 문장 2개 이상
        ns = cn.execute(text("select count(*) from script_sentences ss join script_blocks b on ss.block_id=b.id "
                             "where b.version_id=:v and b.stable_block_key='blk_2'"), {"v": v2}).scalar()
        assert ns >= 2
        # claim 재추출(수치 문장) — unverified(assessment 없음)
        nclaims = cn.execute(text("select count(*) from claims where version_id=:v"), {"v": v2}).scalar()
        assert nclaims >= 1
        eff = cn.execute(text("select count(*) from claim_effective_assessment e join claims c on e.claim_id=c.id where c.version_id=:v"), {"v": v2}).scalar()
        assert eff == 0                                                 # 근거 판정 없음(4단계 전)
        # 편집 이력
        assert cn.execute(text("select count(*) from edits where to_version_id=:v"), {"v": v2}).scalar() == 1

def test_edited_version_not_approvable_until_verified(owner, rw, tenant):
    h, m = tenant["hospital_id"], tenant["membership_id"]
    sc, v1 = _seed(owner, h)
    with tenant_conn(rw, h, m) as cn:
        v2 = apply_block_edit(cn, h, sc, v1, {"blk_2": "THI가 54점에서 2점으로 개선됐습니다."})["version_id"]  # 작성자=m
    # 승인자는 작성자와 다른 membership이어야(작성자≠승인자) evidence gate에 도달
    m2, u2 = uuid.uuid4(), uuid.uuid4()
    with owner.begin() as cn:
        cn.execute(text("insert into users(id,email) values(:u,:e)"), {"u": u2, "e": u2.hex + "@t.c"})
        cn.execute(text("insert into hospital_memberships(id,hospital_id,user_id) values(:m,:h,:u)"), {"m": m2, "h": h, "u": u2})
        cn.execute(text("insert into membership_roles(id,hospital_id,membership_id,role) values(:i,:h,:m,'approver')"),
                   {"i": uuid.uuid4(), "h": h, "m": m2})
    with pytest.raises(Exception) as ei:                               # unverified claim → 승인 차단
        with tenant_conn(rw, h, m2) as cn:
            approve_version(cn, h, v2, "policy-1")
    assert sqlstate(ei.value) == "23514"
    with tenant_conn(rw, h, m2) as cn:
        assert is_stale(cn, h, v2, "policy-1") is True                 # 미승인 → 출력 차단

def test_edit_compliance_recheck_flags_banned(owner, rw, tenant):
    h, m = tenant["hospital_id"], tenant["membership_id"]
    sc, v1 = _seed(owner, h)
    with tenant_conn(rw, h, m) as cn:
        res = apply_block_edit(cn, h, sc, v1, {"blk_2": "이 치료로 100% 완치됩니다."})   # 금지어
    # 변경 블록 compliance 재검사 결과에 금지어 findings
    assert res["compliance"]["blk_2"], "금지어가 재검사에서 잡혀야"
