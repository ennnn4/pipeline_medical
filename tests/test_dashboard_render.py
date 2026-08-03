"""Step 7B — 대시보드 canonical route(/scripts/<h>/<version_id>)가 공유 presentation으로
버전페이지를 '직접 렌더'하는지(리다이렉트 아님). 쓰기·자산은 /studio compat로 향하는지."""
import pytest
from presentation.urls import DashboardUrls, StudioUrls
from presentation import render
from tests.test_api import _setup


def test_dashboard_urls_read_canonical_write_studio():
    u = DashboardUrls("boncure")
    # 읽기·nav = 대시보드 canonical
    assert u.version("v9") == "/scripts/boncure/v9"
    assert u.dashboard() == "/" and u.logout() == "/logout"
    # 쓰기·자산 = /studio compat(세션·CSRF 공유)
    assert u.edit("s9") == "/studio/ui/h/boncure/scripts/s9/edit"
    assert u.approve("v9") == "/studio/ui/h/boncure/versions/v9/approve"
    assert u.export("s9", "v9") == "/studio/api/h/boncure/scripts/s9/versions/v9/export"
    assert u.img("blk_1") == "/studio/img/h/boncure/blk_1"


def test_version_page_with_dashboard_urls_targets():
    ws = dict(version_id="v-1", script_id="s-1", version_no=2, parent_version_id=None,
              is_current=True, stale=True, approval_status="none",
              blocks=[{"stable_block_key": "blk_1", "block_type": "explanation", "text": "안녕"}],
              claims=[], img_keys=set(), images_status={},
              available_actions={"can_edit": True, "can_approve": True, "can_revoke": False, "can_export": False})
    html = render.version_page(ws, DashboardUrls("boncure"), "TOK")
    assert 'action="/studio/ui/h/boncure/scripts/s-1/edit"' in html        # 편집 → /studio
    assert 'action="/studio/ui/h/boncure/versions/v-1/approve"' in html    # 승인 → /studio
    assert "href='/'" in html                                              # 대시보드 nav


@pytest.fixture
def dash_client(rw, monkeypatch):
    import store.db
    monkeypatch.setattr(store.db, "make_engine", lambda *a, **k: rw)   # 라우트가 호출 시점에 조회
    import app as dashboard
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


def test_dashboard_route_renders_not_redirects(dash_client, owner):
    d = _setup(owner, role="editor", verified_claim=True)
    with dash_client.session_transaction() as s:
        s["user"] = "tester"; s["user_id"] = str(d["user_id"])
    r = dash_client.get(f"/scripts/{d['slug']}/{d['version_id']}")
    html = r.get_data(as_text=True)
    assert r.status_code == 200                                           # 리다이렉트(302) 아님
    assert "<title>버전 1" in html and 'name="edit__blk_1"' in html        # 대시보드가 직접 렌더
    assert "/studio/ui/h/" in html                                        # 쓰기 액션은 /studio compat
