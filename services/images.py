"""image service — 장면 이미지 재생성. 라우트 공유.

정책(GPT): scene_images는 (hospital, block_key) 기준(버전 독립) — 대본 텍스트/근거 승인과 별개 축.
따라서 이미지 재생성은 script version 승인 상태를 무효화하지 않는다(이미지 승인은 후속 별도 축 가능).
OpenAI 등 외부 호출은 DB 트랜잭션 밖에서 수행(긴 작업 중 lock 미보유). 이미지 생성기는 주입 가능(테스트).
"""
import uuid, hashlib
from sqlalchemy import text
from store.repositories import tenant_conn
from services import permissions
from services.exceptions import NotFound, ServiceError, InvalidStateTransition


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


def _archive_current(conn, hospital_id, block_key):
    """현재 scene_images 이미지를 히스토리로 보존(비파괴). 다음 seq 부여. 현재 이미지 없으면 no-op."""
    seq = conn.execute(text("select coalesce(max(seq),0)+1 from scene_image_versions "
                            "where hospital_id=:h and block_key=:k"), {"h": hospital_id, "k": block_key}).scalar()
    n = conn.execute(text(
        "insert into scene_image_versions(hospital_id, block_key, seq, mime, data, prompt, model, "
        "source_version_id, source_scene_hash, source_prompt_hash, generated_by_membership_id) "
        "select hospital_id, block_key, :seq, mime, data, prompt, model, source_version_id, source_scene_hash, "
        "source_prompt_hash, generated_by_membership_id from scene_images where hospital_id=:h and block_key=:k"),
        {"seq": seq, "h": hospital_id, "k": block_key}).rowcount
    return seq if n else None


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
        _archive_current(conn, ctx.hospital_id, block_key)   # 비파괴: 현재(이전) 이미지 보존 후 새 것으로
        r = conn.execute(text(
            "update scene_images set data=:d, prompt=:p, source_version_id=:vid, source_scene_hash=:sh, "
            "source_prompt_hash=:ph, generated_by_membership_id="
            "NULLIF(current_setting('app.membership_id', true), '')::uuid, updated_at=now() "
            "where hospital_id=:h and block_key=:k"),
            {"d": jpg, "p": prompt, "vid": vid, "sh": sh, "ph": ph, "h": ctx.hospital_id, "k": block_key})
        if r.rowcount == 0:
            raise NotFound("해당 장면 이미지가 없습니다")
    try:                                    # 성공 이벤트(GPT): prompt·block_key 내용 미포함, 병원 해시
        from services.observability import emit, hid
        emit("image_regenerated", hospital=hid(ctx.hospital_id),
             request_id=getattr(ctx, "request_id", None))
    except Exception:
        pass
    return {"block_key": block_key}


def upload_scene(engine, ctx, block_key, raw_bytes, version_id=None, topic=None):
    """장면 이미지를 사용자가 올린 사진으로 교체(비파괴 — 현재 것은 히스토리 보존). 웹 JPEG로 정규화.
    editor/approver/admin(+platform_operator)만. 이미지 없던 블록이면 새로 INSERT(topic 필요)."""
    permissions.require(ctx, permissions.IMAGE_ROLES)
    from store.seed_images import web_jpeg_bytes
    if not raw_bytes:
        raise InvalidStateTransition("업로드된 파일이 없습니다")
    try:
        jpg = web_jpeg_bytes(raw_bytes)                      # 리사이즈·JPEG 정규화(형식 오류면 예외)
    except Exception:
        raise InvalidStateTransition("이미지 파일이 아니거나 형식을 읽을 수 없습니다")
    vid = (version_id if isinstance(version_id, uuid.UUID) else uuid.UUID(str(version_id))) if version_id else None
    sh = None
    with tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id) as conn:
        exists = conn.execute(text("select topic from scene_images where hospital_id=:h and block_key=:k limit 1"),
                              {"h": ctx.hospital_id, "k": block_key}).first()
        if vid:
            sh = _block_scene_hash(conn, ctx.hospital_id, vid, block_key)
    with tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id,
                     request_id=ctx.request_id) as conn:
        _archive_current(conn, ctx.hospital_id, block_key)   # 비파괴: 현재 이미지 보존
        if exists:
            conn.execute(text(
                "update scene_images set data=:d, mime='image/jpeg', prompt=coalesce(prompt,'[업로드]'), "
                "source_version_id=:vid, source_scene_hash=:sh, generated_by_membership_id="
                "NULLIF(current_setting('app.membership_id', true), '')::uuid, updated_at=now() "
                "where hospital_id=:h and block_key=:k"),
                {"d": jpg, "vid": vid, "sh": sh, "h": ctx.hospital_id, "k": block_key})
        else:
            tp = topic or (exists.topic if exists else None) or "업로드"
            conn.execute(text(
                "insert into scene_images(id,hospital_id,topic,block_key,mime,data,prompt,source_version_id,source_scene_hash,"
                "generated_by_membership_id) values(gen_random_uuid(),:h,:tp,:k,'image/jpeg',:d,'[업로드]',:vid,:sh,"
                "NULLIF(current_setting('app.membership_id', true), '')::uuid)"),
                {"h": ctx.hospital_id, "tp": tp, "k": block_key, "d": jpg, "vid": vid, "sh": sh})
    return {"block_key": block_key, "uploaded": True}


