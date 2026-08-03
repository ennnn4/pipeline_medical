"""공유 렌더 함수 — 버전페이지·근거패널·이미지패널·페이지 셸.

Flask 미의존: 데이터 + URL 어댑터(presentation.urls.StudioUrls) + csrf 토큰 + branding만 받는다.
web/api.py f-string 렌더링을 이관(동작 보존). 7B에서 대시보드 route도 동일 함수를 재사용한다."""
from markupsafe import escape
from presentation.formatters import SUPPORT_KO, KIND_KO, csrf_field, verification_badge

try:
    from web.branding import LOGO_URI, ICON_URI      # data URI(순수 상수, Flask 아님)
except Exception:
    LOGO_URI = ICON_URI = ""

CSS = """:root{--bg:#fff;--surface:#f9fafb;--surface2:#f2f4f6;--card:#fff;--border:#e5e8eb;--ink:#191f28;--ink2:#4e5968;--muted:#8b95a1;--accent:#3182f6;--accw:#eaf2fe;--acci:#1b64da;--good:#12b886;--danger:#f04452;--font:'Pretendard',-apple-system,BlinkMacSystemFont,'Malgun Gothic',system-ui,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--font);line-height:1.6;letter-spacing:-.01em;-webkit-font-smoothing:antialiased}
a{color:var(--acci);text-decoration:none}
.wrap{max-width:900px;margin:0 auto;padding:8px 22px 90px}
h1{font-size:24px;font-weight:800;letter-spacing:-.04em;margin:0 0 6px}h2{font-size:16px;font-weight:800;letter-spacing:-.02em;margin:0 0 12px}
.card{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:22px;box-shadow:0 1px 3px rgba(25,31,40,.03),0 10px 30px rgba(25,31,40,.04);margin-bottom:16px}
.badge{font-size:12px;font-weight:800;padding:4px 11px;border-radius:100px}
.stale{background:#fdeaec;color:var(--danger)}.ok{background:#e6f7f0;color:var(--good)}
label{display:block;font-size:13px;font-weight:700;color:var(--ink2);margin:14px 0 6px}
input,textarea{width:100%;font-family:var(--font);font-size:15px;padding:12px 14px;border:1px solid var(--border);border-radius:12px;background:#fff;color:var(--ink)}
input:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accw)}textarea{min-height:64px;resize:vertical}
.btn{font-family:var(--font);font-weight:700;font-size:15px;padding:11px 18px;border-radius:12px;border:1px solid transparent;background:var(--accent);color:#fff;cursor:pointer;text-decoration:none;display:inline-block;transition:.12s}
.btn:hover{background:#1b6fe0}.btn.g{background:#fff;color:var(--ink);border-color:var(--border)}.btn.g:hover{background:var(--surface2)}
.msg{padding:11px 15px;border-radius:12px;margin:10px 0;font-weight:600;font-size:14px}.msg.e{background:#fdeaec;color:var(--danger)}.msg.s{background:#e6f7f0;color:var(--good)}
.blk{border-top:1px solid var(--border);padding:14px 0}.key{font-size:12px;color:var(--muted);font-weight:700}small{color:var(--muted)}
.thumb{height:92px;border-radius:10px;cursor:pointer;border:1px solid var(--border);object-fit:cover;display:block;margin-top:6px}
.lb{display:none;position:fixed;inset:0;background:rgba(0,0,0,.86);z-index:100;align-items:center;justify-content:center}
.lb img{max-width:92vw;max-height:88vh;border-radius:12px;box-shadow:0 8px 40px rgba(0,0,0,.5)}"""

