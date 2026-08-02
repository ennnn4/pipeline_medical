"""P0 3단계 앱층 — 편집·승인·diff·버전조회 HTTP API (store/ 기반, 실 PostgreSQL).

SDR 준수:
 - app.hospital_id/app.membership_id는 '서버가 인증 세션에서 결정'해서만 설정(요청 body의 membership 신뢰 금지).
 - request_id를 매 요청 생성해 승인 audit에 배선.
 - 승인은 repositories.approve_version 경로로만(advisory lock 하 hash).
CAS 충돌→409, 역할/권한(42501)→403, 미검증 claim(23514)→422, 없음(P0002)→404.
"""
import os, uuid, secrets, hmac
from contextlib import contextmanager
from services.context import ActorContext
from services import scripts as scripts_service
from services import evidence as evidence_service
from services import approvals as approvals_service
from services import exports as exports_service
from services import images as images_service
from services import workspace as workspace_service
from services.exceptions import ServiceError
from flask import Flask, request, jsonify, session, g, abort, redirect, Response
from markupsafe import escape
from werkzeug.security import check_password_hash
from sqlalchemy import text
from store.db import make_engine
from store import repositories as repo
try:
    from web.branding import LOGO_URI, ICON_URI
except Exception:
    LOGO_URI = ICON_URI = ""

