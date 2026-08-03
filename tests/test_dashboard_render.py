"""Step 7B — 대시보드 canonical route(/scripts/<h>/<version_id>)가 공유 presentation으로
버전페이지를 '직접 렌더'하는지(리다이렉트 아님). 쓰기·자산은 /studio compat로 향하는지."""
import pytest
from presentation.urls import DashboardUrls, StudioUrls
from presentation import render
from tests.test_api import _setup


def test_dashboard_urls_all_canonical():
    u = DashboardUrls("boncure")
    # 읽기·쓰기·자산 전부 대시보드 canonical(/scripts) — studio 미경유(diff 디버그만 잔여)
    assert u.version("v9") == "/scripts/boncure/v9"
    assert u.dashboard() == "/" and u.logout() == "/logout"
    assert u.edit("s9") == "/scripts/boncure/s9/edit"
    assert u.approve("v9") == "/scripts/boncure/versions/v9/approve"
    assert u.export("s9", "v9") == "/scripts/boncure/s9/versions/v9/export"
    assert u.img("blk_1") == "/scripts/boncure/img/blk_1"
    assert u.regen("v9", "blk_1") == "/scripts/boncure/versions/v9/blocks/blk_1/regen-image"
    assert u.revert("v9", "blk_1") == "/scripts/boncure/versions/v9/blocks/blk_1/revert-image"


def test_version_page_with_dashboard_urls_targets():
    ws = dict(version_id="v-1", script_id="s-1", version_no=2, parent_version_id=None,
              is_current=True, stale=True, approval_status="none",
              blocks=[{"stable_block_key": "blk_1", "block_type": "explanation", "text": "안녕"}],
              claims=[], img_keys=set(), images_status={},
              available_actions={"can_edit": True, "can_approve": True, "can_revoke": False, "can_export": False})
    html = render.version_page(ws, DashboardUrls("boncure"), "TOK")
    assert 'action="/scripts/boncure/s-1/edit"' in html                    # 편집 → 대시보드
    assert 'action="/scripts/boncure/versions/v-1/approve"' in html        # 승인 → 대시보드
    assert "/studio/" not in html                                          # studio 미경유
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
    assert f'action="/scripts/{d["slug"]}/{d["script_id"]}/edit"' in html  # 쓰기도 대시보드 canonical
    assert "/studio/" not in html                                        # studio 미경유


def test_studio_dashboard_read_parity(dash_client, rw, owner):
    # Step 8 읽기 parity — 같은 버전을 studio·대시보드로 렌더 시 핵심 요소가 동등(액션 URL 프리픽스만 다름).
    from web.api import create_app
    studio = create_app(engine=rw); studio.config["TESTING"] = True
    sc = studio.test_client()
    d = _setup(owner, role="approver", verified_claim=True)
    for c in (sc, dash_client):
        with c.session_transaction() as s:
            s["user"] = "tester"; s["user_id"] = str(d["user_id"])
    s_html = sc.get(f"/ui/h/{d['slug']}/versions/{d['version_id']}").get_data(as_text=True)
    b_html = dash_client.get(f"/scripts/{d['slug']}/{d['version_id']}").get_data(as_text=True)
    for token in ('name="edit__blk_1"', 'name="edit__blk_2"', "근거 검증", "<title>버전 1", "✅ 승인"):
        assert token in s_html and token in b_html                        # 읽기·액션 가용성 동등(구조 parity)
    assert f'/scripts/{d["slug"]}/' in b_html and "/studio/" not in b_html  # 대시보드는 전부 canonical
    assert f'/ui/h/{d["slug"]}/' in s_html                                # studio는 자기 경로


def test_dashboard_edit_route_saves_on_dashboard(dash_client, owner):
    # Step 9: 편집 저장이 대시보드 라우트에서 처리되고 대시보드로 redirect(studio 미경유).
    d = _setup(owner, role="editor")
    with dash_client.session_transaction() as s:
        s["user"] = "tester"; s["user_id"] = str(d["user_id"]); s["_csrf"] = "tok"
    r = dash_client.post(f"/scripts/{d['slug']}/{d['script_id']}/edit",
                         data={"expected": str(d["version_id"]), "edit__blk_2": "새 문장입니다.", "_csrf": "tok"})
    loc = r.headers.get("Location", "")
    assert r.status_code == 302 and loc.startswith(f"/scripts/{d['slug']}/") and "/studio" not in loc
    assert "m=edited" in loc                                   # 새 버전 생성됨


def test_dashboard_http_obs_tags_canonical_surface(dash_client, owner, caplog):
    import json, logging
    d = _setup(owner, role="editor")
    with dash_client.session_transaction() as s:
        s["user"] = "tester"; s["user_id"] = str(d["user_id"])
    with caplog.at_level(logging.INFO, logger="boncure.obs"):
        dash_client.get(f"/scripts/{d['slug']}/{d['version_id']}")
    evs = [json.loads(r.message) for r in caplog.records if r.name == "boncure.obs"]
    http = [e for e in evs if e.get("obs") == "http" and e.get("endpoint") == "scripts_edit"]
    assert http and http[0]["surface"] == "dashboard_canonical" and http[0]["compat"] is False
    assert http[0]["app"] == "dashboard" and "latency_ms" in http[0]
