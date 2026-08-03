"""image service — 재생성 권한 + 프롬프트 피드백 반영 + 영속 갱신(생성기 주입)."""
import uuid, pytest
from sqlalchemy import text
from services.context import ActorContext
from services import images as im
from services.exceptions import Forbidden, NotFound


def _ctx(tenant, roles):
    return ActorContext(user_id=str(tenant["user_id"]), hospital_id=str(tenant["hospital_id"]),
                        membership_id=str(tenant["membership_id"]), roles=frozenset(roles))


def _ensure_scene_images(owner):
    from store.seed_images import ensure_scene_images
    ensure_scene_images(owner)


def _seed_image(owner, h, key="blk_1", prompt="base prompt"):
    _ensure_scene_images(owner)
    with owner.begin() as cn:
        cn.execute(text("insert into scene_images(id,hospital_id,topic,block_key,prompt,mime,data) "
                        "values(:i,:h,'이명',:k,:p,'image/jpeg',:d) on conflict do nothing"),
                   {"i": uuid.uuid4(), "h": h, "k": key, "p": prompt, "d": b"OLD"})


def test_regen_requires_image_role(owner, rw, tenant):
    h = tenant["hospital_id"]; _seed_image(owner, h)
    with pytest.raises(Forbidden):                          # 역할 없음
        im.regenerate_scene(rw, _ctx(tenant, set()), "blk_1", generator=lambda p: b"NEW")


def test_regen_updates_image_with_injected_generator(owner, rw, tenant):
    h = tenant["hospital_id"]; _seed_image(owner, h)
    captured = {}
    def gen(prompt):
        captured["prompt"] = prompt
        return b"NEWJPEG"
    im.regenerate_scene(rw, _ctx(tenant, {"editor"}), "blk_1", feedback="더 밝게", generator=gen)
    assert "더 밝게" in captured["prompt"]                    # 피드백이 프롬프트에 반영
    from store.repositories import tenant_conn
    with tenant_conn(rw, h) as cn:
        data = cn.execute(text("select data from scene_images where hospital_id=:h and block_key='blk_1'"), {"h": h}).scalar()
    assert bytes(data) == b"NEWJPEG"                         # 영속 갱신


def test_regen_missing_scene(owner, rw, tenant):
    h = tenant["hospital_id"]; _ensure_scene_images(owner)
    with pytest.raises(NotFound):
        im.regenerate_scene(rw, _ctx(tenant, {"editor"}), "nope", generator=lambda p: b"X")


def test_regen_keeps_previous_image(owner, rw, tenant):
    # 비파괴: 재생성해도 이전 이미지가 히스토리에 보존(둘 다 남음).
    h = tenant["hospital_id"]; _seed_image(owner, h)          # 현재=b"OLD"
    im.regenerate_scene(rw, _ctx(tenant, {"editor"}), "blk_1", generator=lambda p: b"NEW1")
    from store.repositories import tenant_conn
    with tenant_conn(rw, h) as cn:
        cur = cn.execute(text("select data from scene_images where hospital_id=:h and block_key='blk_1'"), {"h": h}).scalar()
        hist = [bytes(r[0]) for r in cn.execute(text("select data from scene_image_versions "
                "where hospital_id=:h and block_key='blk_1' order by seq"), {"h": h}).all()]
    assert bytes(cur) == b"NEW1" and hist == [b"OLD"]          # 현재=새것, 이전=보존됨


def test_revert_restores_previous_keeps_both(owner, rw, tenant):
    # 되돌리기: 이전 이미지로 복원하되 방금 것도 보존(앞뒤 전환 가능).
    h = tenant["hospital_id"]; _seed_image(owner, h)          # OLD
    ctx = _ctx(tenant, {"editor"})
    im.regenerate_scene(rw, ctx, "blk_1", generator=lambda p: b"NEW1")   # 현재 NEW1, 히스토리[OLD]
    im.revert_scene(rw, ctx, "blk_1")                                     # 현재 OLD, 히스토리[OLD, NEW1]
    from store.repositories import tenant_conn
    with tenant_conn(rw, h) as cn:
        cur = cn.execute(text("select data from scene_images where hospital_id=:h and block_key='blk_1'"), {"h": h}).scalar()
        hist = [bytes(r[0]) for r in cn.execute(text("select data from scene_image_versions "
                "where hospital_id=:h and block_key='blk_1' order by seq"), {"h": h}).all()]
    assert bytes(cur) == b"OLD" and hist == [b"OLD", b"NEW1"]  # 되돌림 + 둘 다 보존


