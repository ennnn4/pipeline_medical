"""동시 편집 CAS(app_rw + 진짜 병렬) + 마이그레이션 lease(owner/BYPASSRLS)."""
import uuid, threading, pytest
from sqlalchemy import text
from store.testkit import new_version, new_block
from store import repositories as repo
from store.repositories import tenant_conn, create_edited_version, Conflict

def _content(cn, h, v):                                     # 편집 콘텐츠(블록 1개) — 빈 버전 방지(#5)
    new_block(cn, h, v, 0)

def _script_current(owner, h):
    with owner.begin() as cn:
        sc, v = new_version(cn, h)
        cn.execute(text("update scripts set current_version_id=:v where id=:s"), {"v": v, "s": sc})
    return sc, v

def test_create_edited_requires_content(owner, rw, tenant):
    h = tenant["hospital_id"]; sc, v1 = _script_current(owner, h)
    with pytest.raises(ValueError):
        with tenant_conn(rw, h) as cn:
            create_edited_version(cn, h, sc, v1, None)     # 콘텐츠 없으면 거부

def test_cas_via_app_rw_sequential(owner, rw, tenant):
    h = tenant["hospital_id"]; sc, v1 = _script_current(owner, h)
    with tenant_conn(rw, h) as cn:
        v2 = create_edited_version(cn, h, sc, v1, _content)  # app_rw + RLS 경로에서 실제 동작
    assert v2 != v1
    with pytest.raises(Conflict):
        with tenant_conn(rw, h) as cn:
            create_edited_version(cn, h, sc, v1, _content)   # 구버전 기대 → Conflict

def test_cas_true_concurrency(owner, rw, tenant):
    h = tenant["hospital_id"]; sc, v1 = _script_current(owner, h)
    results = []; barrier = threading.Barrier(2)
    def worker():
        barrier.wait()                                     # 동시에 진입
        try:
            with tenant_conn(rw, h) as cn:
                nv = create_edited_version(cn, h, sc, v1, _content)
            results.append(("ok", nv))
        except Conflict:
            results.append(("conflict", None))
        except Exception as e:
            results.append(("error", repr(e)))
    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts: t.start()
    for t in ts: t.join()
    oks = [r for r in results if r[0] == "ok"]
    conflicts = [r for r in results if r[0] == "conflict"]
    assert len(oks) == 1 and len(conflicts) == 1, results  # 정확히 한 명만 성공
    with owner.connect() as cn:
        nos = [r[0] for r in cn.execute(text("select version_no from script_versions where script_id=:s order by version_no"), {"s": sc})]
    assert nos == [1, 2]                                    # version_no 중복 없음

# ── lease (owner/마이그레이션 role) ──
def _pending(owner, h):
    iid = uuid.uuid4()
    with owner.begin() as cn:
        cn.execute(text("insert into migration_imports(id,hospital_id,source_uri,raw_sha256,migration_version,status) "
                        "values(:i,:h,'u','sha','m1','pending')"), {"i": iid, "h": h})
    return iid

def test_lease_acquire_and_contention(owner, tenant):
    iid = _pending(owner, tenant["hospital_id"])
    a = repo.acquire_lease(owner, "w1"); assert a and a["id"] == iid
    assert repo.acquire_lease(owner, "w2") is None          # 활성 lease → 못 얻음

def test_heartbeat_fencing(owner, tenant):
    iid = _pending(owner, tenant["hospital_id"])
    a = repo.acquire_lease(owner, "w1")
    assert repo.heartbeat(owner, iid, uuid.uuid4()) == 0    # 다른 토큰 거부
    assert repo.heartbeat(owner, iid, a["lease_token"]) == 1

def test_expired_lease_takeover(owner, tenant):
    iid = _pending(owner, tenant["hospital_id"])
    a = repo.acquire_lease(owner, "w1")
    with owner.begin() as cn:
        cn.execute(text("update migration_imports set lease_expires_at=now()-interval '1 min' where id=:i"), {"i": iid})
    b = repo.acquire_lease(owner, "w2"); assert b and b["id"] == iid
    assert repo.heartbeat(owner, iid, a["lease_token"]) == 0  # 만료된 옛 워커 fencing(인계 후 쓰기 거부)