_CSS = """:root{--bg:#fff;--surface:#f9fafb;--surface2:#f2f4f6;--card:#fff;--border:#e5e8eb;--ink:#191f28;--ink2:#4e5968;--muted:#8b95a1;--accent:#3182f6;--accw:#eaf2fe;--acci:#1b64da;--good:#12b886;--danger:#f04452;--font:'Pretendard',-apple-system,BlinkMacSystemFont,'Malgun Gothic',system-ui,sans-serif}
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

def _page(title, body):
    fav = f"<link rel=icon href='{ICON_URI}'>" if ICON_URI else ""
    logo = f"<img src='{LOGO_URI}' alt='Medical Pipeline' style='height:34px'>" if LOGO_URI else "<b style='font-size:17px'>Medical Pipeline</b>"
    nav = (f"<div style='display:flex;align-items:center;gap:12px;padding:16px 0 12px'>{logo}"
           f"<a class='btn g' href='/' style='margin-left:auto;padding:8px 14px;font-size:13px'>← 대시보드</a></div>")
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{escape(title)}</title>{fav}<style>{_CSS}</style><div class=wrap>{nav}{body}</div>")


def _u(path):
    """마운트 프리픽스(script_root) 인식 절대경로. 단독 실행 시 ''→경로 그대로,
    DispatcherMiddleware로 /studio 등에 마운트되면 프리픽스를 자동 부착(하드코딩 링크가 프리픽스를 우회하지 않도록)."""
    return (request.script_root or "") + path


def _csrf_field():
    return f'<input type=hidden name=_csrf value="{session.get("_csrf", "")}">'


# (권한 게이트는 services/permissions로 이전됨 — 라우트는 service만 호출. _require_role 제거)


_SUPPORT_KO = {"direct": "직접근거", "partial": "부분근거", "inferred": "추론", "unsupported": "근거없음", "unverified": "미검증"}
_KIND_KO = {"automated": "자동검증", "human_review": "원장검수", "override": "원장확정", "migration": "이관"}

def _review_buttons(slug, claim_id, is_current, version_id, script_id):
    """원장 검수/반려 버튼(사람 판정=human_review, 자동보다 우선). 현재 버전에서만 노출.
    version_id·script_id를 명시 전송 → service가 current 재검사·소속 검증(지연 저장 차단)."""
    if not is_current:
        return ""
    act = _u(f"/ui/h/{slug}/claims/{claim_id}/review")
    hidden = (f'{_csrf_field()}<input type=hidden name=version_id value="{version_id}">'
              f'<input type=hidden name=script_id value="{script_id}">')
    return (f'<div style="margin-top:6px;display:flex;gap:6px">'
            f'<form method=post action="{act}" style="margin:0">{hidden}<input type=hidden name=decision value=confirm>'
            f'<button class=btn style="padding:4px 12px;font-size:13px;background:#12b886">확정</button></form>'
            f'<form method=post action="{act}" style="margin:0">{hidden}<input type=hidden name=decision value=reject>'
            f'<button class=btn style="padding:4px 12px;font-size:13px;background:#f04452">반려</button></form></div>')

def _evidence_panel(slug, claims, is_current, version_id, script_id):
    """4단계: 버전의 의학주장별 유효 근거판정 + 원문 인용 + 원장 검수/반려.
    검증됨=초록, 반려/실패=빨강, 판정없음=회색(미검증). 자동판정은 원문 근거에만, 최종은 원장."""
    if not claims:
        return ('<div class=card><h2>근거 검증 (4단계)</h2>'
                '<p><small>이 버전에 등록된 의학주장이 없습니다.</small></p></div>')
    verified = sum(1 for c in claims if c["verification_status"] == "verified")
    failed = sum(1 for c in claims if c["verification_status"] == "failed")
    unver = len(claims) - verified - failed
    rows = []
    for c in claims:
        vs = c["verification_status"]
        if vs == "verified":
            style, label = "background:#e6f7f0;color:#12b886", "검증됨"
        elif vs == "failed":
            style, label = "background:#fdeaec;color:#f04452", "반려/실패"
        else:
            style, label = "background:#f2f4f6;color:#8b95a1", "미검증"
        sup = f'<span class="badge" style="background:#eef4ff;color:#3182f6">{_SUPPORT_KO.get(c["support_level"], "미검증")}</span>' if c["support_level"] else ""
        kind = _KIND_KO.get(c["assessment_kind"], "")
        src = f'<div style="font-size:12px;color:#8b95a1;margin-top:4px">📄 {escape(c["source_title"])}</div>' if c["source_title"] else ""
        quote = (f'<div style="font-size:12px;color:#495057;margin-top:4px;padding:8px 10px;background:#f8f9fa;border-radius:8px;border-left:3px solid #d0d5dd">“{escape((c["source_quote"] or "")[:280])}”</div>'
                 if c["source_quote"] and c["support_level"] else "")
        rat = f'<div style="font-size:12px;color:#8b95a1;margin-top:2px">{escape((c["rationale"] or "")[:200])}</div>' if c["rationale"] else ""
        rows.append(
            f'<div class=blk><div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
            f'<span class="badge" style="{style}">{label}</span>{sup}'
            f'{f"<small>{escape(kind)}</small>" if kind else ""}</div>'
            f'<div style="margin-top:6px;font-size:14px">{escape((c["claim_text"] or "")[:220])}</div>'
            f'{src}{quote}{rat}{_review_buttons(slug, c["id"], is_current, version_id, script_id)}</div>')
    note = ('<p><small>자동검증은 <b>논문 원문을 실제로 대조</b>해 판정합니다(근거 문장 인용). '
            '의학적 근거등급·환자적용의 최종 판단은 원장 몫이며, <b>원장 확정/반려가 자동판정보다 우선</b>합니다.</small></p>')
    summary = (f'검증됨 <b style="color:#12b886">{verified}</b> · '
               f'미검증 <b style="color:#8b95a1">{unver}</b> · 반려/실패 <b style="color:#f04452">{failed}</b> (총 {len(claims)})')
    return (f'<div class=card><h2>근거 검증 (4단계)</h2><p>{summary}</p>{note}{"".join(rows)}</div>')


_LIGHTBOX = """<div class=lb id=lb onclick="if(event.target.id==='lb')this.style.display='none'"><img id=lbimg></div>
<script>
var TH=[].slice.call(document.querySelectorAll('.thumb')),ci=0;
function LB(el){ci=TH.indexOf(el);_sh()}
function _sh(){document.getElementById('lbimg').src=TH[ci].src;document.getElementById('lb').style.display='flex'}
document.addEventListener('keydown',function(e){var lb=document.getElementById('lb');if(!lb||lb.style.display!=='flex')return;
 if(e.key==='ArrowRight'){ci=(ci+1)%TH.length;_sh()}else if(e.key==='ArrowLeft'){ci=(ci-1+TH.length)%TH.length;_sh()}else if(e.key==='Escape')lb.style.display='none';});