def test_revert_without_history_notfound(owner, rw, tenant):
    h = tenant["hospital_id"]; _seed_image(owner, h)
    with pytest.raises(NotFound):
        im.revert_scene(rw, _ctx(tenant, {"editor"}), "blk_1")   # 이전 없음


def test_upload_replaces_image_keeps_previous(owner, rw, tenant):
    # 내 사진 업로드: 현재 이미지를 교체하되 이전 것은 보존(비파괴). 웹 JPEG로 정규화.
    import io
    try:
        from PIL import Image
    except Exception:
        pytest.skip("Pillow 없음")
    h = tenant["hospital_id"]; _seed_image(owner, h)          # 현재=b"OLD"
    buf = io.BytesIO(); Image.new("RGB", (32, 32), (200, 30, 30)).save(buf, "PNG")
    im.upload_scene(rw, _ctx(tenant, {"editor"}), "blk_1", buf.getvalue())
    from store.repositories import tenant_conn
    with tenant_conn(rw, h) as cn:
        cur = cn.execute(text("select data, mime from scene_images where hospital_id=:h and block_key='blk_1'"), {"h": h}).first()
        hist = [bytes(r[0]) for r in cn.execute(text("select data from scene_image_versions "
                "where hospital_id=:h and block_key='blk_1' order by seq"), {"h": h}).all()]
    assert bytes(cur.data) != b"OLD" and cur.mime == "image/jpeg" and len(cur.data) > 0   # 업로드본으로 교체
    assert hist == [b"OLD"]                                   # 이전 보존


def test_upload_rejects_non_image(owner, rw, tenant):
    from services.exceptions import InvalidStateTransition
    h = tenant["hospital_id"]; _seed_image(owner, h)
    with pytest.raises(InvalidStateTransition):
        im.upload_scene(rw, _ctx(tenant, {"editor"}), "blk_1", b"not-an-image")


def test_image_stale_on_scene_change(owner, rw, tenant):
    """이미지를 version v 장면으로 생성 후 대본 장면이 바뀌면 stale 파생 판정."""
    from store.testkit import new_version, new_block
    h = tenant["hospital_id"]; _ensure_scene_images(owner)
    with owner.begin() as cn:
        sc, v = new_version(cn, h)
        cn.execute(text("update scripts set current_version_id=:v where id=:s"), {"v": v, "s": sc})
        b = new_block(cn, h, v, 0, key="blk_1")
        cn.execute(text("update script_blocks set scene='원래 장면', text='원래 문구' where id=:b"), {"b": b})
    _seed_image(owner, h, key="blk_1")
    # 현재 version 장면으로 이미지 생성(source 결착)
    im.regenerate_scene(rw, _ctx(tenant, {"editor"}), "blk_1", version_id=v, generator=lambda p: b"IMG")
    st = im.list_scene_status(rw, _ctx(tenant, {"editor"}), v)
    assert st["blk_1"]["stale"] is False                    # 방금 그 장면으로 생성 → 최신
    # 대본 장면 변경 → stale
    with owner.begin() as cn:
        cn.execute(text("update script_blocks set text='바뀐 문구' where hospital_id=:h and version_id=:v and stable_block_key='blk_1'"),
                   {"h": h, "v": v})
    st2 = im.list_scene_status(rw, _ctx(tenant, {"editor"}), v)
    assert st2["blk_1"]["stale"] is True and st2["blk_1"]["reason"] == "source_scene_changed"
