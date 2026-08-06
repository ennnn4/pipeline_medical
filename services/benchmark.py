"""벤치마킹 service — 프로젝트/영상/자막 업무규칙·권한·트랜잭션 경계(개발지침).

 - route는 이 service만 호출(직접 SQL·상태전이·RLS 우회 금지).
 - 권한은 permissions로 검증, 테넌트 격리는 tenant_conn(RLS)이 강제.
 - 메타/자막 수집은 provider(youtube_meta·transcripts) 주입 → 테스트에서 mock 가능.
 - 원본 자막 ≠ 분석결과: 여기선 수집/저장까지(분석은 C4~).
"""
import os
import json
import uuid
import hashlib
from sqlalchemy import text
from store.repositories import tenant_conn
from services import permissions
from services.exceptions import NotFound, ServiceError, InvalidStateTransition
from services import transcripts as tx
from services import youtube_meta

BENCHMARK_ROLES = {"editor", "approver", "admin", "platform_operator"}
ANALYZE_PROMPT_VERSION = "ba-1"
SYNTH_PROMPT_VERSION = "cs-1"
PLAN_PROMPT_VERSION = "cp-1"


def _uuid(v):
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


def _conn(engine, ctx):
    return tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id, request_id=ctx.request_id)


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 레포 루트

def _kb_read(slug, name, limit):
    p = os.path.join(_ROOT, "data", slug, "kb", name)
    if os.path.exists(p):
        try:
            return open(p, encoding="utf-8", errors="ignore").read()[:limit]
        except Exception:
            return ""
    return ""


def hospital_knowledge_digest(engine, ctx, max_chars=12000):
    """이 병원이 '실제로 보유한' 지식 요약 — 기획안이 우리 근거·원장 전문성을 반영하도록.
    우선순위: KB(원장프로필+논문근거) → corpus(업로드 자료 추출텍스트) → 자료 파일명(PG, 영속).
    아무것도 없으면 ''(형식만 참고)."""
    with _conn(engine, ctx) as cn:
        slug = cn.execute(text("select slug from hospitals where id=:h"),
                          {"h": ctx.hospital_id}).scalar()
        names = [r[0] for r in cn.execute(text(
            "select filename from materials where hospital_id=:h order by filename"),
            {"h": ctx.hospital_id})]
    parts = []
    prof = _kb_read(slug, "profile.json", 4000) if slug else ""
    evid = _kb_read(slug, "evidence.json", 6000) if slug else ""
    if prof:
        parts.append("[원장 프로필·화법]\n" + prof)
    if evid:
        parts.append("[우리 논문 근거]\n" + evid)
    if not parts and slug:                      # KB 없으면 업로드 자료 추출텍스트(disk corpus)
        try:
            from llm.runner import corpus_text
            corp = corpus_text(slug, max_chars=8000)
            if corp:
                parts.append("[업로드 자료(발췌)]\n" + corp)
        except Exception:
            pass
    if not parts and names:                      # 그래도 없으면 PG 원본에서 직접 추출(Render 영속 보장)
        extracted = _extract_materials_text(engine, ctx, names, budget=8000)
        if extracted:
            parts.append("[업로드 자료(원본 추출)]\n" + extracted)
    if names:                                    # 최소한: 어떤 자료를 갖고 있는지(항상 영속)
        parts.append("[보유 자료 목록]\n" + ", ".join(names[:40]))
    digest = "\n\n".join(parts)
    return digest[:max_chars]


# 논문·설문·인터뷰·대본류를 우선 추출(기획에 더 유용)
_MAT_PRIORITY = ("논문", "paper", "근거", "evidence", "설문", "인터뷰", "대본", "강의", "profile")
_MAT_EXT = (".pdf", ".docx", ".txt", ".md", ".hwp", ".pptx", ".csv")

