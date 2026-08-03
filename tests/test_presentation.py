"""공유 presentation(Step 7A) — 버전페이지·패널이 주요 요소를 정확히 렌더하는지(주요 요소 스냅샷).
Flask 없이 순수 함수 호출로 검증(데이터+URL 어댑터+csrf 주입)."""
from presentation import render
from presentation.urls import StudioUrls


def _u(path):                               # 프리픽스 없는 단독 실행 시뮬(script_root='')
    return path


def _urls():
    return StudioUrls(_u, "boncure")


def _claim(**kw):
    base = dict(id="c1", claim_text="이명은 THI로 측정한다", verification_status="verified",
                support_level="direct", assessment_kind="automated",
                source_title="김성은 2025", source_quote="THI 54→2", rationale="원문 일치")
    base.update(kw)
    return base


def _ws(**kw):
    base = dict(version_id="v-1", script_id="s-1", version_no=3, parent_version_id="v-0",
                is_current=True, stale=True, approval_status="none",
                blocks=[{"stable_block_key": "blk_1", "block_type": "explanation", "text": "안녕하세요"}],
                claims=[_claim()], img_keys={"blk_1"},
                images_status={"blk_1": {"stale": False}},
                available_actions={"can_edit": True, "can_approve": True, "can_revoke": False, "can_export": False})
    base.update(kw)
    return base


def test_urls_adapter_paths():
    u = _urls()
    assert u.version("v9") == "/ui/h/boncure/versions/v9"
    assert u.edit("s9") == "/ui/h/boncure/scripts/s9/edit"
    assert u.approve("v9").endswith("/versions/v9/approve")
    assert u.export("s9", "v9") == "/api/h/boncure/scripts/s9/versions/v9/export"
    assert u.diff("v9", "v8").endswith("/versions/v9/diff?from=v8")
    assert u.img("blk_1") == "/img/h/boncure/blk_1"


def test_version_page_current_shows_editform_and_approve():
    html = render.version_page(_ws(), _urls(), "TOK", msg_code="edited")
    assert "<title>버전 3" in html and "버전 v3" in html
    assert 'name="edit__blk_1"' in html                      # 편집 textarea(현재 버전)
    assert 'action="/ui/h/boncure/scripts/s-1/edit"' in html
    assert 'value="TOK"' in html                             # csrf 주입
    assert "/versions/v-1/approve" in html and "/versions/v-1/reject" in html  # 승인 가능
    assert "승인 철회" not in html                            # can_revoke=False
    assert "새 버전이 생성되었습니다" in html                 # msg_code=edited 배너
    assert "미승인/stale" in html                            # stale 배지
    assert "/img/h/boncure/blk_1" in html and "근거 검증" in html  # 이미지·근거 패널


def test_version_page_noncurrent_is_readonly():
    html = render.version_page(_ws(is_current=False, available_actions={
        "can_edit": False, "can_approve": False, "can_revoke": True, "can_export": True}), _urls(), "TOK")
    assert "현재 버전이 아니라 편집할 수 없습니다" in html
    assert "/scripts/s-1/edit" not in html                   # 편집 제출 폼 없음(불변)
    assert "편집 저장" not in html
    assert "승인 철회" in html and "export(JSON)" in html     # can_revoke=True


def test_evidence_panel_counts_and_quote():
    claims = [_claim(verification_status="verified"), _claim(id="c2", verification_status="failed"),
              _claim(id="c3", verification_status="pending", support_level=None, source_quote=None)]
    html = render.evidence_panel(claims, True, _urls(), "TOK", "v-1", "s-1")
    assert "검증됨 <b style=\"color:#12b886\">1</b>" in html
    assert "반려/실패 <b style=\"color:#f04452\">1</b>" in html
    assert "THI 54→2" in html                                # 원문 인용
    assert "/ui/h/boncure/claims/c1/review" in html          # 검수 버튼(현재 버전)


def test_images_panel_stale_badge():
    st = {"blk_1": {"stale": True, "reason": "source_scene_changed"}}
    html = render.images_panel([{"stable_block_key": "blk_1", "block_type": "explanation"}],
                               {"blk_1"}, True, st, _urls(), "TOK", "v-1")
    assert "대본 변경됨 — 재생성 권장" in html and "/img/h/boncure/blk_1" in html


def test_page_shell_has_dashboard_nav():
    html = render.page("제목", "<p>본문</p>")
    assert "← 대시보드" in html and "<title>제목" in html and "<p>본문</p>" in html


def test_version_page_xss_escaping():
    # GPT A유지 조건: f-string 렌더링은 자동 escaping이 없으므로 XSS 회귀 관문 필수.
    # DB/사용자값(블록 key·본문·block_type·claim·source·식별자)이 본문·속성 어디서도 살아있는 태그로 새면 안 됨.
    X = '"><script>alert(1)</script>'
    ws = dict(version_id=X, script_id=X, version_no=1, parent_version_id=None,
              is_current=True, stale=False, approval_status="none",
              blocks=[{"stable_block_key": X, "block_type": X,
                       "text": "</textarea><script>alert(2)</script>"}],
              claims=[{"id": X, "claim_text": "<img src=x onerror=alert(3)>",
                       "verification_status": "verified", "support_level": "direct",
                       "assessment_kind": "automated", "source_title": '"><script>alert(4)</script>',
                       "source_quote": "q", "rationale": "r"}],
              img_keys={X}, images_status={},
              available_actions={"can_edit": True, "can_approve": True, "can_revoke": True, "can_export": True})
    html = render.version_page(ws, _urls(), "TOK")
    assert "<script>alert" not in html                    # 주입 script 태그 없음
    assert "</textarea><script>" not in html               # textarea 본문 breakout 없음
    assert '"><script>' not in html                        # 속성 breakout 없음(key·식별자)
    assert "onerror=alert(3)>" not in html                 # 살아있는 img 이벤트 핸들러 없음
    assert "&lt;script&gt;" in html                        # escape가 실제로 적용됨
    assert "&lt;img src=x onerror=alert(3)&gt;" in html    # claim은 텍스트로만 렌더
