"""Supadata 자동 자막 수집 오케스트레이션(스펙 4·6·7·8·11·12·14절).

흐름: 자동호출 전 게이트(활성·키·quota·기존자막·진행중job) → provider 호출 → 상태저장 + 사용량 기록(멱등)
      → observability. 실패·소진은 전체 장애로 이어지지 않고 수동 fallback로 분기.
route는 이 service만 호출(직접 SQL·상태전이 금지). RLS는 tenant_conn이 강제.
"""
import uuid
import hashlib
from sqlalchemy import text
from store.repositories import tenant_conn
from services import permissions
from services.exceptions import NotFound
from services import supadata
from services.supadata import SupadataConfig, SupadataTranscriptProvider
from store import transcript_usage as usage

BENCHMARK_ROLES = {"editor", "approver", "admin", "platform_operator"}


def _hh(hospital_id):
    return hashlib.sha256(("h:" + str(hospital_id)).encode()).hexdigest()[:12]

def _emit(event, **f):
    try:
        from services.observability import emit
        emit(event, **f)
    except Exception:
        pass

def _conn(engine, ctx):
    return tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id, request_id=ctx.request_id)

def _uuid(v):
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


def _save_row(engine, ctx, video_ref, *, provider, status, text_=None, lang=None,
              has_ts=False, source_note=None, job_id=None, credits=None, available_langs=None):
    thash = hashlib.sha256((text_ or "").encode("utf-8")).hexdigest() if text_ else None
    fetched = "now()" if status == "available" else "null"
    import json as _json
    with _conn(engine, ctx) as cn:
        cn.execute(text(
            f"insert into youtube_transcripts(hospital_id,video_ref,provider,status,lang,has_timestamps,"
            f"normalized_text,source_note,char_count,provider_job_id,credits_used,transcript_hash,"
            f"available_languages,fetched_at) "
            f"values(:h,:r,:p,:s,:l,:ts,:nt,:sn,:cc,:jid,:cu,:th,cast(:al as jsonb),{fetched})"),
            {"h": ctx.hospital_id, "r": _uuid(video_ref), "p": provider, "s": status, "l": lang,
             "ts": has_ts, "nt": (text_ or None), "sn": source_note, "cc": len(text_ or ""),
             "jid": job_id, "cu": credits, "th": thash,
             "al": _json.dumps(available_langs or [], ensure_ascii=False)})


def _video(engine, ctx, video_ref):
    with _conn(engine, ctx) as cn:
        v = cn.execute(text(
            "select id, project_id, url, duration from youtube_videos where id=:r and hospital_id=:h"),
            {"r": _uuid(video_ref), "h": ctx.hospital_id}).first()
        if not v:
            raise NotFound("영상을 찾을 수 없습니다")
        latest = cn.execute(text(
            "select status, provider_job_id from youtube_transcripts where video_ref=:r "
            "order by created_at desc limit 1"), {"r": _uuid(video_ref)}).first()
    return v, latest


def auto_collect(engine, ctx, video_ref, provider=None):
    """Supadata 자동 자막 수집. 반환 dict(status·message·credits·job_id·reused). provider 주입 시 테스트.
    비활성/기존자막/진행중job/quota소진은 provider 호출 없이 분기(비용 보호)."""
    permissions.require(ctx, BENCHMARK_ROLES)
    if not supadata.enabled():
        return {"status": "disabled"}   # 호출부가 기존 수동 흐름으로

    v, latest = _video(engine, ctx, video_ref)
    # 기존 available → 재사용(호출 안 함)
    if latest and latest.status == "available":
        return {"status": "available", "reused": True}
    # 진행 중 전사 job → 그대로(중복 요청 금지)
    if latest and latest.status == "transcribing" and latest.provider_job_id:
        return {"status": "transcribing", "job_id": latest.provider_job_id, "reused": True}

    hh = _hh(ctx.hospital_id)
    qs = usage.quota_status(engine)
    if qs["exhausted"]:
        _save_row(engine, ctx, video_ref, provider="supadata", status="quota_exhausted",
                  source_note="월간 크레딧 소진")
        _emit("transcript_quota_exhausted", hospital=hh, provider="supadata",
              monthly_credits_used=qs["used"], monthly_credit_limit=qs["limit"])
        return {"status": "quota_exhausted", "message": supadata.user_message("quota_exhausted")}
    if qs["warning"]:
        _emit("transcript_quota_warning", hospital=hh, provider="supadata",
              monthly_credits_used=qs["used"], monthly_credit_limit=qs["limit"])

    mode = SupadataConfig.transcript_mode()
    prov = provider or SupadataTranscriptProvider()
    req_id = uuid.uuid4().hex
    _emit("transcript_fetch_started", hospital=hh, provider="supadata", mode=mode, request_id=req_id)
    res = prov.request_transcript(v.url, preferred_language="ko", mode=mode)

    # 사용량 기록(멱등) — credits 없으면 0(실패). available/transcribing은 아래서 실제값 반영
    credits = res.credits_used or 0
    usage.record_usage(engine, ctx.hospital_id, request_id=req_id, operation="transcript_fetch",
                       mode=mode, status=res.status, credits_used=credits,
                       credits_estimated=res.credits_estimated, response_status=res.raw_status_code,
                       project_id=v.project_id, benchmark_video_id=v.id, provider_job_id=res.provider_job_id)

    if res.status == "available":
        _save_row(engine, ctx, video_ref, provider="supadata", status="available",
                  text_=res.transcript_text, lang=res.language, has_ts=bool(res.segments),
                  source_note="Supadata 자동 수집", credits=credits, available_langs=res.available_languages)
        _emit("transcript_fetch_succeeded", hospital=hh, provider="supadata", credits_used=credits,
              monthly_credits_used=qs["used"] + credits, monthly_credit_limit=qs["limit"])
        return {"status": "available", "credits_used": credits}

    if res.status == "transcribing":
        # native 모드에선 AI 전사 자동 실행 안 함(비용 보호) — 사용자에게 선택 넘김
        _save_row(engine, ctx, video_ref, provider="supadata", status="transcribing",
                  source_note="음성 자동 변환 중", job_id=res.provider_job_id)
        _emit("transcript_generation_started", hospital=hh, provider="supadata",
              request_id=req_id, provider_status=res.raw_status_code)
        return {"status": "transcribing", "job_id": res.provider_job_id,
                "message": "이 영상은 기존 자막이 없어 음성 자동 변환이 필요합니다."}

    # 실패·소진·설정오류 → 상태 저장 + 수동 fallback 안내
    _save_row(engine, ctx, video_ref, provider="supadata", status=res.status,
              source_note=(res.error_message or res.error_code))
    evt = {"quota_exhausted": "transcript_quota_exhausted", "manual_required": "transcript_manual_required"}\
        .get(res.status, "transcript_fetch_failed")
    _emit(evt, hospital=hh, provider="supadata", status=res.status, failure_code=res.error_code,
          provider_status=res.raw_status_code, request_id=req_id)
    return {"status": res.status, "message": res.error_message or supadata.user_message(res.error_code)}