def _extract_materials_text(engine, ctx, names, budget=8000, max_files=6):
    """PG 원본 자료에서 텍스트 추출(pdftotext 등). KB/corpus 없는 Render에서도 실제 내용 반영.
    우선순위 파일명 먼저, 소수·글자예산 내로만(요청 지연·비용 방지)."""
    import tempfile
    from store.materials import get_material
    from ingest.extract import extract_one
    def _rank(n):
        low = n.lower()
        return (0 if any(k in low for k in _MAT_PRIORITY) else 1, n)
    cand = [n for n in names if n.lower().endswith(_MAT_EXT)]
    cand.sort(key=_rank)
    out, used = [], 0
    for n in cand[:max_files]:
        try:
            mime, data = get_material(engine, ctx.hospital_id, n)
            if not data or len(data) > 25 * 1024 * 1024:      # 너무 큰 파일 skip
                continue
            ext = "." + n.lower().rsplit(".", 1)[-1]
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
                tf.write(data); tmp = tf.name
            try:
                txt = extract_one(tmp) or ""
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            txt = txt.strip()
            if txt and not txt.startswith("["):               # 파싱실패 마커 제외
                chunk = f"— {n} —\n{txt}"
                out.append(chunk[:budget - used])
                used += min(len(chunk), budget - used)
            if used >= budget:
                break
        except Exception:
            continue
    return "\n\n".join(out)


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


def add_videos_by_topic(engine, ctx, project_id, topic, count=3, searcher=None, fetcher=None):
    """주제 입력 → 인기 영상 자동 검색·등록(수동 URL 붙여넣기 대신). 반환: {added,titles,no_api,topic}.
    키 없으면 added=0·no_api=True. searcher/fetcher 주입형(테스트 비용 0)."""
    permissions.require(ctx, BENCHMARK_ROLES)
    pid = _uuid(project_id)
    topic = (topic or "").strip()
    if not topic:
        raise ServiceError("검색할 주제가 필요합니다")
    count = max(1, min(int(count or 3), 5))
    searcher = searcher or youtube_meta.search
    with _conn(engine, ctx) as cn:
        if not cn.execute(text("select 1 from benchmark_projects where id=:p and hospital_id=:h"),
                          {"p": pid, "h": ctx.hospital_id}).first():
            raise NotFound("벤치마킹 프로젝트를 찾을 수 없습니다")
    results = searcher(topic, want=count) or []
    if not results:
        return {"added": 0, "no_api": not youtube_meta.enabled(), "topic": topic, "titles": []}
    for r in results:
        if r.get("url"):
            add_video(engine, ctx, pid, r["url"])
    fetch_metadata(engine, ctx, pid, fetcher=fetcher)      # 검색된 영상 메타 채우기
    return {"added": len(results), "no_api": False, "topic": topic,
            "titles": [r.get("title") for r in results]}


def remove_video(engine, ctx, project_id, video_ref):
    """프로젝트에서 영상 삭제(자막·분석도 CASCADE 삭제). 반환: {removed}."""
    permissions.require(ctx, BENCHMARK_ROLES)
    pid, ref = _uuid(project_id), _uuid(video_ref)
    with _conn(engine, ctx) as cn:
        n = cn.execute(text(
            "delete from youtube_videos where id=:r and project_id=:p and hospital_id=:h"),
            {"r": ref, "p": pid, "h": ctx.hospital_id}).rowcount
    if not n:
        raise NotFound("영상을 찾을 수 없습니다")
    return {"removed": n}


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


