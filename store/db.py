"""DB 엔진/세션. 운영은 DATABASE_URL(PostgreSQL). 로컬 테스트 기본값은 pgserver(pg8000).

Render 등 관리형 PG 대응:
 - Render가 주는 URL은 `postgresql://...`(psycopg 기본) 형태 → pg8000 드라이버로 정규화.
 - 관리형 PG는 SSL 필요(외부)·허용(내부). 원격 호스트면 암호화 SSL 컨텍스트 자동 부착.
   내부 호스트(dpg-xxx-a)는 도메인 인증서가 없어 hostname 검증이 불가하므로 CERT_NONE(암호화하되 미검증).
   로컬(127.0.0.1/localhost)은 SSL 미사용. DATABASE_SSL=0 으로 강제 해제 가능.
"""
import os, ssl
from sqlalchemy import create_engine

DEFAULT_TEST_URL = "postgresql+pg8000://postgres@127.0.0.1:55432/boncure_test"

def _normalize(url: str) -> str:
    for p in ("postgresql+pg8000://",):
        if url.startswith(p):
            return url
    for p in ("postgresql://", "postgres://"):
        if url.startswith(p):
            return "postgresql+pg8000://" + url[len(p):]
    return url

def _is_local(url: str) -> bool:
    return "127.0.0.1" in url or "localhost" in url or "@/" in url

def make_engine(url=None, **kw):
    raw = url or os.environ.get("DATABASE_URL", DEFAULT_TEST_URL)
    u = _normalize(raw)
    ca = dict(kw.pop("connect_args", {}))
    want_ssl = ("+pg8000" in u) and (not _is_local(u)) and os.environ.get("DATABASE_SSL", "1") != "0"
    if want_ssl and "ssl_context" not in ca:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False          # 내부 호스트(dpg-xxx-a)엔 매칭 도메인 인증서 없음
        ctx.verify_mode = ssl.CERT_NONE     # 암호화는 유지, 인증서 검증만 생략(관리형 사설망)
        ca["ssl_context"] = ctx
    return create_engine(u, future=True, connect_args=ca, **kw)
