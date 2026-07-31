"""review_links 권한·토큰교환 — 직접 SELECT 42501, exchange만 조회, 만료/폐기/오digest 0행."""
import hashlib, uuid, pytest
from sqlalchemy import text
from store.testkit import sqlstate, new_version

def _link(owner, h, v, token, expires="now()+interval '1 day'", revoked="null"):
    d = hashlib.sha256(token).digest()
    with owner.begin() as cn:
        cn.execute(text(f"insert into review_links(id,hospital_id,version_id,token_hash,permission,expires_at,revoked_at) "
                        f"values(:i,:h,:v,:t,'comment_only',{expires},{revoked})"),
                   {"i": uuid.uuid4(), "h": h, "v": v, "t": d})
    return d

def test_app_rw_direct_select_denied(owner, rw, tenant):
    with pytest.raises(Exception) as ei:
        with rw.connect() as cn:
            cn.execute(text("select * from review_links")).fetchall()
    assert sqlstate(ei.value) == "42501"

def test_exchange_valid_and_edge(owner, rw, tenant):
    h = tenant["hospital_id"]
    with owner.begin() as cn:
        _, v = new_version(cn, h)
    good = _link(owner, h, v, b"good")
    _link(owner, h, v, b"exp", expires="now()-interval '1 day'")
    _link(owner, h, v, b"rev", revoked="now()")
    with rw.connect() as cn:
        assert len(cn.execute(text("select * from exchange_review_token(:d)"), {"d": good}).fetchall()) == 1
        assert len(cn.execute(text("select * from exchange_review_token(:d)"),
                              {"d": hashlib.sha256(b"nope").digest()}).fetchall()) == 0
        assert len(cn.execute(text("select * from exchange_review_token(:d)"),
                              {"d": hashlib.sha256(b"exp").digest()}).fetchall()) == 0
        assert len(cn.execute(text("select * from exchange_review_token(:d)"),
                              {"d": hashlib.sha256(b"rev").digest()}).fetchall()) == 0

def test_app_auth_cannot_execute(owner):
    with pytest.raises(Exception) as ei:
        with owner.connect() as cn:
            cn.execute(text("set role app_auth"))
            cn.execute(text("select * from exchange_review_token(:d)"), {"d": b"x" * 32}).fetchall()
    assert sqlstate(ei.value) == "42501"