_LIGHTBOX = """<div class=lb id=lb onclick="if(event.target.id==='lb')this.style.display='none'"><img id=lbimg></div>
<script>
var TH=[].slice.call(document.querySelectorAll('.thumb')),ci=0;
function LB(el){ci=TH.indexOf(el);_sh()}
function _sh(){document.getElementById('lbimg').src=TH[ci].src;document.getElementById('lb').style.display='flex'}
document.addEventListener('keydown',function(e){var lb=document.getElementById('lb');if(!lb||lb.style.display!=='flex')return;
 if(e.key==='ArrowRight'){ci=(ci+1)%TH.length;_sh()}else if(e.key==='ArrowLeft'){ci=(ci-1+TH.length)%TH.length;_sh()}else if(e.key==='Escape')lb.style.display='none';});
</script>"""


def page(title, body, dashboard_href="/"):
    """공통 HTML 셸(로고·대시보드 nav·CSS). dashboard_href=상단 '← 대시보드' 링크."""
    fav = f"<link rel=icon href='{ICON_URI}'>" if ICON_URI else ""
    logo = (f"<img src='{LOGO_URI}' alt='Medical Pipeline' style='height:34px'>" if LOGO_URI
            else "<b style='font-size:17px'>Medical Pipeline</b>")
    nav = (f"<div style='display:flex;align-items:center;gap:12px;padding:16px 0 12px'>{logo}"
           f"<a class='btn g' href='{escape(dashboard_href)}' style='margin-left:auto;padding:8px 14px;font-size:13px'>← 대시보드</a></div>")
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{escape(title)}</title>{fav}<style>{CSS}</style><div class=wrap>{nav}{body}</div>")


def _review_buttons(claim_id, is_current, urls, csrf, version_id, script_id):
    """원장 검수/반려 버튼(사람 판정=human_review, 자동보다 우선). 현재 버전에서만 노출.
    version_id·script_id 명시 전송 → service가 current 재검사·소속 검증(지연 저장 차단)."""
    if not is_current:
        return ""
    act = escape(urls.review(claim_id))     # 속성 삽입 → escape(식별자 방어심층)
    hidden = (f'{csrf_field(csrf)}<input type=hidden name=version_id value="{escape(version_id)}">'
              f'<input type=hidden name=script_id value="{escape(script_id)}">')
    return (f'<div style="margin-top:6px;display:flex;gap:6px">'
            f'<form method=post action="{act}" style="margin:0">{hidden}<input type=hidden name=decision value=confirm>'
            f'<button class=btn style="padding:4px 12px;font-size:13px;background:#12b886">확정</button></form>'
            f'<form method=post action="{act}" style="margin:0">{hidden}<input type=hidden name=decision value=reject>'
            f'<button class=btn style="padding:4px 12px;font-size:13px;background:#f04452">반려</button></form></div>')


def evidence_panel(claims, is_current, urls, csrf, version_id, script_id):
    """버전의 의학주장별 유효 근거판정 + 원문 인용 + 원장 검수/반려."""
    if not claims:
        return ('<div class=card><h2>근거 검증 (4단계)</h2>'
                '<p><small>이 버전에 등록된 의학주장이 없습니다.</small></p></div>')
    verified = sum(1 for c in claims if c["verification_status"] == "verified")
    failed = sum(1 for c in claims if c["verification_status"] == "failed")
    unver = len(claims) - verified - failed
    rows = []
    for c in claims:
        style, label = verification_badge(c["verification_status"])
        sup = (f'<span class="badge" style="background:#eef4ff;color:#3182f6">{SUPPORT_KO.get(c["support_level"], "미검증")}</span>'
               if c["support_level"] else "")
        kind = KIND_KO.get(c["assessment_kind"], "")
        src = (f'<div style="font-size:12px;color:#8b95a1;margin-top:4px">📄 {escape(c["source_title"])}</div>'
               if c["source_title"] else "")
        quote = (f'<div style="font-size:12px;color:#495057;margin-top:4px;padding:8px 10px;background:#f8f9fa;border-radius:8px;border-left:3px solid #d0d5dd">“{escape((c["source_quote"] or "")[:280])}”</div>'
                 if c["source_quote"] and c["support_level"] else "")
        rat = (f'<div style="font-size:12px;color:#8b95a1;margin-top:2px">{escape((c["rationale"] or "")[:200])}</div>'
               if c["rationale"] else "")
        rows.append(
            f'<div class=blk><div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
            f'<span class="badge" style="{style}">{label}</span>{sup}'
            f'{f"<small>{escape(kind)}</small>" if kind else ""}</div>'
            f'<div style="margin-top:6px;font-size:14px">{escape((c["claim_text"] or "")[:220])}</div>'
            f'{src}{quote}{rat}{_review_buttons(c["id"], is_current, urls, csrf, version_id, script_id)}</div>')
    note = ('<p><small>자동검증은 <b>논문 원문을 실제로 대조</b>해 판정합니다(근거 문장 인용). '
            '의학적 근거등급·환자적용의 최종 판단은 원장 몫이며, <b>원장 확정/반려가 자동판정보다 우선</b>합니다.</small></p>')
    summary = (f'검증됨 <b style="color:#12b886">{verified}</b> · '
               f'미검증 <b style="color:#8b95a1">{unver}</b> · 반려/실패 <b style="color:#f04452">{failed}</b> (총 {len(claims)})')
    return f'<div class=card><h2>근거 검증 (4단계)</h2><p>{summary}</p>{note}{"".join(rows)}</div>'


