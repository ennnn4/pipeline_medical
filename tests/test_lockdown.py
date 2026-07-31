"""불변 테이블·승인상태 DML 봉쇄 — app_rw는 UPDATE/DELETE 불가(42501)."""
import uuid, pytest
from sqlalchemy import text
from store.testkit import sqlstate, new_version, new_block
from store.repositories import tenant_conn

def test_app_rw_cannot_update_immutable_block(owner, rw, tenant):
    h = tenant["hospital_id"]
    with owner.begin() as cn:
        _, v = new_version(cn, h); b = new_block(cn, h, v)
    with pytest.raises(Exception) as ei:
        with tenant_conn(rw, h) as cn:
            cn.execute(text("update script_blocks set text='x' where id=:b"), {"b": b})
    assert sqlstate(ei.value) == "42501"

def test_app_rw_cannot_delete_version(owner, rw, tenant):
    h = tenant["hospital_id"]
    with owner.begin() as cn:
        _, v = new_version(cn, h)
    with pytest.raises(Exception) as ei:
        with tenant_conn(rw, h) as cn:
            cn.execute(text("delete from script_versions where id=:v"), {"v": v})
    assert sqlstate(ei.value) == "42501"

def test_app_rw_cannot_direct_update_approval_state(owner, rw, tenant):
    h = tenant["hospital_id"]
    with owner.begin() as cn:
        _, v = new_version(cn, h)
    with pytest.raises(Exception) as ei:
        with tenant_conn(rw, h) as cn:
            cn.execute(text("update version_approval_states set status='approved' where version_id=:v"), {"v": v})
    assert sqlstate(ei.value) == "42501"      # 승인은 fn_approve_version 으로만
