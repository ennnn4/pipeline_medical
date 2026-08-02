"""image service — 장면 이미지 재생성. 라우트 공유.

정책(GPT): scene_images는 (hospital, block_key) 기준(버전 독립) — 대본 텍스트/근거 승인과 별개 축.
따라서 이미지 재생성은 script version 승인 상태를 무효화하지 않는다(이미지 승인은 후속 별도 축 가능).
OpenAI 등 외부 호출은 DB 트랜잭션 밖에서 수행(긴 작업 중 lock 미보유). 이미지 생성기는 주입 가능(테스트).
"""
from sqlalchemy import text
from store.repositories import tenant_conn
from services import permissions
from services.exceptions import NotFound, ServiceError


def _default_generator(prompt):
    from assets.gen_images import gen_image_bytes
    from store.seed_images import web_jpeg_bytes
    return web_jpeg_bytes(gen_image_bytes(prompt))


def regenerate_scene(engine, ctx, block_key, feedback="", generator=None):
    """장면 이미지 재생성 → scene_images 갱신(영속). editor/approver/admin만.
    generator(prompt)->jpeg bytes 주입 가능(기본=OpenAI). 반환: {block_key}."""
    permissions.require(ctx, permissions.IMAGE_ROLES)
    generator = generator or _default_generator
    with tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id) as conn:
        row = conn.execute(text("select prompt from scene_images where hospital_id=:h and block_key=:k limit 1"),
                           {"h": ctx.hospital_id, "k": block_key}).first()
    base = (row.prompt if row else None) or f"clean medical educational illustration for scene {block_key}"
    prompt = base + (f" Reviewer adjustment: {feedback}." if feedback else " Provide a fresh alternative composition.")
    jpg = generator(prompt)                                   # 외부 호출은 트랜잭션 밖
    with tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id,
                     request_id=ctx.request_id) as conn:
        r = conn.execute(text("update scene_images set data=:d, updated_at=now() "
                              "where hospital_id=:h and block_key=:k"),
                         {"d": jpg, "h": ctx.hospital_id, "k": block_key})
        if r.rowcount == 0:
            raise NotFound("해당 장면 이미지가 없습니다")
    return {"block_key": block_key}