# ── 영상별 벤치마크 분석(C4) ──
def analyze_video(engine, ctx, video_ref, model=None, generator=None):
    """자막(available) + 메타 → 구조화 분석(jsonb) 저장. 의학주장은 '관찰'로만 기록(승격 금지).
    generator 주입 시 테스트에서 LLM 없이 검증(기본 runner.generate, 저렴 모델 MODEL_KB)."""
    permissions.require(ctx, BENCHMARK_ROLES)
    ref = _uuid(video_ref)
    with _conn(engine, ctx) as cn:
        v = cn.execute(text(
            "select id, project_id, url, title, channel_name, view_count, like_count, "
            "  comment_count, subscriber_count, duration, published_at "
            "from youtube_videos where id=:r and hospital_id=:h"),
            {"r": ref, "h": ctx.hospital_id}).first()
        if not v:
            raise NotFound("영상을 찾을 수 없습니다")
        tr = cn.execute(text(
            "select normalized_text from youtube_transcripts "
            "where video_ref=:r and status='available' and normalized_text is not null "
            "order by created_at desc limit 1"), {"r": ref}).first()
    if not tr or not (tr.normalized_text or "").strip():
        raise ServiceError("분석하려면 먼저 available 상태의 자막이 필요합니다")

    from llm import runner
    gen = generator or runner.generate
    mdl = model or runner.MODEL_KB
    system = runner.load_prompt("benchmark_analyze.md")
    meta = (f"제목: {v.title or '-'} | 채널: {v.channel_name or '-'} | 조회수: {v.view_count or '-'} | "
            f"좋아요: {v.like_count or '-'} | 구독자: {v.subscriber_count or '-'} | "
            f"길이: {v.duration or '-'} | URL: {v.url}")
    user = f"[영상 메타데이터]\n{meta}\n\n[자막 전문]\n{tr.normalized_text}"
    analysis = gen(system, user, parse_json=True, model=mdl, label="벤치분석", max_tokens=8000, cache=True)
    if not isinstance(analysis, dict):
        raise ServiceError("분석 결과가 올바른 JSON이 아닙니다")
    chash = hashlib.sha256((tr.normalized_text or "").encode("utf-8")).hexdigest()
    with _conn(engine, ctx) as cn:
        aid = cn.execute(text(
            "insert into benchmark_analyses(hospital_id,project_id,video_ref,analysis,model,prompt_version,content_hash) "
            "values(:h,:p,:r,cast(:a as jsonb),:m,:pv,:ch) returning id"),
            {"h": ctx.hospital_id, "p": v.project_id, "r": ref,
             "a": json.dumps(analysis, ensure_ascii=False), "m": mdl,
             "pv": ANALYZE_PROMPT_VERSION, "ch": chash}).scalar()
    return {"analysis_id": str(aid), "video_ref": str(ref), "analysis": analysis}


def list_analyses(engine, ctx, project_id):
    """프로젝트의 영상별 최신 분석 목록(영상당 1건, 최신)."""
    permissions.require(ctx, BENCHMARK_ROLES)
    pid = _uuid(project_id)
    with _conn(engine, ctx) as cn:
        rows = cn.execute(text(
            "select distinct on (a.video_ref) a.id, a.video_ref, a.analysis, a.model, a.created_at, "
            "  v.title, v.url "
            "from benchmark_analyses a join youtube_videos v on v.id=a.video_ref "
            "where a.project_id=:p and a.hospital_id=:h "
            "order by a.video_ref, a.created_at desc"), {"p": pid, "h": ctx.hospital_id}).all()
    return [{"analysis_id": str(r.id), "video_ref": str(r.video_ref), "title": r.title,
             "url": r.url, "model": r.model, "created_at": r.created_at.isoformat(),
             "analysis": r.analysis} for r in rows]


# ── 교차 종합(C5) ──
def synthesize_project(engine, ctx, project_id, model=None, generator=None):
    """프로젝트의 영상별 분석들을 교차 비교 → 흥행공식/차별화기회/검증대상 주장 종합(jsonb) 저장.
    분석이 최소 1건 필요. generator 주입형(테스트 LLM 비용 0)."""
    permissions.require(ctx, BENCHMARK_ROLES)
    pid = _uuid(project_id)
    analyses = list_analyses(engine, ctx, project_id)
    if not analyses:
        raise ServiceError("교차 종합하려면 먼저 영상 분석(1건 이상)이 필요합니다")

    from llm import runner
    gen = generator or runner.generate
    mdl = model or runner.MODEL_KB
    system = runner.load_prompt("benchmark_synthesize.md")
    payload = [{"title": a["title"], "url": a["url"], "analysis": a["analysis"]} for a in analyses]
    user = f"[영상 {len(payload)}편 분석 결과(JSON)]\n" + json.dumps(payload, ensure_ascii=False)
    synthesis = gen(system, user, parse_json=True, model=mdl, label="교차종합", max_tokens=8000, cache=True)
    if not isinstance(synthesis, dict):
        raise ServiceError("종합 결과가 올바른 JSON이 아닙니다")
    chash = hashlib.sha256(user.encode("utf-8")).hexdigest()
    with _conn(engine, ctx) as cn:
        sid = cn.execute(text(
            "insert into cross_syntheses(hospital_id,project_id,synthesis,model,prompt_version,content_hash) "
            "values(:h,:p,cast(:s as jsonb),:m,:pv,:ch) returning id"),
            {"h": ctx.hospital_id, "p": pid, "s": json.dumps(synthesis, ensure_ascii=False),
             "m": mdl, "pv": SYNTH_PROMPT_VERSION, "ch": chash}).scalar()
        cn.execute(text("update benchmark_projects set status='analyzing', updated_at=now() "
                        "where id=:p and hospital_id=:h and status='draft'"),
                   {"p": pid, "h": ctx.hospital_id})
    return {"synthesis_id": str(sid), "video_count": len(payload), "synthesis": synthesis}


