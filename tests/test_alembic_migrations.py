"""Alembic 마이그레이션 실제 검증 — 빈 DB에서 upgrade head 성공(GPT P0).

런타임 ensure_* 함수가 아니라 진짜 `alembic upgrade head`를 새 DB에 실행해,
0001~0004 리비전이 처음부터 정상 생성되는지 확인.
"""
import os
import uuid
import pytest
from sqlalchemy import create_engine, text

ADMIN = os.environ.get("PYTEST_PG_ADMIN", "postgresql+pg8000://postgres@127.0.0.1:55432/postgres")


def test_alembic_fresh_upgrade_head():
    dbname = "alembic_fresh_" + uuid.uuid4().hex[:8]
    admin = create_engine(ADMIN, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as c:
        c.execute(text(f"create database {dbname}"))
    fresh_url = ADMIN.rsplit("/", 1)[0] + "/" + dbname
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = fresh_url          # env.py가 이 URL로 마이그레이션
    try:
        from alembic.config import Config
        from alembic import command
        cfg = Config()
        cfg.set_main_option("script_location", "store/alembic")
        command.upgrade(cfg, "head")                # 0001→0004 전부
        eng = create_engine(fresh_url, future=True)
        with eng.connect() as c:
            n = c.execute(text(
                "select count(*) from information_schema.tables where table_schema='public' "
                "and table_name in ('hospitals','generation_jobs','scene_images','materials')")).scalar()
            # request_idempotency_key 유니크 인덱스(비-partial)까지 생성됐는지
            idx = c.execute(text("select indexdef from pg_indexes where indexname='uq_genjobs_reqkey'")).scalar()
        eng.dispose()
        assert n == 4, f"핵심 테이블 4개가 생성돼야 함(실제 {n})"
        assert idx and "WHERE" not in idx.upper(), "uq_genjobs_reqkey는 non-partial 유니크여야 함"
    finally:
        os.environ.pop("DATABASE_URL", None) if old is None else os.environ.__setitem__("DATABASE_URL", old)
        with admin.connect() as c:
            c.execute(text("select pg_terminate_backend(pid) from pg_stat_activity "
                           f"where datname='{dbname}' and pid<>pg_backend_pid()"))
            c.execute(text(f"drop database if exists {dbname}"))
        admin.dispose()