</script>"""

def _images_panel(slug, version_id, blocks, img_keys, is_current, img_status=None):
    """장면별 AI 이미지 썸네일(클릭=라이트박스) + 피드백 재생성 폼. 편집폼과 분리(폼 중첩 방지)."""
    if not img_keys:
        return ""
    cells = []
    for b in blocks:
        key = b["stable_block_key"]
        if key not in img_keys:
            continue
        regen = (f'<form method=post action="{_u(f"/ui/h/{slug}/versions/{version_id}/blocks/{key}/regen-image")}" '
                 f'style="display:flex;gap:6px;margin-top:6px">{_csrf_field()}'
                 f'<input name=feedback placeholder="어떻게 바꿀까? (비우면 새 버전으로 재생성)" '
                 f'style="flex:1;font-size:12px;padding:7px;margin:0"><button class="btn g" '
                 f'style="padding:7px 12px;font-size:12px" onclick="this.innerHTML=\'생성중…\'">🎨 다시</button></form>') if is_current else ""
        stx = (img_status or {}).get(key) or {}
        badge = ('<span class="badge stale" style="font-size:11px">⚠ 대본 변경됨 — 재생성 권장</span>' if stx.get("stale") and stx.get("reason") == "source_scene_changed"
                 else '<span class="badge stale" style="font-size:11px">⚠ 출처 미결착(수동 확인)</span>' if stx.get("stale")
                 else "")
        cells.append(
            f'<div class=blk id="img_{escape(key)}"><div class=key>{escape(key)} · {escape((b["block_type"] or "")[:20])} {badge}</div>'
            f'<img class=thumb src="{_u(f"/img/h/{slug}/{key}")}" alt="scene" onclick="LB(this)">{regen}</div>')
    note = ('<p><small>영상용 <b>개념 B롤</b>(AI 생성)입니다. 실제 환자사진·논문 그림이 아니며, '
            '사용 전 저작권·의학표현은 원장 확인. 마음에 안 들면 아래에 적고 “다시”.</small></p>')
    return f'<div class=card><h2>장면 이미지 — 클릭하면 크게, ←→ 넘김</h2>{note}{"".join(cells)}</div>' + _LIGHTBOX


def _sqlstate(exc):
    for o in (getattr(exc, "orig", None), exc):
        try:
            return o.args[0].get("C")
        except Exception:
            pass
    return None


def create_app(engine=None):
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-" + uuid.uuid4().hex)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=bool(os.environ.get("SECRET_KEY")))  # 대시보드와 동일(세션 공유 안전)
    app.config["ENGINE"] = engine or make_engine()  # app_rw 엔진(운영은 DATABASE_URL)

    @app.before_request
    def _rid():
        g.request_id = uuid.uuid4().hex
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_urlsafe(24)   # 대시보드와 공유 세션이면 이미 있음
        # 상태변경 CSRF 검증: /ui/ 폼만(브라우저). /api/(JSON)·로그인 POST는 면제(API는 추후 토큰인증).
        if request.method == "POST" and not request.path.startswith("/api/") and request.path != "/login":
            sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
            good = session.get("_csrf") or ""
            if not (sent and good and hmac.compare_digest(str(sent), str(good))):
                abort(400)

    @contextmanager
    def tenant(slug):
        """slug→hospital_id, 세션 user_id→membership_id(서버 결정) 후 tenant_conn."""
        uid = session.get("user_id")
        if not uid:
            abort(401)
        eng = app.config["ENGINE"]
        # hospital_id (hospitals는 RLS 밖, app_rw SELECT 허용)
        with eng.connect() as c0:
            hid = c0.execute(text("select id from hospitals where slug=:s"), {"s": slug}).scalar()
        if not hid:
            abort(404)
        # membership 결정: hospital 컨텍스트에서 (user_id, hospital, active)로 조회 — 요청값 신뢰 안 함
        with repo.tenant_conn(eng, hid) as c1:
            mid = c1.execute(text("select id from hospital_memberships "
                                  "where hospital_id=:h and user_id=:u and archived_at is null"),
                             {"h": hid, "u": uid}).scalar()
        if not mid:
            abort(403)
        with repo.tenant_conn(eng, hid, membership_id=mid, request_id=g.request_id) as conn:
            yield conn, hid, mid

    def _map_pg(exc):
        code = _sqlstate(exc)
        return {"42501": 403, "23514": 422, "P0002": 404, "P2013": 409, "P2014": 409}.get(code, 400), code

    @app.errorhandler(403)
    def _stale_session(e):
        # UI 화면에서의 403은 대개 재시드로 세션 user_id가 무효가 된 경우 → 세션 비우고 로그인으로
        if request.method == "GET" and ("/ui/" in request.path or request.path == "/"):
            session.clear()
            return redirect(_u("/login"))
        return e

    # ── 편집 → 새 버전 ──
    @app.post("/api/h/<slug>/scripts/<script_id>/edit")
    def edit(slug, script_id):
        body = request.get_json(force=True) or {}
        expected = body.get("expected_current_version")
        edits = body.get("edits") or {}
        if not expected or not edits:
            return jsonify(error="expected_current_version과 edits 필요"), 400
        try:      # 업무 규칙은 공통 scripts service (라우트는 파싱+매핑만)
            ctx = ActorContext.resolve(app.config["ENGINE"], session.get("user_id"), slug, g.request_id)
            res = scripts_service.edit_blocks(app.config["ENGINE"], ctx, script_id, expected, edits)
            return jsonify(res), (200 if res.get("no_change") else 201)
        except ServiceError as e:
            return jsonify(error=e.code, detail=str(e)), e.http_status

    # ── 승인(공통 approval service) ──
    @app.post("/api/h/<slug>/versions/<version_id>/approve")
    def approve(slug, version_id):
        policy = (request.get_json(force=True) or {}).get("policy", "policy-1")
        try:      # 작성자≠승인자·current·evidence gate는 service/DB가 강제
            ctx = ActorContext.resolve(app.config["ENGINE"], session.get("user_id"), slug, g.request_id)
            out = approvals_service.approve(app.config["ENGINE"], ctx, version_id, policy=policy)
            return jsonify(ok=True, **out), 200
        except ServiceError as e:
            return jsonify(error=e.code, detail=str(e)), e.http_status

    # ── 버전 조회(+stale) ──
    @app.get("/api/h/<slug>/versions/<version_id>")
    def get_version(slug, version_id):
        policy = request.args.get("policy", "policy-1")
        with tenant(slug) as (conn, hid, mid):
            blocks = conn.execute(text(
                "select stable_block_key, order_index, block_type, scene, text "
                "from script_blocks where hospital_id=:h and version_id=:v order by order_index"),
                {"h": hid, "v": uuid.UUID(version_id)}).mappings().all()
            stale = repo.is_stale(conn, hid, uuid.UUID(version_id), policy)
        return jsonify(version_id=version_id, stale=stale, blocks=[dict(b) for b in blocks])

    # ── 블록 단위 diff ──
    @app.get("/api/h/<slug>/versions/<version_id>/diff")
    def diff(slug, version_id):
        frm = request.args.get("from")
        if not frm:
            return jsonify(error="from(비교 버전) 필요"), 400
        with tenant(slug) as (conn, hid, mid):
            def blocks(v):
                return {r.stable_block_key: r.text for r in conn.execute(text(
                    "select stable_block_key, text from script_blocks where hospital_id=:h and version_id=:v"),
                    {"h": hid, "v": v})}
            a = blocks(uuid.UUID(frm)); b = blocks(uuid.UUID(version_id))
        changed = [{"key": k, "before": a[k], "after": b[k]} for k in a.keys() & b.keys() if a[k] != b[k]]
        added = [{"key": k, "after": b[k]} for k in b.keys() - a.keys()]
        removed = [{"key": k, "before": a[k]} for k in a.keys() - b.keys()]
        return jsonify(changed=changed, added=added, removed=removed)

    # ══ 최소 UI (로그인·버전편집·승인) ══
    @app.route("/login", methods=["GET", "POST"])
    def login():
        err = ""
        if request.method == "POST":
            email = request.form.get("email", "").strip(); pw = request.form.get("password", "")
            eng = app.config["ENGINE"]
            with eng.connect() as cn:
                row = cn.execute(text("select id, pw_hash from lookup_user_for_login(:e)"), {"e": email}).first()
            if row and row.pw_hash and check_password_hash(row.pw_hash, pw):
                session.clear()   # 세션 고정 방지
                session["user_id"] = str(row.id)
                return redirect(request.args.get("next") or _u("/"))
            err = '<div class="msg e">이메일 또는 비밀번호가 올바르지 않습니다.</div>'
        return _page("로그인", f"<div class=card><h1>로그인</h1>{err}<form method=post>"
                     f"<label>이메일</label><input name=email type=email required>"
                     f"<label>비밀번호</label><input name=password type=password required>"
                     f"<button class=btn type=submit>로그인</button></form></div>")

    @app.get("/logout")
    def logout():
        session.clear(); return redirect(_u("/login"))

    @app.get("/")
    def home():
        if not session.get("user_id"): return redirect(_u("/login"))
        return _page("홈", "<div class=card><h1>대본 편집</h1><p>버전 URL로 접근하세요: "
                     "<code>/ui/h/&lt;slug&gt;/versions/&lt;version_id&gt;</code></p>"
                     f'<a class="btn g" href="{_u("/logout")}">로그아웃</a></div>')

    @app.get("/ui/h/<slug>/versions/<version_id>")
    def ui_version(slug, version_id):
        if not session.get("user_id"):
            return redirect(_u("/login") + "?next=" + _u(f"/ui/h/{slug}/versions/{version_id}"))
        msg = {"approved": '<div class="msg s">승인되었습니다.</div>',
               "e403": '<div class="msg e">승인 권한(approver)이 없습니다.</div>',
               "e422": '<div class="msg e">미검증/미지원 claim이 있어 승인할 수 없습니다(4단계 근거검증 필요).</div>',
               "edited": '<div class="msg s">새 버전이 생성되었습니다(미승인).</div>',
               "reviewed": '<div class="msg s">원장 검수가 반영되었습니다(자동판정보다 우선).</div>',
               "conflict": '<div class="msg e">현재 버전이 바뀌었거나 승인된 버전이라 반영하지 못했습니다.</div>',
               "rejected": '<div class="msg s">반려되었습니다.</div>',
               "revoked": '<div class="msg s">승인이 철회되었습니다.</div>',
               "regen": '<div class="msg s">이미지를 다시 생성했습니다.</div>',
               "regenfail": '<div class="msg e">이미지 재생성 실패(OpenAI 키/네트워크 확인).</div>'}.get(request.args.get("m"), "")
        try:      # 읽기 데이터는 공통 query service(get_version_workspace) 한 번으로
            _ctx = ActorContext.resolve(app.config["ENGINE"], session.get("user_id"), slug, g.request_id)
            ws = workspace_service.get_version_workspace(app.config["ENGINE"], _ctx, version_id)
        except ServiceError as e:
            if e.http_status == 401:
                return redirect(_u("/login") + "?next=" + _u(f"/ui/h/{slug}/versions/{version_id}"))
            abort(e.http_status)
        script_id = ws["script_id"]; blocks = ws["blocks"]; claims = ws["claims"]
        is_current = ws["is_current"]; appr_status = ws["approval_status"]; stale = ws["stale"]
        badge = '<span class="badge stale">미승인/stale</span>' if stale else '<span class="badge ok">승인됨</span>'
        rows = "".join(f'<div class=blk><div class=key>{escape(b["stable_block_key"])} · {escape(b["block_type"])}</div>'
                       f'<textarea name="edit__{escape(b["stable_block_key"])}">{escape(b["text"])}</textarea></div>' for b in blocks)
        editform = (f'<form method=post action="{_u(f"/ui/h/{slug}/scripts/{script_id}/edit")}">{_csrf_field()}'
                    f'<input type=hidden name=expected value="{version_id}">{rows}'
                    f'<button class=btn type=submit>💾 편집 저장(새 버전 생성)</button></form>') if is_current else \
                   f'<p><small>이 버전은 현재 버전이 아니라 편집할 수 없습니다(불변).</small></p>{rows}'
        _rj = _u(f"/ui/h/{slug}/versions/{version_id}"); act = ws["available_actions"]
        approve = reject = revoke = export = ""
        if act["can_approve"]:
            approve = (f'<form method=post action="{_rj}/approve" style="display:inline-block;margin-top:12px">{_csrf_field()}'
                       f'<button class=btn type=submit>✅ 승인</button></form>')
            reject = (f'<form method=post action="{_rj}/reject" style="display:inline-block;margin-top:12px;margin-left:6px">{_csrf_field()}'
                      f'<input name=reason placeholder="반려 사유" style="padding:6px 8px;font-size:13px">'
                      f'<button class=btn type=submit style="background:#f04452">반려</button></form>')
        if act["can_revoke"]:
            export = f'<a class="btn g" style="margin-left:6px" href="{_u(f"/api/h/{slug}/scripts/{script_id}/versions/{version_id}/export")}">⬇ export(JSON)</a>'
            revoke = (f'<form method=post action="{_rj}/revoke" style="display:inline-block;margin-top:12px;margin-left:6px">{_csrf_field()}'
                      f'<input name=reason placeholder="철회 사유" style="padding:6px 8px;font-size:13px">'
                      f'<button class=btn type=submit style="background:#f04452">승인 철회</button></form>')
        diff = f'<a class="btn g" href="{_u(f"/api/h/{slug}/versions/{version_id}/diff")}?from={ws["parent_version_id"]}">diff(JSON)</a>' if ws["parent_version_id"] else ""
        evidence = _evidence_panel(slug, claims, is_current, version_id, script_id)
        images = _images_panel(slug, version_id, blocks, ws["img_keys"], is_current, ws["images_status"])
        return _page(f"버전 {ws['version_no']}",
                     f'<div class=card><h1>버전 v{ws["version_no"]} {badge}</h1>{msg}'
                     f'<h2>블록 (편집 → 새 immutable 버전)</h2>{editform}{approve}{reject}{revoke}{export} {diff} '
                     f'<a class="btn g" href="{_u("/logout")}">로그아웃</a></div>{images}{evidence}')

    @app.post("/ui/h/<slug>/scripts/<script_id>/edit")
    def ui_edit(slug, script_id):
        expected = request.form.get("expected")
        try:
            uuid.UUID(expected); uuid.UUID(script_id)      # 폼 누락/오형식 → 400(500 방지)
        except (TypeError, ValueError):
            abort(400)
        edits = {k[6:]: v for k, v in request.form.items() if k.startswith("edit__")}
        try:      # 편집 규칙(권한·변경필터·새버전)은 공통 scripts service
            ctx = ActorContext.resolve(app.config["ENGINE"], session.get("user_id"), slug, g.request_id)
            res = scripts_service.edit_blocks(app.config["ENGINE"], ctx, script_id, expected, edits)
            if res.get("no_change"):
                return redirect(_u(f"/ui/h/{slug}/versions/{expected}"))
            return redirect(_u(f"/ui/h/{slug}/versions/{res['version_id']}?m=edited"))
        except ServiceError as e:
            if e.http_status == 409:
                return redirect(_u(f"/ui/h/{slug}/versions/{expected}?m=conflict"))
            abort(e.http_status)

    def _approval_action(slug, version_id, action, ok_msg):
        """승인/반려/철회 공통 — ctx resolve + service 호출 + 결과 메시지 리다이렉트(라우트=파싱+매핑)."""
        try:
            uuid.UUID(version_id)
        except (TypeError, ValueError):
            abort(400)
        try:
            ctx = ActorContext.resolve(app.config["ENGINE"], session.get("user_id"), slug, g.request_id)
            action(ctx)
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m={ok_msg}"))
        except ServiceError as e:
            m = {401: None, 403: "e403", 422: "e422", 409: "conflict"}.get(e.http_status, None)
            if m is None:
                if e.http_status == 401:
                    return redirect(_u("/login"))
                abort(e.http_status)
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m={m}"))

    @app.post("/ui/h/<slug>/versions/<version_id>/approve")
    def ui_approve(slug, version_id):
        return _approval_action(slug, version_id,
                                lambda ctx: approvals_service.approve(app.config["ENGINE"], ctx, version_id), "approved")

    @app.post("/ui/h/<slug>/versions/<version_id>/reject")
    def ui_reject(slug, version_id):
        reason = (request.form.get("reason") or "").strip()
        return _approval_action(slug, version_id,
                                lambda ctx: approvals_service.reject(app.config["ENGINE"], ctx, version_id, reason), "rejected")

    @app.post("/ui/h/<slug>/versions/<version_id>/revoke")
    def ui_revoke(slug, version_id):
        reason = (request.form.get("reason") or "").strip()
        return _approval_action(slug, version_id,
                                lambda ctx: approvals_service.revoke(app.config["ENGINE"], ctx, version_id, reason), "revoked")

    @app.post("/ui/h/<slug>/versions/<version_id>/self-approve")
    def ui_self_approve(slug, version_id):
        reason = (request.form.get("reason") or "").strip()
        return _approval_action(slug, version_id,
                                lambda ctx: approvals_service.self_approve(app.config["ENGINE"], ctx, version_id, reason), "approved")

    # ── export gate: current이며 approved인 version만 산출물 반환(inv14) ──
    @app.get("/api/h/<slug>/scripts/<script_id>/versions/<version_id>/export")
    def export_version(slug, script_id, version_id):
        try:
            ctx = ActorContext.resolve(app.config["ENGINE"], session.get("user_id"), slug, g.request_id)
            payload = exports_service.prepare_export(app.config["ENGINE"], ctx, script_id, version_id)
            return jsonify(payload), 200
        except ServiceError as e:
            return jsonify(error=e.code, detail=str(e)), e.http_status

    # ── 원장 검수/반려: 주장별 사람 판정(human_review) — 자동판정보다 우선 ──
    @app.post("/ui/h/<slug>/claims/<claim_id>/review")
    def ui_review(slug, claim_id):
        version_id = request.form.get("version_id"); script_id = request.form.get("script_id")
        decision = request.form.get("decision")
        try:
            uuid.UUID(claim_id); uuid.UUID(version_id); uuid.UUID(script_id)   # 폼 누락/오형식 → 400
        except (TypeError, ValueError):
            abort(400)
        try:      # 검수 규칙(권한·current 재검사·approved 동결·소속검증)은 공통 evidence service
            ctx = ActorContext.resolve(app.config["ENGINE"], session.get("user_id"), slug, g.request_id)
            evidence_service.assess_claim(app.config["ENGINE"], ctx, script_id, version_id, claim_id, decision)
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m=reviewed"))
        except ServiceError as e:
            if e.http_status == 409:   # current 변경/승인 동결 → 최신 버전으로
                return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m=conflict"))
            abort(e.http_status)

    # ── 장면 이미지 서빙(DB bytea) ──
    @app.get("/img/h/<slug>/<block_key>")
    def scene_img(slug, block_key):
        with tenant(slug) as (conn, hid, mid):
            row = conn.execute(text("select mime, data from scene_images "
                                    "where hospital_id=:h and block_key=:k limit 1"),
                               {"h": hid, "k": block_key}).first()
        if not row:
            abort(404)
        return Response(bytes(row.data), mimetype=row.mime or "image/jpeg",
                        headers={"Cache-Control": "no-store"})

    # ── 피드백 반영 이미지 재생성(사람 피드백 → 프롬프트 조정 → gpt-image-1) ──
    @app.post("/ui/h/<slug>/versions/<version_id>/blocks/<block_key>/regen-image")
    def regen_image(slug, version_id, block_key):
        feedback = (request.form.get("feedback") or "").strip()
        try:      # 이미지 재생성 규칙(권한·프롬프트·영속)은 공통 image service. OpenAI는 TX 밖.
            ctx = ActorContext.resolve(app.config["ENGINE"], session.get("user_id"), slug, g.request_id)
            images_service.regenerate_scene(app.config["ENGINE"], ctx, block_key, feedback, version_id=version_id)
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m=regen#{block_key}"))
        except ServiceError as e:
            if e.http_status == 401:
                return redirect(_u("/login"))
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m=regenfail"))
        except Exception:
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m=regenfail"))

    return app
