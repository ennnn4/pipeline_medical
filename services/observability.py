"""경량 관측성 — 구조화 이벤트를 stdout(JSON 한 줄)로 방출. Render 로그 스트림이 수집한다.

GPT 운영 마감 항목: service 예외 유형별·P2013/14/15·요청 endpoint별 발생을 사후 집계할 수 있게.
외부 의존 없음(파이썬 logging). 실패해도 절대 요청을 깨지 않는다(관측이 기능을 막지 않음)."""
import hashlib, json, logging, os, re, time

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def hid(value):
    """식별자(병원 등)를 일방향 해시 8자로 — 집계는 되지만 원본 UUID/slug는 노출 안 함(GPT)."""
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]


def mask_ids(path):
    """경로에서 UUID를 <id>로 치환하고 쿼리스트링 제거 — 로그에 테넌트/자원 식별자 미노출(GPT).
    이미 <slug> 같은 route 패턴이면 그대로. 집계는 패턴 단위로 되므로 정보 손실 없음."""
    if not path:
        return path
    p = str(path).split("?", 1)[0]
    return _UUID_RE.sub("<id>", p)


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