def get_latest_synthesis(engine, ctx, project_id):
    """프로젝트 최신 교차 종합. 없으면 None."""
    permissions.require(ctx, BENCHMARK_ROLES)
    pid = _uuid(project_id)
    with _conn(engine, ctx) as cn:
        r = cn.execute(text(
            "select id, synthesis, model, created_at from cross_syntheses "
            "where project_id=:p and hospital_id=:h order by created_at desc limit 1"),
            {"p": pid, "h": ctx.hospital_id}).first()
    if not r:
        return None
    return {"synthesis_id": str(r.id), "model": r.model,
            "created_at": r.created_at.isoformat(), "synthesis": r.synthesis}


# ── 유튜브 의학주장 후보(C6) ──
def extract_claim_candidates(engine, ctx, project_id):
    """최신 교차 종합의 claims_to_verify → yt_claim_candidates(status=pending_verification) 적재.
    evidence 자동승격 금지: 상태는 무조건 검증 전, linked_claim_card_id는 null(Phase 2에서 연결).
    claim_text 기준 dedup(재실행 안전). 반환: {inserted,skipped,total}."""
    permissions.require(ctx, BENCHMARK_ROLES)
    pid = _uuid(project_id)
    syn = get_latest_synthesis(engine, ctx, project_id)
    if not syn:
        raise ServiceError("주장 후보를 뽑으려면 먼저 교차 종합이 필요합니다")
    claims = (syn.get("synthesis") or {}).get("claims_to_verify") or []
    inserted = skipped = 0
    with _conn(engine, ctx) as cn:
        existing = {r[0] for r in cn.execute(text(
            "select claim_text from yt_claim_candidates where project_id=:p and hospital_id=:h"),
            {"p": pid, "h": ctx.hospital_id})}
        for c in claims:
            ct = (c.get("claim_text") or "").strip()
            if not ct or ct in existing:
                skipped += 1
                continue
            cn.execute(text(
                "insert into yt_claim_candidates(hospital_id,project_id,claim_text,claim_type,"
                "population,condition,intervention,comparator,outcome,numeric_value) "
                "values(:h,:p,:ct,:cty,:po,:co,:iv,:cm,:ou,:nv)"),
                {"h": ctx.hospital_id, "p": pid, "ct": ct, "cty": c.get("claim_type"),
                 "po": c.get("population"), "co": c.get("condition"), "iv": c.get("intervention"),
                 "cm": c.get("comparator"), "ou": c.get("outcome"), "nv": c.get("numeric_value")})
            existing.add(ct)
            inserted += 1
    return {"inserted": inserted, "skipped": skipped, "total": len(claims)}


def list_claim_candidates(engine, ctx, project_id):
    permissions.require(ctx, BENCHMARK_ROLES)
    pid = _uuid(project_id)
    with _conn(engine, ctx) as cn:
        rows = cn.execute(text(
            "select id, claim_text, claim_type, population, condition, intervention, comparator, "
            "  outcome, numeric_value, status, linked_claim_card_id, created_at "
            "from yt_claim_candidates where project_id=:p and hospital_id=:h order by created_at"),
            {"p": pid, "h": ctx.hospital_id}).all()
    return [{"id": str(r.id), "claim_text": r.claim_text, "claim_type": r.claim_type,
             "population": r.population, "condition": r.condition, "intervention": r.intervention,
             "comparator": r.comparator, "outcome": r.outcome, "numeric_value": r.numeric_value,
             "status": r.status, "linked_claim_card_id": str(r.linked_claim_card_id) if r.linked_claim_card_id else None,
             "created_at": r.created_at.isoformat()} for r in rows]