def images_panel(blocks, img_keys, is_current, img_status, urls, csrf, version_id):
    """장면별 AI 이미지 썸네일(클릭=라이트박스) + 피드백 재생성 폼."""
    if not img_keys:
        return ""
    cells = []
    for b in blocks:
        key = b["stable_block_key"]
        if key not in img_keys:
            continue
        regen = (f'<form method=post action="{escape(urls.regen(version_id, key))}" '
                 f'style="display:flex;gap:6px;margin-top:6px">{csrf_field(csrf)}'
                 f'<input name=feedback placeholder="어떻게 바꿀까? (비우면 새 버전으로 재생성)" '
                 f'style="flex:1;font-size:12px;padding:7px;margin:0"><button class="btn g" '
                 f'style="padding:7px 12px;font-size:12px" onclick="this.innerHTML=\'생성중…\'">🎨 다시</button></form>') if is_current else ""
        stx = (img_status or {}).get(key) or {}
        badge = ('<span class="badge stale" style="font-size:11px">⚠ 대본 변경됨 — 재생성 권장</span>' if stx.get("stale") and stx.get("reason") == "source_scene_changed"
                 else '<span class="badge stale" style="font-size:11px">⚠ 출처 미결착(수동 확인)</span>' if stx.get("stale")
                 else "")
        cells.append(
            f'<div class=blk id="img_{escape(key)}"><div class=key>{escape(key)} · {escape((b["block_type"] or "")[:20])} {badge}</div>'
            f'<img class=thumb src="{escape(urls.img(key))}" alt="scene" onclick="LB(this)">{regen}</div>')
    note = ('<p><small>영상용 <b>개념 B롤</b>(AI 생성)입니다. 실제 환자사진·논문 그림이 아니며, '
            '사용 전 저작권·의학표현은 원장 확인. 마음에 안 들면 아래에 적고 “다시”.</small></p>')
    return f'<div class=card><h2>장면 이미지 — 클릭하면 크게, ←→ 넘김</h2>{note}{"".join(cells)}</div>' + _LIGHTBOX


# 버전페이지 안내 메시지(쿼리 m= 코드 → 배너)
VERSION_MESSAGES = {
    "approved": '<div class="msg s">승인되었습니다.</div>',
    "e403": '<div class="msg e">승인 권한(approver)이 없습니다.</div>',
    "e422": '<div class="msg e">미검증/미지원 claim이 있어 승인할 수 없습니다(4단계 근거검증 필요).</div>',
    "edited": '<div class="msg s">새 버전이 생성되었습니다(미승인).</div>',
    "reviewed": '<div class="msg s">원장 검수가 반영되었습니다(자동판정보다 우선).</div>',
    "conflict": '<div class="msg e">현재 버전이 바뀌었거나 승인된 버전이라 반영하지 못했습니다.</div>',
    "rejected": '<div class="msg s">반려되었습니다.</div>',
    "revoked": '<div class="msg s">승인이 철회되었습니다.</div>',
    "regen": '<div class="msg s">이미지를 다시 생성했습니다.</div>',
    "regenfail": '<div class="msg e">이미지 재생성 실패(OpenAI 키/네트워크 확인).</div>',
}


