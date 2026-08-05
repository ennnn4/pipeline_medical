"""Supadata 자동 자막 수집 provider — 교체 가능 구조(스펙 3·4·7·8·13절).

원칙:
 - Supadata를 route에 하드코딩하지 않음. TranscriptProvider 계약(services/transcripts.py와 동일 계열).
 - API 키·자막 원문·원시 오류 body는 로그/HTML/클라이언트에 노출 금지. 서버에서만 호출.
 - 키 없거나 SUPADATA_PROVIDER_ENABLED!=true 이면 enabled()=False → 호출 안 함(현재 수동 흐름 무손상).
 - 무료 한도는 '월 100 credits' 기준(영상 개수 아님). 코드에 100 하드코딩 금지 → 환경변수.
 - 상태/오류를 명시적 코드로 매핑(HTTP stack trace 비노출).
"""
import os
import math
import re
from dataclasses import dataclass, field


# ── 운영 설정(환경변수 — 유료 업그레이드 시 재배포 없이 값만 변경) ──
def _env(name, default=None):
    v = os.environ.get(name)
    return v if v is not None and v != "" else default

def _flag(name, default=False):
    return str(_env(name, "true" if default else "false")).strip().lower() in ("1", "true", "yes", "on")


class SupadataConfig:
    @staticmethod
    def api_key():
        return _env("SUPADATA_API_KEY")

    @staticmethod
    def provider_enabled():
        return _flag("SUPADATA_PROVIDER_ENABLED", False)

    @staticmethod
    def monthly_credit_limit():
        try:
            return int(_env("SUPADATA_MONTHLY_CREDIT_LIMIT", "100"))
        except ValueError:
            return 100

    @staticmethod
    def warning_threshold():
        try:
            return int(_env("SUPADATA_CREDIT_WARNING_THRESHOLD", "80"))
        except ValueError:
            return 80

    @staticmethod
    def transcript_mode():
        m = (_env("SUPADATA_TRANSCRIPT_MODE", "native") or "native").strip().lower()
        return m if m in ("native", "auto") else "native"

    @staticmethod
    def allow_ai_generation():
        return _flag("SUPADATA_ALLOW_AI_GENERATION", False)

    @staticmethod
    def base_url():
        return (_env("SUPADATA_BASE_URL", "https://api.supadata.ai/v1") or "").rstrip("/")


def enabled():
    """provider가 실제 호출 가능한 상태인가(플래그 ON + 키 존재)."""
    return SupadataConfig.provider_enabled() and bool(SupadataConfig.api_key())


def estimate_ai_credits(duration_minutes):
    """AI 전사 예상 credits = ceil(분 × 2). 스펙 7절. 길이 모르면 None."""
    if not duration_minutes or duration_minutes <= 0:
        return None
    return int(math.ceil(float(duration_minutes) * 2))


def iso8601_duration_to_minutes(dur):
    """YouTube ISO8601 재생시간(PT8M30S) → 분(올림 아님, float). 실패 시 None."""
    if not dur:
        return None
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", str(dur).strip())
    if not m:
        return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    total = h * 60 + mi + s / 60.0
    return total or None


@dataclass
class TranscriptFetchResult:
    provider: str = "supadata"
    status: str = "pending"                 # transcript 상태모델(스펙 8절)
    transcript_text: str = ""
    segments: list = field(default_factory=list)
    language: str = None
    available_languages: list = field(default_factory=list)
    provider_job_id: str = None             # 202 비동기 job
    credits_used: int = None
    credits_estimated: bool = False
    error_code: str = None                  # provider_config_error|quota_exhausted|rate_limited|...
    error_message: str = None               # 사용자 노출 안전 메시지(원시 body 아님)
    raw_status_code: int = None


# HTTP 상태 → (transcript status, error_code) 매핑(스펙 4·8절)
def _map_http(status_code, body_flags=None):
    body_flags = body_flags or {}
    if status_code == 200:
        return ("available", None)
    if status_code == 202:
        return ("transcribing", None)          # 비동기 전사 시작 → polling
    if status_code == 206 or body_flags.get("transcript_unavailable"):
        return ("manual_required", "transcript_unavailable")
    if status_code == 401:
        return ("config_error", "provider_config_error")
    if status_code == 402:
        return ("quota_exhausted", "payment_required")
    if status_code == 429:
        # rate_limited vs quota_exhausted 구분: body/헤더에 quota 신호 있으면 quota
        if body_flags.get("quota"):
            return ("quota_exhausted", "quota_exhausted")
        return ("rate_limited", "rate_limited")
    if 500 <= status_code < 600:
        return ("provider_failed", "provider_failed")
    return ("provider_failed", "unexpected_status")


