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
    from store.seed_images import DDL, _policies
    with owner.begin() as cn:
        cn.execute(text(DDL))
        for s in _policies():
            cn.execute(text(s))


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
