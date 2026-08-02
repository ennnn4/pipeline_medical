"""Bootstrap 계약 관문(GPT 영구 관문) — reseed(deploy_bootstrap) 후에도 강화 스키마·함수·ACL이
살아있어야 함. reseed 치명버그(승인함수 소실·구형 monolithic 복귀) 재발 방지.

deploy_bootstrap의 스키마 부분과 동일한 순서로 fresh DB를 구성하고 계약을 검증한다."""
import os, uuid, pytest
from sqlalchemy import create_engine, text
import store.schema as S
import store.rls_sql as R

ADMIN_URL = os.environ.get("PYTEST_PG_ADMIN", "postgresql+pg8000://postgres@127.0.0.1:55432/postgres")


@pytest.fixture(scope="module")
def bootstrapped_url():
    dbname = "pytest_bootstrap_parity"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as cn:
        cn.execute(text(f"drop database if exists {dbname}"))
        cn.execute(text(f"create database {dbname}"))
    admin.dispose()
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + dbname
    eng = create_engine(url, future=True)
    # deploy_bootstrap과 동일한 순서: create_all → FK → R.apply → ensure_* (approval_fns 마지막)
    S.metadata.create_all(eng)
    with eng.begin() as cn:
        cn.execute(text("ALTER TABLE scripts ADD CONSTRAINT fk_scripts_current_version "
                        "FOREIGN KEY (hospital_id, id, current_version_id) "
                        "REFERENCES script_versions (hospital_id, script_id, id)"))
    R.apply(eng)
    from store.ingest import ensure_gen_schema
    from store.materials import ensure_materials_schema
    from store.provision import ensure_provision
    from store.approval_foundation import ensure_approval_foundation
    from store.seed_images import ensure_scene_images
    from store.approval_fns import ensure_approval_fns
    ensure_gen_schema(eng); ensure_materials_schema(eng); ensure_provision(eng)
    ensure_approval_foundation(eng); ensure_scene_images(eng); ensure_approval_fns(eng)
    yield eng
    eng.dispose()


def test_approval_fn_is_strengthened_single_owner(bootstrapped_url):
    with bootstrapped_url.connect() as cn:
        d = cn.execute(text("select pg_get_functiondef('public.fn_approve_version(uuid,uuid,text,text,text)'::regprocedure)")).scalar()
        n = cn.execute(text("select count(*) from pg_proc where proname='fn_approve_version'")).scalar()
        owner = cn.execute(text("select r.rolname from pg_proc p join pg_roles r on r.oid=p.proowner "
                                "where p.proname='fn_approve_version'")).scalar()
    assert "fn_approve_core" in d          # 강화형(core 위임) — 구형 monolithic 아님
    assert n == 1                          # 구형 overload 부재
    assert owner == "app_owner"            # 소유권 유지


def test_required_functions_exist(bootstrapped_url):
    need = {"fn_approve_core", "fn_self_approve_version", "fn_reject_version", "fn_revoke_version",
            "fn_add_human_assessment", "fn_mark_version_superseded", "fn_freeze_assessment_if_decided",
            "fn_seal_job_materials", "fn_provision_hospital"}
    with bootstrapped_url.connect() as cn:
        have = {r[0] for r in cn.execute(text("select proname from pg_proc where proname = any(:n)"),
                                         {"n": list(need)})}
    assert need <= have, f"누락 함수: {need - have}"


def test_required_columns_exist(bootstrapped_url):
    checks = [
        ("version_approval_states", "approval_event_id"), ("version_approval_states", "revoked_by_membership_id"),
        ("version_approval_states", "superseded_by_version_id"),
        ("claim_assessments", "human_decision"), ("claim_assessments", "review_seq"),
        ("script_versions", "generation_job_id"), ("audit_events", "metadata"),
        ("hospitals", "allow_self_approval"), ("scene_images", "source_scene_hash"),
        ("generation_jobs", "worker_token"), ("generation_jobs", "material_snapshot_hash"),
    ]
    with bootstrapped_url.connect() as cn:
        for tbl, col in checks:
            got = cn.execute(text("select 1 from information_schema.columns where table_name=:t and column_name=:c"),
                             {"t": tbl, "c": col}).scalar()
            assert got, f"누락 컬럼: {tbl}.{col}"


def test_app_rw_cannot_insert_claim_assessments(bootstrapped_url):
    with bootstrapped_url.connect() as cn:
        ins = cn.execute(text("select has_table_privilege('app_rw','claim_assessments','INSERT')")).scalar()
    assert ins is False                    # 사람판정 직접 INSERT 회수 유지(전용 함수만)