_USER_MSG = {
    "provider_config_error": "자동 자막 서비스 설정 오류입니다. 관리자에게 문의해 주세요.",
    "payment_required": "자동 자막 수집 크레딧이 부족합니다. 직접 자막을 입력하거나 관리자에게 문의해 주세요.",
    "quota_exhausted": "이번 달 자동 자막 수집 한도를 모두 사용했습니다. 관리자에게 문의하거나 자막을 직접 입력해 주세요.",
    "rate_limited": "자동 자막 수집이 잠시 혼잡합니다. 잠시 후 다시 시도하거나 자막을 직접 입력해 주세요.",
    "transcript_unavailable": "이 영상은 자동으로 자막을 가져올 수 없습니다. 자막을 직접 붙여넣거나 파일을 업로드해 주세요.",
    "provider_failed": "자동으로 자막을 가져오지 못했습니다. 잠시 후 다시 시도하거나 자막을 직접 입력해 주세요.",
}

def user_message(error_code):
    return _USER_MSG.get(error_code, "자동으로 자막을 가져오지 못했습니다. 자막을 직접 입력해 주세요.")


class SupadataTranscriptProvider:
    """Supadata transcript API 어댑터. request_transcript()만 공개.
    실제 HTTP는 http_client 주입 가능(테스트에서 mock). 키 없으면 config_error 반환."""
    name = "supadata"

    def __init__(self, http_client=None):
        self._client = http_client   # (method, url, headers, params/json) → (status_code, headers, json) 형태 mock 주입용

    def request_transcript(self, video_url, preferred_language="ko", mode=None):
        if not enabled():
            return TranscriptFetchResult(status="config_error", error_code="provider_config_error",
                                         error_message=user_message("provider_config_error"))
        mode = (mode or SupadataConfig.transcript_mode())
        try:
            code, headers, data = self._call(
                "GET", f"{SupadataConfig.base_url()}/transcript",
                params={"url": video_url, "lang": preferred_language or "ko", "text": "false", "mode": mode})
        except Exception as e:
            # 네트워크/타임아웃 등 — 원시 예외 비노출
            return TranscriptFetchResult(status="provider_failed", error_code="provider_failed",
                                         error_message=user_message("provider_failed"),
                                         raw_status_code=None)
        return self._to_result(code, headers, data, mode)

    def poll_job(self, provider_job_id):
        """202로 시작된 전사 job 상태 조회. (polling은 새 transcript 요청으로 집계 안 함)"""
        if not enabled():
            return TranscriptFetchResult(status="config_error", error_code="provider_config_error")
        try:
            code, headers, data = self._call(
                "GET", f"{SupadataConfig.base_url()}/transcript/{provider_job_id}")
        except Exception:
            return TranscriptFetchResult(status="transcribing", provider_job_id=provider_job_id)
        r = self._to_result(code, headers, data, SupadataConfig.transcript_mode())
        r.provider_job_id = provider_job_id
        return r

    # ── 내부 ──
    def _call(self, method, url, params=None):
        if self._client is not None:            # 테스트/주입 경로
            return self._client(method, url, params)
        import httpx                              # lazy — 미설치·미사용 환경 안전
        with httpx.Client(timeout=httpx.Timeout(connect=10.0, read=60.0, write=15.0, pool=10.0)) as cl:
            resp = cl.request(method, url, params=params,
                              headers={"x-api-key": SupadataConfig.api_key()})
            try:
                data = resp.json()
            except Exception:
                data = {}
            return resp.status_code, dict(resp.headers), data

    def _to_result(self, code, headers, data, mode):
        headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        data = data or {}
        body_flags = {
            "transcript_unavailable": bool(data.get("error") == "transcript-unavailable"
                                           or data.get("available") is False),
            "quota": ("quota" in str(data.get("error", "")).lower()
                      or "credit" in str(data.get("error", "")).lower()),
        }
        status, err = _map_http(code, body_flags)
        res = TranscriptFetchResult(provider="supadata", status=status, raw_status_code=code,
                                    error_code=err, error_message=(user_message(err) if err else None))
        # 실제 청구량: x-billable-requests 헤더 우선(스펙 5절)
        billed = headers.get("x-billable-requests")
        if billed is not None:
            try:
                res.credits_used = int(float(billed)); res.credits_estimated = False
            except ValueError:
                pass
        if status == "available":
            segs = data.get("content") or data.get("segments") or []
            if isinstance(segs, list) and segs and isinstance(segs[0], dict):
                res.segments = [{"start": s.get("offset", s.get("start")), "text": s.get("text", "")} for s in segs]
                res.transcript_text = "\n".join(s.get("text", "") for s in segs).strip()
            else:
                res.transcript_text = (data.get("text") or data.get("transcript") or "").strip()
            res.language = data.get("lang") or data.get("language")
            res.available_languages = data.get("availableLangs") or data.get("available_languages") or []
            if res.credits_used is None:        # 헤더 없으면 보수적: native 성공=1
                res.credits_used = 1; res.credits_estimated = True
            if not res.transcript_text and not res.segments:
                res.status = "manual_required"; res.error_code = "transcript_unavailable"
                res.error_message = user_message("transcript_unavailable")
        elif status == "transcribing":
            res.provider_job_id = data.get("jobId") or data.get("id") or data.get("job_id")
        return res