# ── 기획안 artifact + 승인(C7) ──
def generate_plan(engine, ctx, project_id, direction=None, model=None, generator=None):
    """최신 종합 + 후보주장 + gaps (+ 운영자 방향) → 기획안(형식 설계) 생성. status=draft로 저장(승인 대기).
    의학 사실은 여기서 확정 안 함(unverified_claims로 분리). direction=운영자가 원하는 주제·방향(선택)."""
    permissions.require(ctx, BENCHMARK_ROLES)
    pid = _uuid(project_id)
    syn = get_latest_synthesis(engine, ctx, project_id)
    if not syn:
        raise ServiceError("기획안 전에 교차 종합이 필요합니다")
    candidates = list_claim_candidates(engine, ctx, project_id)

    from llm import runner
    gen = generator or runner.generate
    mdl = model or runner.MODEL_KB
    system = runner.load_prompt("benchmark_plan.md")
    ctx_payload = {"synthesis": syn.get("synthesis"),
                   "claim_candidates": [{"claim_text": c["claim_text"], "claim_type": c["claim_type"],
                                         "status": c["status"]} for c in candidates]}
    digest = hospital_knowledge_digest(engine, ctx)     # 우리 병원 실제 보유 자료(원장·논문·업로드)
    our = ("\n\n[우리 병원 보유 자료 — 이걸로 차별화·검증가능 여부 판단]\n" + digest) if digest \
        else "\n\n[우리 병원 보유 자료]\n(없음 — 경쟁 영상의 '형식'만 참고하고, 의학 내용은 추후 근거검증)"
    direction = (direction or "").strip()
    steer = (f"\n\n[운영자 요청 방향 — 최우선 반영]\n{direction}") if direction else ""
    user = "[교차 종합 + 검증대상 주장(JSON)]\n" + json.dumps(ctx_payload, ensure_ascii=False) + our + steer
    plan = gen(system, user, parse_json=True, model=mdl, label="기획안", max_tokens=8000, cache=True)
    if not isinstance(plan, dict):
        raise ServiceError("기획안 결과가 올바른 JSON이 아닙니다")
    chash = hashlib.sha256(user.encode("utf-8")).hexdigest()
    with _conn(engine, ctx) as cn:
        plan_id = cn.execute(text(
            "insert into content_plans(hospital_id,project_id,plan,status,model,prompt_version,"
            "content_hash,created_by_membership_id) "
            "values(:h,:p,cast(:pl as jsonb),'draft',:m,:pv,:ch,:mb) returning id"),
            {"h": ctx.hospital_id, "p": pid, "pl": json.dumps(plan, ensure_ascii=False),
             "m": mdl, "pv": PLAN_PROMPT_VERSION, "ch": chash, "mb": ctx.membership_id}).scalar()
        cn.execute(text("update benchmark_projects set status='planned', updated_at=now() "
                        "where id=:p and hospital_id=:h and status in ('draft','analyzing')"),
                   {"p": pid, "h": ctx.hospital_id})
    return {"plan_id": str(plan_id), "status": "draft", "plan": plan}


def approve_plan(engine, ctx, plan_id):
    """기획안 승인(기존 script 승인과 분리된 게이트). approver/admin만. draft만 승인 가능."""
    permissions.require(ctx, permissions.REVIEW_ROLES)
    return _decide_plan(engine, ctx, plan_id, "approved")


def reject_plan(engine, ctx, plan_id):
    """기획안 반려. approver/admin만. draft만 반려 가능."""
    permissions.require(ctx, permissions.REVIEW_ROLES)
    return _decide_plan(engine, ctx, plan_id, "rejected")


