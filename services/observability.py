"""경량 관측성 — 구조화 이벤트를 stdout(JSON 한 줄)로 방출. Render 로그 스트림이 수집한다.

GPT 운영 마감 항목: service 예외 유형별·P2013/14/15·요청 endpoint별 발생을 사후 집계할 수 있게.
외부 의존 없음(파이썬 logging). 실패해도 절대 요청을 깨지 않는다(관측이 기능을 막지 않음)."""
import json, logging, os, time

_log = logging.getLogger("boncure.obs")
if not _log.handlers:                       # 앱이 별도 설정 안 했을 때만 기본 핸들러(중복 방지)
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(message)s"))
    _log.addHandler(_h)
    _log.setLevel(logging.INFO)
    _log.propagate = False

_ENABLED = os.environ.get("OBS_DISABLE", "").lower() not in ("1", "true", "yes")


def emit(event, **fields):
    """이벤트 한 건 방출. event=범주(service_error/http/reap/...), fields=속성. 예외 안전."""
    if not _ENABLED:
        return
    try:
        rec = {"obs": event, "ts": round(time.time(), 3)}
        rec.update({k: v for k, v in fields.items() if v is not None})
        _log.info(json.dumps(rec, ensure_ascii=False, default=str))
    except Exception:
        pass                                # 관측 실패가 요청을 깨지 않게
