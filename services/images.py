"""image service — 장면 이미지 재생성. 라우트 공유.

정책(GPT): scene_images는 (hospital, block_key) 기준(버전 독립) — 대본 텍스트/근거 승인과 별개 축.
따라서 이미지 재생성은 script version 승인 상태를 무효화하지 않는다(이미지 승인은 후속 별도 축 가능).
OpenAI 등 외부 호출은 DB 트랜잭션 밖에서 수행(긴 작업 중 lock 미보유). 이미지 생성기는 주입 가능(테스트).
"""
import uuid, hashlib
from sqlalchemy import text
from store.repositories import tenant_conn
from services import permissions
from services.exceptions import NotFound, ServiceError


def _default_generator(prompt):
    from assets.gen_images import gen_image_bytes
    from store.seed_images import web_jpeg_bytes
    return web_jpeg_bytes(gen_image_bytes(prompt))


def scene_hash(block_key, block_type, scene, text_):
    """이미지 생성에 영향을 주는 장면 입력의 canonical 해시(대본 변경 시 stale 판정 기준)."""
    canon = "\x1f".join([str(block_key), str(block_type or ""), str(scene or ""), str(text_ or "")])
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _block_scene_hash(conn, hospital_id, version_id, block_key):
    r = conn.execute(text("select block_type, scene, text from script_blocks "
                          "where hospital_id=:h and version_id=:v and stable_block_key=:k limit 1"),
                     {"h": hospital_id, "v": version_id, "k": block_key}).first()
    if r is None:
        return None
    return scene_hash(block_key, r.block_type, r.scene, r.text)


def regenerate_scene(engine, ctx, block_key, feedback="", version_id=None, generator=None):
    """장면 이미지 재생성 → scene_images 갱신(영속) + provenance 기록. editor/approver/admin만.
    version_id를 주면 그 version 장면 입력의 source_scene_hash를 결착(대본 변경 시 stale 판정).
    generator(prompt)->jpeg bytes 주입 가능(기본=OpenAI). 반환: {block_key}."""
    permissions.require(ctx, permissions.IMAGE_ROLES)
    generator = generator or _default_generator
    vid = (version_id if isinstance(version_id, uuid.UUID) else uuid.UUID(str(version_id))) if version_id else None
    with tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id) as conn:
        row = conn.execute(text("select prompt from scene_images where hospital_id=:h and block_key=:k limit 1"),
                           {"h": ctx.hospital_id, "k": block_key}).first()
        sh = _block_scene_hash(conn, ctx.hospital_id, vid, block_key) if vid else None
    base = (row.prompt if row else None) or f"clean medical educational illustration for scene {block_key}"
    prompt = base + (f" Reviewer adjustment: {feedback}." if feedback else " Provide a fresh alternative composition.")
    ph = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    jpg = generator(prompt)                                   # 외부 호출은 트랜잭션 밖
    with tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id,
                     request_id=ctx.request_id) as conn:
        r = conn.execute(text(
            "update scene_images set data=:d, prompt=:p, source_version_id=:vid, source_scene_hash=:sh, "
            "source_prompt_hash=:ph, generated_by_membership_id="
            "NULLIF(current_setting('app.membership_id', true), '')::uuid, updated_at=now() "
            "where hospital_id=:h and block_key=:k"),
            {"d": jpg, "p": prompt, "vid": vid, "sh": sh, "ph": ph, "h": ctx.hospital_id, "k": block_key})
        if r.rowcount == 0:
            raise NotFound("해당 장면 이미지가 없습니다")
    return {"block_key": block_key}


def list_scene_status(engine, ctx, version_id):
    """이 version의 블록별 이미지 존재·stale 파생 판정(대본 장면이 이미지 생성 당시와 달라졌는지).
    반환: {block_key: {'has_image', 'stale', 'reason'}}. source 결착이 없으면 legacy_unbound(수동 확인)."""
    vid = version_id if isinstance(version_id, uuid.UUID) else uuid.UUID(str(version_id))
    out = {}
    with tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id) as conn:
        blocks = conn.execute(text("select stable_block_key, block_type, scene, text from script_blocks "
                                   "where hospital_id=:h and version_id=:v"), {"h": ctx.hospital_id, "v": vid}).all()
        imgs = {r.block_key: r for r in conn.execute(text(
            "select block_key, source_version_id, source_scene_hash from scene_images where hospital_id=:h"),
            {"h": ctx.hospital_id})}
    for b in blocks:
        img = imgs.get(b.stable_block_key)
        if img is None:
            out[b.stable_block_key] = {"has_image": False, "stale": False, "reason": None}
            continue
        if img.source_scene_hash is None:
            out[b.stable_block_key] = {"has_image": True, "stale": True, "reason": "legacy_unbound"}
            continue
        cur = scene_hash(b.stable_block_key, b.block_type, b.scene, b.text)
        stale = cur != img.source_scene_hash
        out[b.stable_block_key] = {"has_image": True, "stale": stale,
                                   "reason": ("source_scene_changed" if stale else None)}
    return out