def revert_scene(engine, ctx, block_key, seq=None):
    """이전 이미지로 되돌리기(비파괴) — 현재 것도 히스토리에 보존한 뒤 지정(또는 가장 최근) 아카이브를 현재로.
    둘 다 남으므로 다시 앞뒤로 전환 가능. editor/approver/admin(+platform_operator)만."""
    permissions.require(ctx, permissions.IMAGE_ROLES)
    with tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id,
                     request_id=ctx.request_id) as conn:
        q = ("select seq, mime, data, prompt, model, source_version_id, source_scene_hash, source_prompt_hash "
             "from scene_image_versions where hospital_id=:h and block_key=:k "
             + ("and seq=:s " if seq is not None else "") + "order by seq desc limit 1")
        p = {"h": ctx.hospital_id, "k": block_key}
        if seq is not None:
            p["s"] = seq
        t = conn.execute(text(q), p).first()
        if t is None:
            raise NotFound("되돌릴 이전 이미지가 없습니다")
        _archive_current(conn, ctx.hospital_id, block_key)   # 현재(새) 것도 보존 → 앞뒤 전환 가능
        r = conn.execute(text(
            "update scene_images set data=:d, prompt=:p, mime=:m, source_version_id=:vid, "
            "source_scene_hash=:sh, source_prompt_hash=:ph, updated_at=now() "
            "where hospital_id=:h and block_key=:k"),
            {"d": t.data, "p": t.prompt, "m": t.mime, "vid": t.source_version_id, "sh": t.source_scene_hash,
             "ph": t.source_prompt_hash, "h": ctx.hospital_id, "k": block_key})
        if r.rowcount == 0:
            raise NotFound("해당 장면 이미지가 없습니다")
    return {"block_key": block_key, "reverted_to_seq": t.seq}


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
        prev = {r[0] for r in conn.execute(text(   # 이전 이미지(되돌리기 가능) 존재하는 블록
            "select distinct block_key from scene_image_versions where hospital_id=:h"), {"h": ctx.hospital_id})}
    for b in blocks:
        img = imgs.get(b.stable_block_key)
        hp = b.stable_block_key in prev
        if img is None:
            out[b.stable_block_key] = {"has_image": False, "stale": False, "reason": None, "has_prev": hp}
            continue
        if img.source_scene_hash is None:
            out[b.stable_block_key] = {"has_image": True, "stale": True, "reason": "legacy_unbound", "has_prev": hp}
            continue
        cur = scene_hash(b.stable_block_key, b.block_type, b.scene, b.text)
        stale = cur != img.source_scene_hash
        out[b.stable_block_key] = {"has_image": True, "stale": stale,
                                   "reason": ("source_scene_changed" if stale else None), "has_prev": hp}
    return out
