"""벤치마킹 service — 프로젝트/영상/자막 업무규칙·권한·트랜잭션 경계(개발지침).

 - route는 이 service만 호출(직접 SQL·상태전이·RLS 우회 금지).
 - 권한은 permissions로 검증, 테넌트 격리는 tenant_conn(RLS)이 강제.
 - 메타/자막 수집은 provider(youtube_meta·transcripts) 주입 → 테스트에서 mock 가능.
 - 원본 자막 ≠ 분석결과: 여기선 수집/저장까지(분석은 C4~).
"""
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
def generate_plan(engine, ctx, project_id, model=None, generator=None):
    """최신 종합 + 후보주장 + gaps → 기획안(형식 설계) 생성. status=draft로 저장(승인 대기).
    의학 사실은 여기서 확정 안 함(unverified_claims로 분리). generator 주입형."""
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
    user = "[교차 종합 + 검증대상 주장(JSON)]\n" + json.dumps(ctx_payload, ensure_ascii=False)
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
