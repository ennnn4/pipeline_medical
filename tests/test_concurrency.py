"""동시 편집 CAS(app_rw + 진짜 병렬) + 마이그레이션 lease(owner/BYPASSRLS)."""
import uuid, threading, time, pytest
from sqlalchemy import text
from store.testkit import new_version, new_block
from store import repositories as repo
import store.rls_sql as R
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

def test_create_edited_rejects_noop_content(owner, rw, tenant):
    h = tenant["hospital_id"]; sc, v1 = _script_current(owner, h)
    with pytest.raises(ValueError):                        # no-op content_fn → 블록 0 → 거부
        with tenant_conn(rw, h) as cn:
            create_edited_version(cn, h, sc, v1, lambda cn, h, v: None)

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

def test_advisory_lock_blocks_content_during_approval(owner, rw, tenant):
    """한 트랜잭션이 version advisory lock 보유 중 다른 트랜잭션의 콘텐츠 INSERT가 '실제로 대기'함을 증명."""
    h, m = tenant["hospital_id"], tenant["membership_id"]
    with owner.begin() as cn:
        _, v = new_version(cn, h)
    acquired = threading.Event(); release = threading.Event(); inserted = threading.Event()
    def holder():                                          # lock 보유(승인 함수와 동일 키)
        with tenant_conn(rw, h, m) as cn:
            cn.execute(text("select pg_advisory_xact_lock(hashtextextended(:v,0))"), {"v": str(v)})
            acquired.set()                                 # 획득 신호(sleep 추정 대신 결정적)
            release.wait(5)
    def inserter():                                        # 콘텐츠 INSERT(트리거가 같은 lock 시도 → 대기)
        with tenant_conn(rw, h, m) as cn:
            cn.execute(text("insert into script_blocks(id,hospital_id,version_id,stable_block_key,order_index,block_type,text) "
                            "values(:b,:h,:v,'k9',9,'explanation','x')"), {"b": uuid.uuid4(), "h": h, "v": v})
        inserted.set()
    th = threading.Thread(target=holder); th.start()
    assert acquired.wait(5)                                # holder가 lock 획득 확인 후
    ti = threading.Thread(target=inserter); ti.start()
    assert not inserted.wait(0.6)                          # ← lock 때문에 INSERT가 대기 중
    release.set(); ti.join(5); th.join(5)
    assert inserted.is_set()                               # holder 릴리스 후 INSERT 진행

def test_rls_apply_idempotent(owner):
    """rls_sql.apply()를 두 번 실행해도 실패하지 않음(정책/함수/트리거 재적용)."""
    R.apply(owner)                                         # base_url이 이미 1회, 여기서 2회째
    # R.apply는 fn_approve_version을 rls_sql 기본형으로 되돌리므로, 이후 테스트를 위해 Step4 함수 복원
    from store.approval_fns import ensure_approval_fns
    ensure_approval_fns(owner)

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