def version_page(ws, urls, csrf, msg_code=None):
    """버전페이지 전체(셸 포함). ws=workspace service 결과, urls=StudioUrls, csrf=세션 토큰."""
    msg = VERSION_MESSAGES.get(msg_code, "")
    script_id = ws["script_id"]; blocks = ws["blocks"]; claims = ws["claims"]
    is_current = ws["is_current"]; stale = ws["stale"]; version_id = ws["version_id"]
    badge = ('<span class="badge stale">미승인/stale</span>' if stale
             else '<span class="badge ok">승인됨</span>')
    rows = "".join(
        f'<div class=blk><div class=key>{escape(b["stable_block_key"])} · {escape(b["block_type"])}</div>'
        f'<textarea name="edit__{escape(b["stable_block_key"])}">{escape(b["text"])}</textarea></div>' for b in blocks)
    editform = ((f'<form method=post action="{escape(urls.edit(script_id))}">{csrf_field(csrf)}'
                 f'<input type=hidden name=expected value="{escape(version_id)}">{rows}'
                 f'<button class=btn type=submit>💾 편집 저장(새 버전 생성)</button></form>') if is_current
                else f'<p><small>이 버전은 현재 버전이 아니라 편집할 수 없습니다(불변).</small></p>{rows}')
    act = ws["available_actions"]
    approve = reject = revoke = export = ""
    if act["can_approve"]:
        approve = (f'<form method=post action="{escape(urls.approve(version_id))}" style="display:inline-block;margin-top:12px">{csrf_field(csrf)}'
                   f'<button class=btn type=submit>✅ 승인</button></form>')
        reject = (f'<form method=post action="{escape(urls.reject(version_id))}" style="display:inline-block;margin-top:12px;margin-left:6px">{csrf_field(csrf)}'
                  f'<input name=reason placeholder="반려 사유" style="padding:6px 8px;font-size:13px">'
                  f'<button class=btn type=submit style="background:#f04452">반려</button></form>')
    if act["can_revoke"]:
        export = f'<a class="btn g" style="margin-left:6px" href="{escape(urls.export(script_id, version_id))}">⬇ export(JSON)</a>'
        revoke = (f'<form method=post action="{escape(urls.revoke(version_id))}" style="display:inline-block;margin-top:12px;margin-left:6px">{csrf_field(csrf)}'
                  f'<input name=reason placeholder="철회 사유" style="padding:6px 8px;font-size:13px">'
                  f'<button class=btn type=submit style="background:#f04452">승인 철회</button></form>')
    diff = (f'<a class="btn g" href="{escape(urls.diff(version_id, ws["parent_version_id"]))}">diff(JSON)</a>'
            if ws["parent_version_id"] else "")
    evidence = evidence_panel(claims, is_current, urls, csrf, version_id, script_id)
    images = images_panel(blocks, ws["img_keys"], is_current, ws["images_status"], urls, csrf, version_id)
    body = (f'<div class=card><h1>버전 v{ws["version_no"]} {badge}</h1>{msg}'
            f'<h2>블록 (편집 → 새 immutable 버전)</h2>{editform}{approve}{reject}{revoke}{export} {diff} '
            f'<a class="btn g" href="{escape(urls.logout())}">로그아웃</a></div>{images}{evidence}')
    return page(f"버전 {ws['version_no']}", body, dashboard_href=urls.dashboard())
