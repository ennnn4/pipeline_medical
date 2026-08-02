"""service 계층 — ActorContext 해석 + scripts 편집 service(권한·변경필터·parity)."""
import uuid, pytest
from sqlalchemy import text
from store.testkit import new_version
from store.repositories import tenant_conn
from services.context import ActorContext
from services import scripts as svc
from services.exceptions import Forbidden, VersionConflict, NotFound


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


def _grant(owner, h, m, role):
    with owner.begin() as cn:
        cn.execute(text("insert into membership_roles(id,hospital_id,membership_id,role) values(:i,:h,:m,:r)"),
                   {"i": uuid.uuid4(), "h": h, "m": m, "r": role})


def _ctx(tenant, roles):
    return ActorContext(user_id=str(tenant["user_id"]), hospital_id=str(tenant["hospital_id"]),
                        membership_id=str(tenant["membership_id"]), roles=frozenset(roles))


def test_resolve_builds_context_with_roles(rw, owner, tenant):
    h, m = tenant["hospital_id"], tenant["membership_id"]
    _grant(owner, h, m, "editor")
    with owner.connect() as cn:
        slug = cn.execute(text("select slug from hospitals where id=:h"), {"h": h}).scalar()
    ctx = ActorContext.resolve(rw, tenant["user_id"], slug)
    assert ctx.hospital_id == str(h) and ctx.membership_id == str(m)
    assert ctx.has_role("editor")


def test_resolve_rejects_non_member(rw, owner, tenant):
    with owner.connect() as cn:
        slug = cn.execute(text("select slug from hospitals where id=:h"), {"h": tenant["hospital_id"]}).scalar()
    with pytest.raises(Forbidden):
        ActorContext.resolve(rw, uuid.uuid4(), slug)      # 멤버 아님


def test_edit_service_creates_version(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v1 = _seed(owner, h)
    ctx = _ctx(tenant, {"editor"})
    res = svc.edit_blocks(rw, ctx, sc, v1, {"blk_2": "THI가 54점에서 2점으로 개선됐습니다."})
    assert res["no_change"] is False and res["version_id"]
    with owner.connect() as cn:
        assert str(cn.execute(text("select current_version_id from scripts where id=:s"), {"s": sc}).scalar()) == res["version_id"]


def test_edit_service_no_change(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v1 = _seed(owner, h)
    ctx = _ctx(tenant, {"editor"})
    res = svc.edit_blocks(rw, ctx, sc, v1, {"blk_2": "이명이 있으면 목을 함께 봅니다."})  # 원문과 동일
    assert res["no_change"] is True and res["version_id"] is None       # 새 버전 안 만듦


def test_edit_service_requires_role(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v1 = _seed(owner, h)
    ctx = _ctx(tenant, set())                                            # 역할 없음
    with pytest.raises(Forbidden):
        svc.edit_blocks(rw, ctx, sc, v1, {"blk_2": "바뀐 내용"})


def test_edit_records_author_and_supersedes(rw, owner, tenant):
    h, m = tenant["hospital_id"], tenant["membership_id"]
    sc, v1 = _seed(owner, h)
    ctx = _ctx(tenant, {"editor"})
    v2 = svc.edit_blocks(rw, ctx, sc, v1, {"blk_2": "THI가 54에서 2로 개선됐습니다."})["version_id"]
    with owner.connect() as cn:
        # 새 버전 작성자 = 편집 membership(source=editor)
        assert str(cn.execute(text("select created_by_membership_id from script_versions where id=:v"),
                              {"v": v2}).scalar()) == str(m)
        # superseded 명시 기록(Step2.5.1): v1.superseded_by = v2, 그리고 current도 v2
        assert str(cn.execute(text("select superseded_by_version_id from version_approval_states where version_id=:v"),
                              {"v": v1}).scalar()) == v2
        cur = str(cn.execute(text("select current_version_id from scripts where id=:s"), {"s": sc}).scalar())
        assert cur == v2 and cur != str(v1)


def test_edit_rejects_unknown_block(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v1 = _seed(owner, h)
    ctx = _ctx(tenant, {"editor"})
    with pytest.raises(NotFound):                          # base version에 없는 블록 → 조용히 무시 안 함
        svc.edit_blocks(rw, ctx, sc, v1, {"nonexistent_block": "x"})


def test_workspace_returns_render_data(rw, owner, tenant):
    from services import workspace as ws
    h = tenant["hospital_id"]; sc, v1 = _seed(owner, h)
    ctx = _ctx(tenant, {"editor"})
    w = ws.get_version_workspace(rw, ctx, v1)
    assert w["script_id"] == str(sc) and w["is_current"] is True and w["approval_status"] == "none"
    assert [b["stable_block_key"] for b in w["blocks"]] == ["blk_1", "blk_2"]
    assert w["available_actions"]["can_edit"] is True and w["available_actions"]["can_export"] is False


def test_workspace_missing_version(rw, owner, tenant):
    from services import workspace as ws
    from services.exceptions import NotFound
    ctx = _ctx(tenant, {"editor"})
    with pytest.raises(NotFound):
        ws.get_version_workspace(rw, ctx, uuid.uuid4())


def test_edit_service_version_conflict(rw, owner, tenant):
    h = tenant["hospital_id"]; sc, v1 = _seed(owner, h)
    ctx = _ctx(tenant, {"editor"})
    svc.edit_blocks(rw, ctx, sc, v1, {"blk_2": "첫 편집"})               # v1→v2 (current=v2)
    with pytest.raises(VersionConflict):                                 # 낡은 base_version으로 재편집
        svc.edit_blocks(rw, ctx, sc, v1, {"blk_2": "두번째 편집"})
