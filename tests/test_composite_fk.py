"""3열 복합 FK — 같은 병원 안에서도 다른 script/version 계보를 섞으면 거부(23503)."""
import uuid, pytest
from sqlalchemy import text
from store.testkit import sqlstate, new_version, new_block, new_sentence, new_claim

FK_VIOLATION = "23503"

def _expect_fk(owner, fn):
    with pytest.raises(Exception) as ei:
        with owner.begin() as cn:
            fn(cn)
    assert sqlstate(ei.value) == FK_VIOLATION, f"expected 23503, got {sqlstate(ei.value)}"

def test_claim_version_ne_sentence_version(owner, tenant):
    h = tenant["hospital_id"]
    with owner.begin() as cn:
        _, v1 = new_version(cn, h); _, v2 = new_version(cn, h)
        b = new_block(cn, h, v1); s = new_sentence(cn, h, v1, b)
    # claim: version=v2 이지만 sentence는 v1 소속 → 23503
    _expect_fk(owner, lambda cn: cn.execute(text(
        "insert into claims(id,hospital_id,version_id,sentence_id,claim_index,claim_text,claim_type,detection_method) "
        "values(:c,:h,:v,:s,7,'x','statistic','migration')"), {"c": uuid.uuid4(), "h": h, "v": v2, "s": s}))

def test_comment_version_ne_block_version(owner, tenant):
    h = tenant["hospital_id"]
    with owner.begin() as cn:
        _, v1 = new_version(cn, h); _, v2 = new_version(cn, h)
        b = new_block(cn, h, v1)
    _expect_fk(owner, lambda cn: cn.execute(text(
        "insert into review_comments(id,hospital_id,version_id,block_id,reviewer_name,comment,author_membership_id) "
        "values(:i,:h,:v,:b,'r','c',:m)"),
        {"i": uuid.uuid4(), "h": h, "v": v2, "b": b, "m": tenant["membership_id"]}))

def test_comment_version_ne_link_version(owner, tenant):
    h = tenant["hospital_id"]
    with owner.begin() as cn:
        _, v1 = new_version(cn, h); _, v2 = new_version(cn, h)
        link = uuid.uuid4()
        cn.execute(text("insert into review_links(id,hospital_id,version_id,token_hash,permission,expires_at) "
                        "values(:i,:h,:v,:t,'comment_only',now()+interval '1 day')"),
                   {"i": link, "h": h, "v": v1, "t": b"tok" + link.bytes})
    # comment.version=v2 이지만 link는 v1 → 23503
    _expect_fk(owner, lambda cn: cn.execute(text(
        "insert into review_comments(id,hospital_id,version_id,review_link_id,reviewer_name,comment) "
        "values(:i,:h,:v,:l,'r','c')"), {"i": uuid.uuid4(), "h": h, "v": v2, "l": link}))

def test_edit_script_ne_version_script(owner, tenant):
    h = tenant["hospital_id"]
    with owner.begin() as cn:
        sc1, v1 = new_version(cn, h); sc2, v2 = new_version(cn, h)
    # edit.script=sc1 이지만 to_version=v2(sc2 소속) → 23503
    _expect_fk(owner, lambda cn: cn.execute(text(
        "insert into edits(id,hospital_id,script_id,to_version_id,stable_block_key,category) "
        "values(:i,:h,:s,:v,'blk_0','tone')"), {"i": uuid.uuid4(), "h": h, "s": sc1, "v": v2}))

def test_current_version_other_script(owner, tenant):
    h = tenant["hospital_id"]
    with owner.begin() as cn:
        sc1, v1 = new_version(cn, h); sc2, v2 = new_version(cn, h)
    # sc1.current_version = v2(sc2 소속) → 23503
    _expect_fk(owner, lambda cn: cn.execute(text(
        "update scripts set current_version_id=:v where id=:s"), {"v": v2, "s": sc1}))

def test_parent_version_other_script(owner, tenant):
    h = tenant["hospital_id"]
    with owner.begin() as cn:
        sc1, v1 = new_version(cn, h); sc2, v2 = new_version(cn, h)
    # sc1의 새 버전인데 parent=v2(sc2) → 23503
    _expect_fk(owner, lambda cn: cn.execute(text(
        "insert into script_versions(id,hospital_id,script_id,parent_version_id,version_no,source) "
        "values(:i,:h,:s,:p,2,'editor')"), {"i": uuid.uuid4(), "h": h, "s": sc1, "p": v2}))
