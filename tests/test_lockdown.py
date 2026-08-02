"""불변 테이블·승인상태 DML 봉쇄 + 권한상승 차단 + 승인버전 콘텐츠 동결."""
import uuid, pytest
from sqlalchemy import text
from store.testkit import sqlstate, new_version, new_block, new_sentence, new_claim
from store.repositories import tenant_conn, approve_version

def test_app_rw_cannot_self_grant_role(owner, rw, tenant):
    """#1 권한상승: app_rw가 membership_roles에 자기 approver 역할 추가 시도 → 차단."""
    h, m = tenant["hospital_id"], tenant["membership_id"]
    with pytest.raises(Exception) as ei:
        with tenant_conn(rw, h, m) as cn:
            cn.execute(text("insert into membership_roles(id,hospital_id,membership_id,role) values(:i,:h,:m,'approver')"),
                       {"i": uuid.uuid4(), "h": h, "m": m})
    assert sqlstate(ei.value) == "42501"

def test_app_rw_cannot_insert_into_approved_version(owner, rw, tenant):
    """#4 불변 우회: 승인된 버전에 블록 추가 시도 → 동결 트리거 차단."""
    h, m = tenant["hospital_id"], tenant["membership_id"]
    with owner.begin() as cn:
        sc, v = new_version(cn, h); b = new_block(cn, h, v); s = new_sentence(cn, h, v, b); c = new_claim(cn, h, v, s)
        cn.execute(text("update scripts set current_version_id=:v where id=:s"), {"v": v, "s": sc})  # 승인 대상=current
        cn.execute(text("insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,"
                        "support_level,verification_status,medical_risk) values(:i,:h,:c,'automated','a','direct','verified','low')"),
                   {"i": uuid.uuid4(), "h": h, "c": c})
        cn.execute(text("insert into membership_roles(id,hospital_id,membership_id,role) values(:i,:h,:m,'approver')"),
                   {"i": uuid.uuid4(), "h": h, "m": m})
    with tenant_conn(rw, h, m) as cn:
        approve_version(cn, h, v, "policy-1")             # 승인
    with pytest.raises(Exception) as ei:                  # 승인 후 블록 추가 시도
        with tenant_conn(rw, h, m) as cn:
            cn.execute(text("insert into script_blocks(id,hospital_id,version_id,stable_block_key,order_index,block_type,text) "
                            "values(:b,:h,:v,'k2',9,'explanation','x')"), {"b": uuid.uuid4(), "h": h, "v": v})
    assert sqlstate(ei.value) == "P2013"                  # frozen 트리거(decided_version_frozen)

def _seed_link(owner, h):
    import hashlib
    with owner.begin() as cn:
        _, v = new_version(cn, h)
        lid = uuid.uuid4()
        cn.execute(text("insert into review_links(id,hospital_id,version_id,token_hash,permission,expires_at) "
                        "values(:i,:h,:v,:t,'comment_only',now()+interval '1 day')"),
                   {"i": lid, "h": h, "v": v, "t": hashlib.sha256(lid.bytes).digest()})
    return lid

@pytest.mark.parametrize("sql", [
    "update review_links set revoked_at=now() where id=:l",
    "delete from review_links where id=:l",
    "insert into review_links(id,hospital_id,version_id,token_hash,permission,expires_at) "
    "select gen_random_uuid(),:h,version_id,'\\x00','approve',now()+interval '1 day' from review_links where id=:l",
])
def test_app_rw_cannot_direct_dml_review_links(owner, rw, tenant, sql):
    """review_links 직접 UPDATE/DELETE/INSERT(approve 링크 재생성 포함) 차단 → 함수로만."""
    h, m = tenant["hospital_id"], tenant["membership_id"]
    lid = _seed_link(owner, h)
    with pytest.raises(Exception) as ei:
        with tenant_conn(rw, h, m) as cn:
            cn.execute(text(sql), {"l": lid, "h": h})
    assert sqlstate(ei.value) == "42501"

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
