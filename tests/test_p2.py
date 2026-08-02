"""P2 검증 — 자료 immutable 버전(재현) + provisioning 충돌 정책 + generation_jobs status CHECK."""
import uuid, tempfile, os
import pytest
from sqlalchemy import text
from store.repositories import tenant_conn
import store.materials as M
from store import ingest as I


@pytest.fixture
def mats(owner):
    M.ensure_materials_schema(owner)
    I.ensure_gen_schema(owner)
    return True


def test_material_version_preserves_original(rw, tenant, mats):
    h = tenant["hospital_id"]; fn = "paper.txt"
    M.save_material(rw, h, fn, b"ORIGINAL")
    M.save_material(rw, h, fn, b"REPLACED")
    versions = M.list_material_versions(rw, h, fn)
    assert len(versions) == 2                       # 교체해도 원본 버전이 남음
    # 현재값은 REPLACED
    assert M.get_material(rw, h, fn)[1] == b"REPLACED"


def test_job_snapshot_reproduces_exact_version(rw, tenant, mats):
    h = tenant["hospital_id"]; fn = "src.txt"
    M.save_material(rw, h, fn, b"AT-GENERATION-TIME")
    j = I.create_job(rw, h, "재현", str(uuid.uuid4()))["job_id"]
    M.snapshot_job_materials(rw, h, j)
    M.save_material(rw, h, fn, b"CHANGED-AFTERWARDS")   # 생성 후 자료 교체
    d = tempfile.mkdtemp()
    n = M.materialize_job_snapshot(rw, h, j, d)
    assert n == 1
    assert open(os.path.join(d, fn), "rb").read() == b"AT-GENERATION-TIME"   # 그때 그 원본


def test_material_version_content_immutable(rw, tenant, mats):
    h = tenant["hospital_id"]; fn = "immut.txt"
    vid = M.save_material(rw, h, fn, b"FROZEN")
    with pytest.raises(Exception):        # 내용 변조 트리거 차단
        with tenant_conn(rw, h) as cn:
            cn.execute(text("update material_versions set data=:d where id=:v"), {"d": b"HACK", "v": vid})


def test_delete_material_preserves_versions(rw, tenant, mats):
    h = tenant["hospital_id"]; fn = "del.txt"
    M.save_material(rw, h, fn, b"one"); M.save_material(rw, h, fn, b"two")
    before = len(M.list_material_versions(rw, h, fn))
    M.delete_material(rw, h, fn)
    assert len(M.list_material_versions(rw, h, fn)) == before   # 논리삭제해도 이력 보존
    with tenant_conn(rw, h) as cn:
        assert cn.execute(text("select count(*) from materials where hospital_id=:h and filename=:f"),
                          {"h": h, "f": fn}).scalar() == 0


def test_provision_conflict_blocks_unrelated_user(owner, rw, tenant, mats):
    from store.provision import ensure_provision, provision_hospital, ProvisionConflict
    ensure_provision(owner)
    uB = uuid.uuid4()
    with owner.begin() as cn:
        cn.execute(text("insert into users(id,email) values(:u,:e)"), {"u": uB, "e": uB.hex + "@t.c"})
    slug = "clinic" + uuid.uuid4().hex[:8]
    h1 = provision_hospital(rw, slug, "A", owner_user=tenant["user_id"])
    assert h1 is not None
    assert provision_hospital(rw, slug, "A", owner_user=tenant["user_id"]) == h1   # 같은 유저 멱등
    with pytest.raises(ProvisionConflict):                                          # 타인 → 차단
        provision_hospital(rw, slug, "탈취", owner_user=uB)


def test_provision_rejects_bad_slug(owner, rw, tenant, mats):
    from store.provision import ensure_provision, provision_hospital
    ensure_provision(owner)
    with pytest.raises(Exception):
        provision_hospital(rw, "bad slug!!", "X", owner_user=tenant["user_id"])


def test_genjobs_status_check(rw, tenant, mats):
    h = tenant["hospital_id"]
    j = I.create_job(rw, h, "체크", str(uuid.uuid4()))["job_id"]
    with pytest.raises(Exception):     # 화이트리스트 밖 상태 직접 주입 차단
        with tenant_conn(rw, h) as cn:
            cn.execute(text("update generation_jobs set status='bogus' where id=:j"), {"j": j})
