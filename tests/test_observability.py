"""경량 관측성 — service 예외·SQLSTATE가 구조화 이벤트로 방출되는지, 예외 안전한지."""
import json, logging
from services.observability import emit
from services import exceptions as ex


def _capture(caplog):
    out = []
    for r in caplog.records:
        if r.name == "boncure.obs":
            try: out.append(json.loads(r.message))
            except Exception: pass
    return out


def test_emit_writes_structured_line(caplog):
    with caplog.at_level(logging.INFO, logger="boncure.obs"):
        emit("http", app="studio", method="GET", rule="/ui/x", status=200)
    recs = _capture(caplog)
    assert any(r["obs"] == "http" and r["status"] == 200 and r["rule"] == "/ui/x" for r in recs)


def test_service_error_emits_on_construction(caplog):
    with caplog.at_level(logging.INFO, logger="boncure.obs"):
        ex.VersionConflict("current 아님")
    recs = _capture(caplog)
    assert any(r["obs"] == "service_error" and r["code"] == "version_conflict" and r["status"] == 409
               for r in recs)


def test_from_sqlstate_emits_raw_code(caplog):
    with caplog.at_level(logging.INFO, logger="boncure.obs"):
        e = ex.from_sqlstate("P2015", "not current")
    assert isinstance(e, ex.VersionConflict)
    recs = _capture(caplog)
    assert any(r["obs"] == "sqlstate" and r["sqlstate"] == "P2015" for r in recs)


def test_emit_never_raises():
    class Bad:
        def __str__(self): raise RuntimeError("boom")
    emit("x", weird=Bad())                  # default=str가 터져도 삼켜야 함 — 예외 없이 반환
