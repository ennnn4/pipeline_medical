"""DB 엔진/세션. 운영은 DATABASE_URL(PostgreSQL). 로컬 테스트 기본값은 pgserver(pg8000)."""
import os
from sqlalchemy import create_engine

# 운영: DATABASE_URL 환경변수. 로컬 테스트: postgresql-binaries로 띄운 인스턴스(pg8000 순수드라이버).
DEFAULT_TEST_URL = "postgresql+pg8000://postgres@127.0.0.1:55432/boncure_test"
DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_TEST_URL)

def make_engine(url=None, **kw):
    return create_engine(url or DATABASE_URL, future=True, **kw)
