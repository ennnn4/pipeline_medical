"""Supadata provider 단위 테스트(스펙 15절) — HTTP 매핑·크레딧·비활성 fallback. DB·네트워크 불필요."""
import os
import pytest

from services import supadata as S
from services.supadata import SupadataTranscriptProvider, TranscriptFetchResult


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("SUPADATA_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("SUPADATA_API_KEY", "test-key")
    monkeypatch.setenv("SUPADATA_MONTHLY_CREDIT_LIMIT", "100")
    monkeypatch.setenv("SUPADATA_TRANSCRIPT_MODE", "native")


def _client(code, headers=None, data=None):
    def call(method, url, params=None):
        return code, (headers or {}), (data or {})
    return call


def test_disabled_without_key(monkeypatch):
    monkeypatch.delenv("SUPADATA_API_KEY", raising=False)
    assert S.enabled() is False
    r = SupadataTranscriptProvider().request_transcript("https://youtu.be/x")
    assert r.status == "config_error" and r.error_code == "provider_config_error"


def test_200_success_with_segments():
    p = SupadataTranscriptProvider(http_client=_client(
        200, {"x-billable-requests": "1"},
        {"lang": "ko", "content": [{"offset": 0, "text": "안녕"}, {"offset": 1, "text": "하세요"}]}))
    r = p.request_transcript("https://youtu.be/x")
    assert r.status == "available"
    assert "안녕" in r.transcript_text and len(r.segments) == 2
    assert r.credits_used == 1 and r.credits_estimated is False
    assert r.language == "ko"


def test_200_empty_falls_to_manual():
    p = SupadataTranscriptProvider(http_client=_client(200, {}, {"text": ""}))
    r = p.request_transcript("https://youtu.be/x")
    assert r.status == "manual_required" and r.error_code == "transcript_unavailable"


def test_202_job_created():
    p = SupadataTranscriptProvider(http_client=_client(202, {}, {"jobId": "job-123"}))
    r = p.request_transcript("https://youtu.be/x")
    assert r.status == "transcribing" and r.provider_job_id == "job-123"


def test_206_unavailable():
    p = SupadataTranscriptProvider(http_client=_client(206, {}, {}))
    r = p.request_transcript("https://youtu.be/x")
    assert r.status == "manual_required" and r.error_code == "transcript_unavailable"


def test_401_config_error():
    r = SupadataTranscriptProvider(http_client=_client(401, {}, {})).request_transcript("u")
    assert r.status == "config_error" and r.error_code == "provider_config_error"
    assert "관리자" in r.error_message


def test_402_payment_required():
    r = SupadataTranscriptProvider(http_client=_client(402, {}, {})).request_transcript("u")
    assert r.status == "quota_exhausted" and r.error_code == "payment_required"


def test_429_rate_vs_quota():
    rate = SupadataTranscriptProvider(http_client=_client(429, {}, {})).request_transcript("u")
    assert rate.status == "rate_limited" and rate.error_code == "rate_limited"
    quota = SupadataTranscriptProvider(http_client=_client(429, {}, {"error": "credit-limit-exceeded"})).request_transcript("u")
    assert quota.status == "quota_exhausted"


def test_5xx_provider_failed():
    r = SupadataTranscriptProvider(http_client=_client(503, {}, {})).request_transcript("u")
    assert r.status == "provider_failed" and r.error_code == "provider_failed"


def test_network_error_is_safe():
    def boom(method, url, params=None):
        raise RuntimeError("connection reset")
    r = SupadataTranscriptProvider(http_client=boom).request_transcript("u")
    assert r.status == "provider_failed"
    assert "connection reset" not in (r.error_message or "")   # 원시 오류 비노출


def test_billable_header_overrides_estimate():
    p = SupadataTranscriptProvider(http_client=_client(200, {"x-billable-requests": "3"}, {"text": "hi"}))
    r = p.request_transcript("u")
    assert r.credits_used == 3 and r.credits_estimated is False


def test_credit_estimation():
    assert S.estimate_ai_credits(10) == 20
    assert S.estimate_ai_credits(8.5) == 17
    assert S.estimate_ai_credits(0) is None
    assert S.iso8601_duration_to_minutes("PT8M30S") == pytest.approx(8.5)
    assert S.iso8601_duration_to_minutes("PT1H2M") == pytest.approx(62)


def test_config_defaults(monkeypatch):
    monkeypatch.delenv("SUPADATA_MONTHLY_CREDIT_LIMIT", raising=False)
    assert S.SupadataConfig.monthly_credit_limit() == 100
    assert S.SupadataConfig.transcript_mode() == "native"
