"""동시 편집 CAS(current_version) + 마이그레이션 lease(SKIP LOCKED)."""
import uuid, pytest
from sqlalchemy import text
from store.testkit import new_version
from store import repositories as repo

def _script_with_current(owner, h):
    with owner.begin() as cn:
        sc, v = new_version(cn, h)
        cn.execute(text("update scripts set current_version_id=:v where id=:s"), {"v": v, "s": sc})
    return sc, v

def test_cas_only_one_wins(owner, tenant):
    h = tenant["hospital_id"]; sc, v1 = _script_with_current(owner, h)
    v2 = repo.create_edited_version(owner, h, sc, expected_current_version_id=v1)   # 편집자 A
    assert v2 != v1
    with pytest.raises(repo.Conflict):                                             # 편집자 B(구버전 기대)
        repo.create_edited_version(owner, h, sc, expected_current_version_id=v1)
    # current는 v2, version_no 중복 없음
    with owner.connect() as cn:
        cur = cn.execute(text("select current_version_id from scripts where id=:s"), {"s": sc}).scalar()
        nos = [r[0] for r in cn.execute(text("select version_no from script_versions where script_id=:s order by version_no"), {"s": sc})]
    assert cur == v2 and nos == [1, 2]

def _pending_import(owner, h):
    iid = uuid.uuid4()
    with owner.begin() as cn:
        cn.execute(text("insert into migration_imports(id,hospital_id,source_uri,raw_sha256,migration_version,status) "
                        "values(:i,:h,'u','sha','m1','pending')"), {"i": iid, "h": h})
    return iid

def test_lease_acquire_and_contention(owner, tenant):
    h = tenant["hospital_id"]; iid = _pending_import(owner, h)
    a = repo.acquire_lease(owner, "worker-1")
    assert a and a["id"] == iid                       # 워커1 획득
    b = repo.acquire_lease(owner, "worker-2")
    assert b is None                                  # 워커2는 못 얻음(활성 lease)

def test_heartbeat_token_match(owner, tenant):
    h = tenant["hospital_id"]; iid = _pending_import(owner, h)
    a = repo.acquire_lease(owner, "worker-1")
    assert repo.heartbeat(owner, iid, uuid.uuid4()) == 0        # 다른 토큰 거부
    assert repo.heartbeat(owner, iid, a["lease_token"]) == 1    # 정당 워커

def test_expired_lease_takeover(owner, tenant):
    h = tenant["hospital_id"]; iid = _pending_import(owner, h)
    repo.acquire_lease(owner, "worker-1", ttl_sec=1)
    with owner.begin() as cn:                          # lease 만료 강제
        cn.execute(text("update migration_imports set lease_expires_at=now()-interval '1 min' where id=:i"), {"i": iid})
    b = repo.acquire_lease(owner, "worker-2")
    assert b and b["id"] == iid                        # 만료 lease 인계
