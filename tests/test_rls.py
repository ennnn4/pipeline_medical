"""RLS·connection pool — 테넌트 격리, 컨텍스트 누수 없음, 교차 테넌트 차단."""
import uuid, pytest
from sqlalchemy import text
from store.testkit import set_tenant, sqlstate

def _hospital(owner):
    h = uuid.uuid4()
    with owner.begin() as cn:
        cn.execute(text("insert into hospitals(id,slug,name) values(:h,:s,'T')"), {"h": h, "s": "h" + h.hex[:10]})
    return h

def _seed_scripts(owner, h, n):
    with owner.begin() as cn:
        for _ in range(n):
            cn.execute(text("insert into scripts(id,hospital_id,topic) values(:i,:h,'a')"), {"i": uuid.uuid4(), "h": h})

def test_select_isolation_and_pool_no_leak(owner, rw):
    h1, h2 = _hospital(owner), _hospital(owner)
    _seed_scripts(owner, h1, 2); _seed_scripts(owner, h2, 1)
    with rw.connect() as cn:                       # 같은 pooled connection 재사용
        with cn.begin():
            set_tenant(cn, h1)
            assert cn.execute(text("select count(*) from scripts")).scalar() == 2
        with cn.begin():                            # 컨텍스트 미설정(누수 검사)
            assert cn.execute(text("select count(*) from scripts")).scalar() == 0
        with cn.begin():
            set_tenant(cn, h2)
            assert cn.execute(text("select count(*) from scripts")).scalar() == 1

def test_cross_tenant_insert_blocked(owner, rw):
    h1, h2 = _hospital(owner), _hospital(owner)
    with pytest.raises(Exception) as ei:
        with rw.connect() as cn, cn.begin():
            set_tenant(cn, h1)
            cn.execute(text("insert into scripts(id,hospital_id,topic) values(:i,:h,'x')"), {"i": uuid.uuid4(), "h": h2})
    assert sqlstate(ei.value) == "42501"           # WITH CHECK 위반

def test_cross_tenant_update_hospital_blocked(owner, rw):
    h1, h2 = _hospital(owner), _hospital(owner)
    _seed_scripts(owner, h1, 1)
    with pytest.raises(Exception) as ei:
        with rw.connect() as cn, cn.begin():
            set_tenant(cn, h1)
            cn.execute(text("update scripts set hospital_id=:h2 where hospital_id=:h1"), {"h1": h1, "h2": h2})
    assert sqlstate(ei.value) == "42501"

def test_failed_transaction_then_reuse(owner, rw):
    h1 = _hospital(owner); _seed_scripts(owner, h1, 1)
    with rw.connect() as cn:
        try:
            with cn.begin():
                set_tenant(cn, h1)
                cn.execute(text("select 1/0"))     # 에러로 트랜잭션 중단
        except Exception:
            pass
        with cn.begin():                            # 같은 connection 재사용 정상
            set_tenant(cn, h1)
            assert cn.execute(text("select count(*) from scripts")).scalar() == 1

def test_view_isolation(owner, rw):
    # security_invoker 뷰도 테넌트 격리(호출자 RLS 적용)
    h1 = _hospital(owner)
    with rw.connect() as cn, cn.begin():
        set_tenant(cn, h1)
        # 다른 병원 데이터는 안 보임(0). 자기 병원엔 아직 assessment 없음 → 0
        assert cn.execute(text("select count(*) from claim_effective_assessment")).scalar() == 0
