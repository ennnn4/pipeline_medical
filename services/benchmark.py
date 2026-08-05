"""벤치마킹 service — 프로젝트/영상/자막 업무규칙·권한·트랜잭션 경계(개발지침).

 - route는 이 service만 호출(직접 SQL·상태전이·RLS 우회 금지).
 - 권한은 permissions로 검증, 테넌트 격리는 tenant_conn(RLS)이 강제.
 - 메타/자막 수집은 provider(youtube_meta·transcripts) 주입 → 테스트에서 mock 가능.
 - 원본 자막 ≠ 분석결과: 여기선 수집/저장까지(분석은 C4~).
"""
import uuid
from sqlalchemy import text
from store.repositories import tenant_conn
from services import permissions
from services.exceptions import NotFound, ServiceError
from services import transcripts as tx
from services import youtube_meta

BENCHMARK_ROLES = {"editor", "approver", "admin", "platform_operator"}


def _uuid(v):
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


def _conn(engine, ctx):
    return tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id, request_id=ctx.request_id)


# ── 프로젝트 ──
def create_project(engine, ctx, title):
    permissions.require(ctx, BENCHMARK_ROLES)
    title = (title or "").strip()
    if not title:
        raise ServiceError("프로젝트 제목이 필요합니다")
    with _conn(engine, ctx) as cn:
        pid = cn.execute(text(
            "insert into benchmark_projects(hospital_id,title,created_by_membership_id) "
            "values(:h,:t,:m) returning id"),
            {"h": ctx.hospital_id, "t": title, "m": ctx.membership_id}).scalar()
    return {"project_id": str(pid), "title": title, "status": "draft"}


def list_projects(engine, ctx):
    permissions.require(ctx, BENCHMARK_ROLES)
    with _conn(engine, ctx) as cn:
        rows = cn.execute(text(
            "select p.id, p.title, p.status, p.created_at, "
            "  (select count(*) from youtube_videos v where v.project_id=p.id) as videos "
            "from benchmark_projects p where p.hospital_id=:h "
            "order by p.created_at desc"), {"h": ctx.hospital_id}).all()
    return [{"project_id": str(r.id), "title": r.title, "status": r.status,
             "videos": r.videos, "created_at": r.created_at.isoformat()} for r in rows]


def get_project(engine, ctx, project_id):
    permissions.require(ctx, BENCHMARK_ROLES)
    pid = _uuid(project_id)
    with _conn(engine, ctx) as cn:
        p = cn.execute(text(
            "select id, title, status, created_at from benchmark_projects "
            "where id=:p and hospital_id=:h"), {"p": pid, "h": ctx.hospital_id}).first()
        if not p:
            raise NotFound("벤치마킹 프로젝트를 찾을 수 없습니다")
        vids = cn.execute(text(
            "select v.id, v.url, v.video_id, v.title, v.channel_name, v.view_count, "
            "  v.like_count, v.published_at, v.duration, v.caption_status, v.metadata_fetched_at, "
            "  (select t.status from youtube_transcripts t where t.video_ref=v.id "
            "     order by t.created_at desc limit 1) as transcript_status "
            "from youtube_videos v where v.project_id=:p order by v.created_at"),
            {"p": pid}).all()
    return {
        "project_id": str(p.id), "title": p.title, "status": p.status,
        "created_at": p.created_at.isoformat(),
        "videos": [{
            "video_ref": str(v.id), "url": v.url, "video_id": v.video_id, "title": v.title,
            "channel_name": v.channel_name, "view_count": v.view_count, "like_count": v.like_count,
            "published_at": v.published_at.isoformat() if v.published_at else None,
            "duration": v.duration, "caption_status": v.caption_status,
            "metadata_fetched": bool(v.metadata_fetched_at),
            "transcript_status": v.transcript_status,
        } for v in vids],
    }


# ── 영상 ──
def add_video(engine, ctx, project_id, url):
    """프로젝트에 영상 URL 등록. video_id 추출(실패해도 URL만으로 등록). 중복 URL은 기존 반환."""
    permissions.require(ctx, BENCHMARK_ROLES)
    pid = _uuid(project_id)
    url = (url or "").strip()
    if not url:
        raise ServiceError("영상 URL이 필요합니다")
    vid = tx.youtube_video_id(url)
    with _conn(engine, ctx) as cn:
        if not cn.execute(text("select 1 from benchmark_projects where id=:p and hospital_id=:h"),
                          {"p": pid, "h": ctx.hospital_id}).first():
            raise NotFound("벤치마킹 프로젝트를 찾을 수 없습니다")
        row = cn.execute(text(
            "insert into youtube_videos(hospital_id,project_id,url,video_id) "
            "values(:h,:p,:u,:v) "
            "on conflict (project_id,url) do update set video_id=coalesce(youtube_videos.video_id,excluded.video_id) "
            "returning id, video_id"), {"h": ctx.hospital_id, "p": pid, "u": url, "v": vid}).first()
    return {"video_ref": str(row.id), "url": url, "video_id": row.video_id}


