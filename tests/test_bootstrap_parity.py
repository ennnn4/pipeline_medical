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
    from store.platform_ops import ensure_platform_ops
    from store.approval_foundation import ensure_approval_foundation
    from store.seed_images import ensure_scene_images
    from store.approval_fns import ensure_approval_fns
    ensure_gen_schema(eng); ensure_materials_schema(eng); ensure_provision(eng)
    ensure_platform_ops(eng)
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


def test_platform_ops_objects_and_acl(bootstrapped_url):
    # platform operator: 테이블·컬럼·함수 존재 + ensure는 app_rw 실행 가능, grant/revoke는 불가, 직접 쓰기 회수.
    with bootstrapped_url.connect() as cn:
        assert cn.execute(text("select to_regclass('public.platform_access_grants')")).scalar()
        for col in ("grant_source", "platform_grant_id", "revoked_at"):
            assert cn.execute(text("select 1 from information_schema.columns where table_name='membership_roles' "
                                   "and column_name=:c"), {"c": col}).scalar(), col
        ens = cn.execute(text("select has_function_privilege('app_rw','public.fn_ensure_platform_operator_membership(uuid)','EXECUTE')")).scalar()
        lst = cn.execute(text("select has_function_privilege('app_rw','public.fn_list_platform_hospitals()','EXECUTE')")).scalar()
        grant_x = cn.execute(text("select has_function_privilege('app_rw','public.fn_grant_platform_operator(uuid,uuid)','EXECUTE')")).scalar()
        ins = cn.execute(text("select has_table_privilege('app_rw','platform_access_grants','INSERT')")).scalar()
        sel = cn.execute(text("select has_table_privilege('app_rw','platform_access_grants','SELECT')")).scalar()
    assert ens is True and lst is True         # resolve·병원목록은 app_rw 실행
    assert grant_x is False                    # 부여/철회는 owner 전용
    assert ins is False and sel is True        # 직접 쓰기 회수, 읽기만(role 유효성 판정용)


def test_required_functions_exist(bootstrapped_url):
    need = {"fn_approve_core", "fn_self_approve_version", "fn_reject_version", "fn_revoke_version",
            "fn_add_human_assessment", "fn_mark_version_superseded", "fn_freeze_assessment_if_decided",
            "fn_seal_job_materials", "fn_provision_hospital",
            "fn_ensure_platform_operator_membership", "fn_grant_platform_operator",
            "fn_revoke_platform_operator", "fn_list_platform_hospitals"}
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


# ── GPT 추가 요구: 이름·소유자만 맞고 ACL/search_path/보안속성이 퇴행하는 치명 케이스 차단 ──

# (시그니처 명시 — has_function_privilege·pg_get_functiondef 대상)
_APPROVE_FNS = {
    "core":  "public.fn_approve_core(uuid,uuid,text,text,text,text,text)",
    "approve": "public.fn_approve_version(uuid,uuid,text,text,text)",
    "self":  "public.fn_self_approve_version(uuid,uuid,text,text,text,text)",
    "reject": "public.fn_reject_version(uuid,uuid,text)",
    "revoke": "public.fn_revoke_version(uuid,uuid,text)",
}


def test_definer_fns_are_security_definer_with_fixed_search_path(bootstrapped_url):
    with bootstrapped_url.connect() as cn:
        for sig in _APPROVE_FNS.values():
            secdef = cn.execute(text("select prosecdef from pg_proc where oid = cast(:s as regprocedure)"),
                                {"s": sig}).scalar()
            cfg = cn.execute(text("select coalesce(proconfig,'{}') from pg_proc where oid = cast(:s as regprocedure)"),
                             {"s": sig}).scalar()
            assert secdef is True, f"{sig} SECURITY DEFINER 아님"
            assert any(str(c).startswith("search_path=") for c in cfg), f"{sig} 고정 search_path 없음"


def test_core_not_executable_by_app_rw_but_wrappers_are(bootstrapped_url):
    with bootstrapped_url.connect() as cn:
        core = cn.execute(text("select has_function_privilege('app_rw', cast(:s as regprocedure), 'EXECUTE')"),
                          {"s": _APPROVE_FNS["core"]}).scalar()
        assert core is False, "private core를 app_rw가 실행 가능(격리 붕괴)"
        for k in ("approve", "self", "reject", "revoke"):
            ok = cn.execute(text("select has_function_privilege('app_rw', cast(:s as regprocedure), 'EXECUTE')"),
                            {"s": _APPROVE_FNS[k]}).scalar()
            assert ok is True, f"wrapper {k} app_rw EXECUTE 부재"


def test_no_public_execute_on_approval_fns(bootstrapped_url):
    # proacl에 grantee 빈('=X/...') PUBLIC EXECUTE 항목이 없어야 함(REVOKE ALL FROM PUBLIC 유지).
    with bootstrapped_url.connect() as cn:
        for sig in _APPROVE_FNS.values():
            acl = cn.execute(text("select coalesce(proacl::text[], '{}') from pg_proc where oid = cast(:s as regprocedure)"),
                             {"s": sig}).scalar()
            assert not any(str(a).startswith("=") for a in acl), f"{sig} PUBLIC EXECUTE 잔존"


def test_public_cannot_create_in_schema(bootstrapped_url):
    with bootstrapped_url.connect() as cn:
        can = cn.execute(text("select has_schema_privilege('public','public','CREATE')")).scalar()
    assert can is False                    # untrusted가 public 스키마에 객체 생성 금지(definer 안전)


def test_key_tables_rls_enabled_and_forced(bootstrapped_url):
    tables = ["scripts", "script_versions", "script_blocks", "claims", "claim_assessments",
              "version_approval_states", "materials", "material_versions",
              "generation_jobs", "generation_job_materials", "scene_images"]
    with bootstrapped_url.connect() as cn:
        for t in tables:
            row = cn.execute(text("select relrowsecurity, relforcerowsecurity from pg_class where relname=:t "
                                  "and relnamespace = 'public'::regnamespace"), {"t": t}).first()
            assert row and row[0] is True and row[1] is True, f"{t} RLS enable/force 아님"


def test_storage_stats_runs(bootstrapped_url):
    # bytea 용량 집계(관리 명령) — 빈 테이블에서도 스키마·집계가 동작하고 키가 채워지는지.
    from store.storage_stats import scene_image_stats
    s = scene_image_stats(bootstrapped_url)
    for k in ("images", "data_bytes", "avg_bytes", "max_bytes", "images_last_7d",
              "table_total_bytes", "db_bytes", "pct_of_db", "per_hospital"):
        assert k in s
    assert s["images"] == 0 and isinstance(s["per_hospital"], list)


def test_freeze_seal_triggers_enabled(bootstrapped_url):
    # tgenabled: 'O'(origin/enabled), 'D'=disabled. seal/freeze 트리거가 실제 작동 상태여야 함.
    triggers = ["trg_freeze_assessment", "trg_seal_job_materials", "trg_freeze_material_version",
                "trg_frozen", "trg_lock_assessment"]
    with bootstrapped_url.connect() as cn:
        for tg in triggers:
            enabled = cn.execute(text("select bool_and(tgenabled <> 'D') from pg_trigger where tgname=:g"),
                                 {"g": tg}).scalar()
            assert enabled is True, f"트리거 {tg} 비활성/부재"