def _decide_plan(engine, ctx, plan_id, new_status):
    pid = _uuid(plan_id)
    approved = new_status == "approved"
    with _conn(engine, ctx) as cn:
        cur = cn.execute(text(
            "select status from content_plans where id=:i and hospital_id=:h"),
            {"i": pid, "h": ctx.hospital_id}).first()
        if not cur:
            raise NotFound("기획안을 찾을 수 없습니다")
        if cur.status != "draft":
            raise InvalidStateTransition(f"draft 상태만 처리 가능합니다(현재: {cur.status})")
        cn.execute(text(
            "update content_plans set status=:s, updated_at=now(), "
            "approved_by_membership_id=:mb, approved_at=(case when :ap then now() else null end) "
            "where id=:i and hospital_id=:h"),
            {"s": new_status, "mb": ctx.membership_id, "ap": approved, "i": pid, "h": ctx.hospital_id})
    return {"plan_id": str(pid), "status": new_status}


def get_plan(engine, ctx, plan_id):
    permissions.require(ctx, BENCHMARK_ROLES)
    pid = _uuid(plan_id)
    with _conn(engine, ctx) as cn:
        r = cn.execute(text(
            "select id, project_id, plan, status, script_version_id, model, created_at, updated_at "
            "from content_plans where id=:i and hospital_id=:h"),
            {"i": pid, "h": ctx.hospital_id}).first()
    if not r:
        raise NotFound("기획안을 찾을 수 없습니다")
    return {"plan_id": str(r.id), "project_id": str(r.project_id), "status": r.status,
            "script_version_id": str(r.script_version_id) if r.script_version_id else None,
            "model": r.model, "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat(), "plan": r.plan}


def list_plans(engine, ctx, project_id):
    permissions.require(ctx, BENCHMARK_ROLES)
    pid = _uuid(project_id)
    with _conn(engine, ctx) as cn:
        rows = cn.execute(text(
            "select id, status, script_version_id, created_at from content_plans "
            "where project_id=:p and hospital_id=:h order by created_at desc"),
            {"p": pid, "h": ctx.hospital_id}).all()
    return [{"plan_id": str(r.id), "status": r.status,
             "script_version_id": str(r.script_version_id) if r.script_version_id else None,
             "created_at": r.created_at.isoformat()} for r in rows]


# ── 대본 생성 브릿지(C8) — 기존 생성 로직 무손상 ──
def _plan_brief_text(plan):
    """승인된 기획안(jsonb) → 생성기(director)에 넣을 한국어 브리핑 텍스트.
    형식(구성·각도·훅)만 지시하고, 의학 주장은 '검증 필요'로 명시(사실로 쓰지 말 것)."""
    plan = plan or {}
    L = []
    L.append("[벤치마킹 기획 브리핑 — 형식·톤 참고용]")
    fmt = plan.get("format"); tone = plan.get("tone")
    if fmt or tone:
        L.append(f"★ 포맷: {fmt or '-'} / 톤: {tone or '-'} — 이 포맷·톤을 지켜라. "
                 "가벼운 톤이면 가볍고 생활밀착하게, 논문 설명·전문 심화로 흐르지 말 것.")
    if plan.get("topic"): L.append(f"주제: {plan['topic']}")
    if plan.get("angle"): L.append(f"차별화 각도: {plan['angle']}")
    if plan.get("why_now"): L.append(f"기획 이유: {plan['why_now']}")
    if plan.get("target_audience"): L.append(f"타깃: {plan['target_audience']}")
    if plan.get("hook"): L.append(f"훅 아이디어: {plan['hook']}")
    if plan.get("narration_style"): L.append(f"화법: {plan['narration_style']}")
    outline = plan.get("outline") or []
    if outline:
        L.append("구성(형식 참고 — 우리 표현/근거로 채울 것):")
        for i, o in enumerate(outline, 1):
            L.append(f"  {i}. {o.get('section','')} — {o.get('beat','')}"
                     + (f" ({o['est_ratio']})" if o.get("est_ratio") else ""))
    if plan.get("cta"): L.append(f"마무리 CTA: {plan['cta']}")
    uc = plan.get("unverified_claims") or []
    if uc:
        L.append("※ 아래는 '검증 대상' 주장 — 논문 근거 검증을 통과하기 전엔 대본에 '사실'로 쓰지 말 것:")
        for c in uc:
            L.append(f"  - (미검증) {c.get('claim_text','')}")
    sg = plan.get("similarity_guard") or []
    if sg:
        L.append("표절 방지(원본과 겹치지 않게):")
        for s in sg:
            L.append(f"  - {s}")
    L.append("※ 위 브리핑은 '구성/각도' 참고용이며, 의학 내용·표현은 우리 자료와 근거검증을 따른다.")
    return "\n".join(L)


def build_generation_brief(engine, ctx, plan_id):
    """승인된 기획안 → 생성 브리핑. draft/rejected면 거부(생성 전 승인 게이트 강제).
    반환: {plan_id, topic, brief_text}. 생성 자체는 기존 파이프라인이 수행(무손상)."""
    permissions.require(ctx, BENCHMARK_ROLES)
    p = get_plan(engine, ctx, plan_id)
    if p["status"] != "approved":
        raise InvalidStateTransition(f"승인된 기획안만 생성에 쓸 수 있습니다(현재: {p['status']})")
    plan = p["plan"] or {}
    return {"plan_id": p["plan_id"], "project_id": p["project_id"],
            "topic": plan.get("topic") or "", "brief_text": _plan_brief_text(plan)}


def link_script_version(engine, ctx, plan_id, script_version_id):
    """생성된 대본 버전을 기획안에 역링크(브릿지) + 프로젝트 →scripted. 승인된 기획안만."""
    permissions.require(ctx, BENCHMARK_ROLES)
    pid = _uuid(plan_id)
    svid = _uuid(script_version_id)
    with _conn(engine, ctx) as cn:
        r = cn.execute(text("select project_id, status from content_plans where id=:i and hospital_id=:h"),
                       {"i": pid, "h": ctx.hospital_id}).first()
        if not r:
            raise NotFound("기획안을 찾을 수 없습니다")
        if r.status != "approved":
            raise InvalidStateTransition(f"승인된 기획안만 대본과 연결할 수 있습니다(현재: {r.status})")
        cn.execute(text("update content_plans set script_version_id=:v, updated_at=now() "
                        "where id=:i and hospital_id=:h"),
                   {"v": svid, "i": pid, "h": ctx.hospital_id})
        cn.execute(text("update benchmark_projects set status='scripted', updated_at=now() "
                        "where id=:p and hospital_id=:h"), {"p": r.project_id, "h": ctx.hospital_id})
    return {"plan_id": str(pid), "script_version_id": str(svid), "project_status": "scripted"}


# ── 원본 유사도(표절) 검사(C9) ──
SIMILARITY_PROMPT_VERSION = "sr-1"

def check_similarity(engine, ctx, project_id, script_text, script_version_id=None,
                     model=None, generator=None):
    """생성 대본 vs 프로젝트 원본 자막들. 축자(결정론) + 선택적 의미/사례/구조(LLM).
    risk(low/medium/high) 산출 후 similarity_reports 저장. generator 없으면 축자만으로 판정."""
    permissions.require(ctx, BENCHMARK_ROLES)
    pid = _uuid(project_id)
    if not (script_text or "").strip():
        raise ServiceError("검사할 대본 텍스트가 필요합니다")
    with _conn(engine, ctx) as cn:
        srcs = cn.execute(text(
            "select v.title, t.normalized_text from youtube_transcripts t "
            "join youtube_videos v on v.id=t.video_ref "
            "where v.project_id=:p and t.hospital_id=:h and t.status='available' "
            "and t.normalized_text is not null"),
            {"p": pid, "h": ctx.hospital_id}).all()
    sources = [(r.title or "원본", r.normalized_text) for r in srcs if (r.normalized_text or "").strip()]
    if not sources:
        raise ServiceError("비교할 원본 자막(available)이 없습니다")

    from services import similarity as sim
    per_source, worst = [], {"verbatim_score": 0.0, "longest_run_words": 0, "shingle_jaccard": 0.0}
    for title, txt in sources:
        vo = sim.verbatim_overlap(script_text, txt)
        per_source.append({"source": title, **vo})
        if vo["verbatim_score"] > worst["verbatim_score"]:
            worst = vo

    semantic_score, example_overlaps, structure_note, flagged, llm_notes = 0.0, [], None, [], None
    if generator:
        from llm import runner
        mdl = model or runner.MODEL_KB
        system = runner.load_prompt("benchmark_similarity.md")
        joined = "\n\n---\n\n".join(f"[원본: {t}]\n{x}" for t, x in sources)
        user = f"[우리 대본]\n{script_text}\n\n[원본 자막들]\n{joined}"
        res = generator(system, user, parse_json=True, model=mdl, label="유사도", max_tokens=4000)
        if isinstance(res, dict):
            semantic_score = float(res.get("semantic_score") or 0)
            example_overlaps = res.get("example_overlaps") or []
            structure_note = res.get("structure_note")
            flagged = res.get("flagged") or []
            llm_notes = res.get("notes")

    risk = sim.risk_level(worst["verbatim_score"], worst["longest_run_words"], semantic_score)
    report = {
        "verbatim": {"worst": worst, "per_source": per_source},
        "semantic_score": semantic_score, "example_overlaps": example_overlaps,
        "structure_note": structure_note, "flagged": flagged, "notes": llm_notes,
        "source_count": len(sources), "llm_used": bool(generator),
    }
    with _conn(engine, ctx) as cn:
        rid = cn.execute(text(
            "insert into similarity_reports(hospital_id,project_id,script_version_id,report,risk) "
            "values(:h,:p,:v,cast(:r as jsonb),:rk) returning id"),
            {"h": ctx.hospital_id, "p": pid,
             "v": _uuid(script_version_id) if script_version_id else None,
             "r": json.dumps(report, ensure_ascii=False), "rk": risk}).scalar()
    return {"report_id": str(rid), "risk": risk, "report": report}


def list_recent_scripts(engine, ctx, limit=15):
    """이 병원의 최근 대본 버전 목록(유사도 검사 대상 선택용)."""
    permissions.require(ctx, BENCHMARK_ROLES)
    with _conn(engine, ctx) as cn:
        rows = cn.execute(text(
            "select v.id, s.topic, v.version_no, v.created_at, (s.current_version_id=v.id) as is_current "
            "from script_versions v join scripts s "
            "  on s.id=v.script_id and s.hospital_id=v.hospital_id "
            "where v.hospital_id=:h order by v.created_at desc limit :l"),
            {"h": ctx.hospital_id, "l": limit}).all()
    return [{"version_id": str(r.id), "topic": r.topic, "version_no": r.version_no,
             "is_current": bool(r.is_current), "created_at": r.created_at.isoformat()} for r in rows]


def _version_text(engine, ctx, version_id):
    with _conn(engine, ctx) as cn:
        rows = cn.execute(text(
            "select text from script_blocks where hospital_id=:h and version_id=:v order by order_index"),
            {"h": ctx.hospital_id, "v": _uuid(version_id)}).all()
    return "\n".join((r.text or "") for r in rows).strip()


def check_similarity_version(engine, ctx, project_id, version_id, use_llm=False):
    """생성된 대본 버전을 DB에서 바로 가져와 유사도 검사(붙여넣기 불필요)."""
    permissions.require(ctx, BENCHMARK_ROLES)
    txt = _version_text(engine, ctx, version_id)
    if not txt:
        raise ServiceError("그 대본에서 텍스트를 찾지 못했어요(빈 버전일 수 있어요)")
    gen = None
    if use_llm:
        from llm import runner
        gen = runner.generate
    return check_similarity(engine, ctx, project_id, txt, script_version_id=version_id, generator=gen)


def latest_similarity_report(engine, ctx, project_id):
    """이 프로젝트의 가장 최근 유사도 결과(화면 표시용). 없으면 None."""
    permissions.require(ctx, BENCHMARK_ROLES)
    pid = _uuid(project_id)
    with _conn(engine, ctx) as cn:
        r = cn.execute(text(
            "select report, risk, script_version_id, created_at from similarity_reports "
            "where project_id=:p and hospital_id=:h order by created_at desc limit 1"),
            {"p": pid, "h": ctx.hospital_id}).first()
    if not r:
        return None
    return {"risk": r.risk, "report": r.report,
            "script_version_id": str(r.script_version_id) if r.script_version_id else None,
            "created_at": r.created_at.isoformat()}