def fetch_metadata(engine, ctx, project_id, fetcher=None):
    """프로젝트 영상들의 YouTube 메타 조회 후 저장. fetcher 미지정 시 youtube_meta.fetch(키 필요).
    키 없으면 skipped 반환(무영향). 반환: {updated, skipped, no_api}."""
    permissions.require(ctx, BENCHMARK_ROLES)
    pid = _uuid(project_id)
    fetcher = fetcher or youtube_meta.fetch
    with _conn(engine, ctx) as cn:
        rows = cn.execute(text(
            "select id, video_id from youtube_videos where project_id=:p and hospital_id=:h and video_id is not null"),
            {"p": pid, "h": ctx.hospital_id}).all()
    if not rows:
        return {"updated": 0, "skipped": 0, "no_api": not youtube_meta.enabled()}
    by_vid = {}
    for r in rows:
        by_vid.setdefault(r.video_id, []).append(r.id)
    meta = fetcher(list(by_vid.keys())) or {}
    if not meta:
        return {"updated": 0, "skipped": len(rows), "no_api": not youtube_meta.enabled()}
    updated = 0
    with _conn(engine, ctx) as cn:
        for vid, m in meta.items():
            for ref in by_vid.get(vid, []):
                cn.execute(text(
                    "update youtube_videos set title=:t, description=:d, thumbnail_url=:th, "
                    "channel_id=:cid, channel_name=:cn, published_at=cast(:pa as timestamptz), "
                    "view_count=:vc, like_count=:lc, comment_count=:cc, subscriber_count=:sc, "
                    "duration=:du, caption_status=:cs, metadata_fetched_at=now() "
                    "where id=:id and hospital_id=:h"),
                    {"t": m.get("title"), "d": m.get("description"), "th": m.get("thumbnail_url"),
                     "cid": m.get("channel_id"), "cn": m.get("channel_name"), "pa": m.get("published_at"),
                     "vc": m.get("view_count"), "lc": m.get("like_count"), "cc": m.get("comment_count"),
                     "sc": m.get("subscriber_count"), "du": m.get("duration"), "cs": m.get("caption_status"),
                     "id": ref, "h": ctx.hospital_id})
                updated += 1
    return {"updated": updated, "skipped": len(rows) - updated, "no_api": False}


# ── 자막(C2 provider → DB 저장) ──
def fetch_transcript(engine, ctx, video_ref, *, pasted_text=None, file_bytes=None,
                     filename=None, try_external=True):
    """영상 자막 수집(collect_transcript) 후 youtube_transcripts에 저장.
    manual_required도 상태로 저장(운영자가 나중에 붙여넣기·업로드 가능). 반환: {status,provider,char_count}."""
    permissions.require(ctx, BENCHMARK_ROLES)
    ref = _uuid(video_ref)
    with _conn(engine, ctx) as cn:
        v = cn.execute(text("select url from youtube_videos where id=:r and hospital_id=:h"),
                       {"r": ref, "h": ctx.hospital_id}).first()
        if not v:
            raise NotFound("영상을 찾을 수 없습니다")
    res = tx.collect_transcript(v.url, pasted_text=pasted_text, file_bytes=file_bytes,
                                filename=filename, try_external=try_external)
    provider = res.provider or ("manual" if pasted_text else "external")
    fetched = "now()" if res.status == "available" else "null"
    with _conn(engine, ctx) as cn:
        cn.execute(text(
            f"insert into youtube_transcripts(hospital_id,video_ref,provider,status,lang,has_timestamps,"
            f"normalized_text,source_note,char_count,fetched_at) "
            f"values(:h,:r,:p,:s,:l,:ts,:nt,:sn,:cc,{fetched})"),
            {"h": ctx.hospital_id, "r": ref, "p": provider, "s": res.status, "l": res.lang,
             "ts": res.has_timestamps, "nt": (res.text or None), "sn": res.source_note,
             "cc": len(res.text or "")})
    return {"status": res.status, "provider": provider, "char_count": len(res.text or ""),
            "source_note": res.source_note}
