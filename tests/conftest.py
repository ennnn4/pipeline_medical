"""pytest 픽스처 — 실제 PostgreSQL 대상. 로컬 PG(postgresql-binaries) 55432 기본.
PYTEST_PG_ADMIN 로 다른 PG(운영 major version smoke) 지정 가능."""
import os, uuid, pytest
from sqlalchemy import create_engine, text
import store.schema as S
import store.rls_sql as R

ADMIN_URL = os.environ.get("PYTEST_PG_ADMIN", "postgresql+pg8000://postgres@127.0.0.1:55432/postgres")
TESTDB = os.environ.get("PYTEST_DB", "pytest_boncure")

@pytest.fixture(scope="session")
def base_url():
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as cn:
        cn.execute(text(f"drop database if exists {TESTDB}"))
        cn.execute(text(f"create database {TESTDB}"))
    admin.dispose()
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + TESTDB
    eng = create_engine(url, future=True)
    S.metadata.create_all(eng)
    with eng.begin() as cn:
        cn.execute(text("ALTER TABLE scripts ADD CONSTRAINT fk_scripts_current_version "
                        "FOREIGN KEY (hospital_id, id, current_version_id) "
                        "REFERENCES script_versions (hospital_id, script_id, id)"))
    R.apply(eng)
    eng.dispose()
    return url

@pytest.fixture(scope="session")
def owner(base_url):
    e = create_engine(base_url, future=True)
    yield e
    e.dispose()

@pytest.fixture(scope="session")
def rw(base_url):
    url = base_url.replace("postgres@", "app_rw:x@")
    e = create_engine(url, future=True, pool_size=2, max_overflow=0)
    yield e
    e.dispose()

@pytest.fixture
def tenant(owner):
    hid, uid, mid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with owner.begin() as cn:
        cn.execute(text("insert into hospitals(id,slug,name) values(:h,:s,'T')"), {"h": hid, "s": "h" + hid.hex[:10]})
        cn.execute(text("insert into users(id,email) values(:u,:e)"), {"u": uid, "e": uid.hex + "@t.c"})
        cn.execute(text("insert into hospital_memberships(id,hospital_id,user_id) values(:m,:h,:u)"),
                   {"m": mid, "h": hid, "u": uid})
    return {"hospital_id": hid, "user_id": uid, "membership_id": mid}
