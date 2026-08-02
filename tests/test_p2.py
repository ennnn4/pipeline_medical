"""P2 검증 — 자료 immutable 버전(재현) + provisioning 충돌 정책 + generation_jobs status CHECK."""
import uuid, tempfile, os
import pytest
from sqlalchemy import text
from store.repositories import tenant_conn
import store.materials as M
from store import ingest as I


@pytest.fixture
def mats(owner):
    I.ensure_gen_schema(owner)         # 먼저: generation_jobs + UNIQUE 타깃(복합 FK 전제)
    M.ensure_materials_schema(owner)   # 그 다음: 복합 FK·seal
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


def test_snapshot_sealed_after_pending(rw, tenant, mats):
    h = tenant["hospital_id"]; fn = "seal.txt"
    M.save_material(rw, h, fn, b"DATA")
    j = I.create_job(rw, h, "봉인", str(uuid.uuid4()))["job_id"]
    M.snapshot_job_materials(rw, h, j)                     # pending → 봉인
    assert I.claim_job(rw, h, j, "t")[0] is True           # 봉인완료 → 실행권 획득
    assert M.snapshot_job_materials(rw, h, j) is None      # 봉인 후 재호출 = 멱등 no-op
    with pytest.raises(Exception):                         # 봉인 후 직접 GJM 변경은 차단
        with tenant_conn(rw, h) as cn:
            cn.execute(text("delete from generation_job_materials where job_id=:j"), {"j": j})


def test_atomic_job_acquisition(rw, tenant, mats):
    h = tenant["hospital_id"]
    j = I.create_job(rw, h, "획득", str(uuid.uuid4()))["job_id"]
    first = I.mark_job(rw, h, j, "generating", allowed_from={"pending"})
    second = I.mark_job(rw, h, j, "generating", allowed_from={"pending"})
    assert first is True and second is False               # 한 워커만 실행권 획득


def test_snapshot_reseal_noop(rw, tenant, mats):
    h = tenant["hospital_id"]
    M.save_material(rw, h, "s.txt", b"D")
    j = I.create_job(rw, h, "재봉인", str(uuid.uuid4()))["job_id"]
    assert M.snapshot_job_materials(rw, h, j) is not None
    assert M.snapshot_job_materials(rw, h, j) is None      # 이미 봉인 → no-op
    # 봉인 후 pending이어도 seal 트리거가 변경 차단
    with pytest.raises(Exception):
        with tenant_conn(rw, h) as cn:
            cn.execute(text("insert into generation_job_materials"
                            "(id,hospital_id,job_id,material_version_id,filename,size_bytes) "
                            "select gen_random_uuid(),:h,:j,current_version_id,filename,1 "
                            "from materials where hospital_id=:h limit 1"), {"h": h, "j": j})


def test_worker_token_guards_transition(rw, tenant, mats):
    h = tenant["hospital_id"]
    M.save_material(rw, h, "w.txt", b"D")
    j = I.create_job(rw, h, "토큰", str(uuid.uuid4()))["job_id"]
    M.snapshot_job_materials(rw, h, j)
    acquired, reason = I.claim_job(rw, h, j, "tokA")
    assert acquired is True and reason is None
    assert I.mark_job(rw, h, j, "generated", allowed_from={"generating"}, worker_token="WRONG") is False
    assert I.mark_job(rw, h, j, "generated", allowed_from={"generating"}, worker_token="tokA") is True


def test_one_active_job_per_hospital(rw, tenant, mats):
    h = tenant["hospital_id"]
    M.save_material(rw, h, "a.txt", b"D")
    j1 = I.create_job(rw, h, "액티브1", str(uuid.uuid4()))["job_id"]
    M.snapshot_job_materials(rw, h, j1)
    assert I.claim_job(rw, h, j1, "t1")[0] is True        # j1 active
    j2 = I.create_job(rw, h, "액티브2", str(uuid.uuid4()))["job_id"]
    M.snapshot_job_materials(rw, h, j2)
    acquired, reason = I.claim_job(rw, h, j2, "t2")       # 같은 병원 동시 active 차단
    assert acquired is False and reason == "hospital_busy"


def test_claim_requires_sealed_snapshot(rw, tenant, mats):
    h = tenant["hospital_id"]
    j = I.create_job(rw, h, "미봉인", str(uuid.uuid4()))["job_id"]   # 스냅샷 봉인 안 함
    acquired, reason = I.claim_job(rw, h, j, "t")
    assert acquired is False and reason == "not_sealed"    # 봉인 전 실행권 획득 불가


def test_gjm_rejects_cross_tenant_version(owner, rw, tenant, mats):
    """다른 병원의 material_version_id를 job snapshot에 넣으려 하면 복합 FK가 차단."""
    from store.provision import ensure_provision, provision_hospital
    ensure_provision(owner)
    hA = tenant["hospital_id"]
    # 병원 B + B의 자료 버전
    uB = uuid.uuid4()
    with owner.begin() as cn:
        cn.execute(text("insert into users(id,email) values(:u,:e)"), {"u": uB, "e": uB.hex + "@t.c"})
    hB = provision_hospital(rw, "clinicB" + uuid.uuid4().hex[:6], "B", owner_user=uB)
    verB = M.save_material(rw, hB, "b.txt", b"B-DATA")
    jA = I.create_job(rw, hA, "cross", str(uuid.uuid4()))["job_id"]
    with pytest.raises(Exception):     # A의 job + B의 version → FK 위반
        with tenant_conn(rw, hA) as cn:
            cn.execute(text("insert into generation_job_materials"
                            "(id,hospital_id,job_id,material_version_id,filename,size_bytes) "
                            "values(gen_random_uuid(),:h,:j,:v,'x',1)"),
                       {"h": hA, "j": jA, "v": verB})
