"""스토리보드 편집 오버레이 — 예쁜 렌더는 유지하고, edit 주면 대사 ✏️수정 + 장면 AI사진 얹힘."""
from render.render import render

_PKG = {"title": "이명편", "hook": "훅", "script": [
    {"tc": "0:00–0:10", "block": "인트로", "tags": [], "scene": "원장 정면", "say": "원래 대사"}]}
_EDIT = {"by_idx": {0: {"key": "blk_1", "text": "PG 현재 대사"}},
         "csrf": '<input name=_csrf>', "rt": '<input name=return_to>', "version_id": "v1",
         "edit_url": "/scripts/boncure/s1/edit",
         "img_url": lambda k: f"/scripts/boncure/img/{k}",
         "has_img": lambda k: True, "has_prev": lambda k: True,
         "regen_url": lambda k: f"/scripts/boncure/versions/v1/blocks/{k}/regen-image",
         "revert_url": lambda k: f"/scripts/boncure/versions/v1/blocks/{k}/revert-image",
         "upload_url": lambda k: f"/scripts/boncure/versions/v1/blocks/{k}/upload-image"}


def test_static_render_unchanged_without_edit():
    h = render(_PKG, {"host": "송정현"})
    assert "✏️ 수정" not in h and "edit__blk_1" not in h   # 편집 미주입 = 기존 정적과 동일
    assert "🎬 화면" in h and "🎙 대사" in h and "원래 대사" in h  # 예쁜 스토리보드 그대로


def test_edit_overlay_adds_dialogue_edit_and_ai_image():
    h = render(_PKG, {"host": "송정현"}, edit=_EDIT)
    assert "PG 현재 대사" in h                              # 대사=PG 현재본
    assert "✏️ 수정" in h and 'name="edit__blk_1"' in h     # 대사 편집 버튼·폼
    assert 'action="/scripts/boncure/s1/edit"' in h and 'value="v1"' in h
    assert "/scripts/boncure/img/blk_1" in h               # 이 장면 AI 사진
    assert "🎨 AI 다시" in h and "↩ 이전" in h and "내 사진 올리기" in h  # 다시·이전·업로드
    assert "multipart/form-data" in h                      # 업로드 form
    assert "🎬 화면" in h                                   # 스토리보드 화면 지시 유지