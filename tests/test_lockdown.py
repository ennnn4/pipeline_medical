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

def test_app_rw_cannot_insert_approved_state(owner, rw, tenant):
    """app_rw가 status='approved' 행을 직접 INSERT해 fn_approve 우회하려는 시도 차단."""
    import uuid
    h = tenant["hospital_id"]
    with owner.begin() as cn:
        sc, v = new_version(cn, h)   # 이미 'none' 행 존재 → 새 버전 만들어 그 버전에 approved 위조 시도
        _, v2 = new_version(cn, h)
    with pytest.raises(Exception) as ei:
        with tenant_conn(rw, h) as cn:
            cn.execute(text("insert into version_approval_states(id,hospital_id,version_id,status,approver_membership_id,"
                            "assessment_set_hash,version_content_hash,compliance_policy_version,decided_at) "
                            "values(:i,:h,:v,'approved',:m,'x','y','p',now())"),
                       {"i": uuid.uuid4(), "h": h, "v": v2, "m": tenant["membership_id"]})
    assert sqlstate(ei.value) == "42501"      # RLS WITH CHECK: INSERT는 status='none'만
