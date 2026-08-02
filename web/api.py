"""P0 3단계 앱층 — 편집·승인·diff·버전조회 HTTP API (store/ 기반, 실 PostgreSQL).

SDR 준수:
 - app.hospital_id/app.membership_id는 '서버가 인증 세션에서 결정'해서만 설정(요청 body의 membership 신뢰 금지).
 - request_id를 매 요청 생성해 승인 audit에 배선.
 - 승인은 repositories.approve_version 경로로만(advisory lock 하 hash).
CAS 충돌→409, 역할/권한(42501)→403, 미검증 claim(23514)→422, 없음(P0002)→404.
"""
import os, uuid
from contextlib import contextmanager
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


def _require_role(conn, hid, mid, allowed):
    """병원 내부 역할 게이트(P1). membership의 role이 allowed에 없으면 403.
    RLS는 병원 간 격리만 하므로 병원 내부 행동 제한은 여기서."""
    roles = {r[0] for r in conn.execute(text(
        "select role from membership_roles where hospital_id=:h and membership_id=:m"),
        {"h": hid, "m": mid})}
    if not (roles & set(allowed)):
        abort(403)


_SUPPORT_KO = {"direct": "직접근거", "partial": "부분근거", "inferred": "추론", "unsupported": "근거없음", "unverified": "미검증"}
_KIND_KO = {"automated": "자동검증", "human_review": "원장검수", "override": "원장확정", "migration": "이관"}

def _review_buttons(slug, claim_id, is_current):
    """원장 검수/반려 버튼(사람 판정=human_review, 자동보다 우선). 현재 버전에서만 노출."""
    if not is_current:
        return ""
    act = _u(f"/ui/h/{slug}/claims/{claim_id}/review")
    return (f'<div style="margin-top:6px;display:flex;gap:6px">'
            f'<form method=post action="{act}" style="margin:0">{_csrf_field()}<input type=hidden name=decision value=confirm>'
            f'<button class=btn style="padding:4px 12px;font-size:13px;background:#12b886">확정</button></form>'
            f'<form method=post action="{act}" style="margin:0">{_csrf_field()}<input type=hidden name=decision value=reject>'
            f'<button class=btn style="padding:4px 12px;font-size:13px;background:#f04452">반려</button></form></div>')

def _evidence_panel(slug, claims, is_current):
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
            f'{src}{quote}{rat}{_review_buttons(slug, c["id"], is_current)}</div>')
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

def _images_panel(slug, version_id, blocks, img_keys, is_current):
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
        cells.append(
            f'<div class=blk id="img_{escape(key)}"><div class=key>{escape(key)} · {escape((b["block_type"] or "")[:20])}</div>'
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
            session["_csrf"] = uuid.uuid4().hex + uuid.uuid4().hex   # 대시보드와 공유 세션이면 이미 있음
        # 상태변경 CSRF 검증: /ui/ 폼만(브라우저). /api/(JSON)·로그인 POST는 면제(API는 추후 토큰인증).
        if request.method == "POST" and not request.path.startswith("/api/") and request.path != "/login":
            if request.form.get("_csrf") != session.get("_csrf"):
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
        return {"42501": 403, "23514": 422, "P0002": 404, "2BP01": 409}.get(code, 400), code

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
        try:
            with tenant(slug) as (conn, hid, mid):
                res = repo.apply_block_edit(conn, hid, uuid.UUID(script_id), uuid.UUID(expected), edits)
            res["version_id"] = str(res["version_id"])
            res["compliance"] = {k: [f[0] if isinstance(f, (list, tuple)) else str(f) for f in v]
                                 for k, v in res["compliance"].items()}
            return jsonify(res), 201
        except repo.Conflict as e:
            return jsonify(error="conflict", detail=str(e)), 409       # 다른 편집 선반영

    # ── 승인 ──
    @app.post("/api/h/<slug>/versions/<version_id>/approve")
    def approve(slug, version_id):
        policy = (request.get_json(force=True) or {}).get("policy", "policy-1")
        try:
            with tenant(slug) as (conn, hid, mid):
                out = repo.approve_version(conn, hid, uuid.UUID(version_id), policy)
            return jsonify(ok=True, **out), 200
        except Exception as e:
            status, code = _map_pg(e)
            if status == 400 and code is None:
                raise
            return jsonify(error="approve_failed", sqlstate=code), status

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
               "regen": '<div class="msg s">이미지를 다시 생성했습니다.</div>',
               "regenfail": '<div class="msg e">이미지 재생성 실패(OpenAI 키/네트워크 확인).</div>'}.get(request.args.get("m"), "")
        with tenant(slug) as (conn, hid, mid):
            sc = conn.execute(text("select script_id, version_no, parent_version_id from script_versions where hospital_id=:h and id=:v"),
                              {"h": hid, "v": uuid.UUID(version_id)}).first()
            if not sc: abort(404)
            blocks = conn.execute(text("select stable_block_key, order_index, block_type, text from script_blocks "
                                       "where hospital_id=:h and version_id=:v order by order_index"),
                                  {"h": hid, "v": uuid.UUID(version_id)}).mappings().all()
            stale = repo.is_stale(conn, hid, uuid.UUID(version_id), "policy-1")
            is_current = conn.execute(text("select current_version_id=:v from scripts where id=:s"),
                                      {"v": uuid.UUID(version_id), "s": sc.script_id}).scalar()
            # 4단계: 이 버전의 의학주장 + 유효 근거판정(사람>자동, migration 제외) + 출처
            claims = conn.execute(text(
                "select c.id, c.claim_text, e.support_level, e.verification_status, e.medical_risk, "
                "e.assessment_kind, e.rationale, "
                "(select s.title from claim_sources cs join source_versions sv "
                "  on sv.hospital_id=cs.hospital_id and sv.id=cs.source_version_id "
                "  join sources s on s.hospital_id=sv.hospital_id and s.id=sv.source_id "
                "  where cs.hospital_id=c.hospital_id and cs.claim_id=c.id limit 1) as source_title, "
                "(select cs.source_quote from claim_sources cs "
                "  where cs.hospital_id=c.hospital_id and cs.claim_id=c.id limit 1) as source_quote "
                "from claims c left join claim_effective_assessment e "
                "  on e.hospital_id=c.hospital_id and e.claim_id=c.id "
                "where c.hospital_id=:h and c.version_id=:v order by c.claim_index"),
                {"h": hid, "v": uuid.UUID(version_id)}).mappings().all()
            has_img_tbl = conn.execute(text("select to_regclass('public.scene_images')")).scalar()
            img_keys = ({r[0] for r in conn.execute(text(
                "select block_key from scene_images where hospital_id=:h"), {"h": hid})}
                if has_img_tbl else set())   # scene_images 미설치 환경(테스트 등) 안전
        badge = '<span class="badge stale">미승인/stale</span>' if stale else '<span class="badge ok">승인됨</span>'
        rows = "".join(f'<div class=blk><div class=key>{escape(b["stable_block_key"])} · {escape(b["block_type"])}</div>'
                       f'<textarea name="edit__{escape(b["stable_block_key"])}">{escape(b["text"])}</textarea></div>' for b in blocks)
        editform = (f'<form method=post action="{_u(f"/ui/h/{slug}/scripts/{sc.script_id}/edit")}">{_csrf_field()}'
                    f'<input type=hidden name=expected value="{version_id}">{rows}'
                    f'<button class=btn type=submit>💾 편집 저장(새 버전 생성)</button></form>') if is_current else \
                   f'<p><small>이 버전은 현재 버전이 아니라 편집할 수 없습니다(불변).</small></p>{rows}'
        approve = (f'<form method=post action="{_u(f"/ui/h/{slug}/versions/{version_id}/approve")}" style="margin-top:12px">{_csrf_field()}'
                   f'<button class=btn type=submit>✅ 승인</button></form>') if (is_current and stale) else ""
        diff = f'<a class="btn g" href="{_u(f"/api/h/{slug}/versions/{version_id}/diff")}?from={sc.parent_version_id}">diff(JSON)</a>' if sc.parent_version_id else ""
        evidence = _evidence_panel(slug, claims, is_current)
        images = _images_panel(slug, version_id, blocks, img_keys, is_current)
        return _page(f"버전 {sc.version_no}",
                     f'<div class=card><h1>버전 v{sc.version_no} {badge}</h1>{msg}'
                     f'<h2>블록 (편집 → 새 immutable 버전)</h2>{editform}{approve} {diff} '
                     f'<a class="btn g" href="{_u("/logout")}">로그아웃</a></div>{images}{evidence}')

    @app.post("/ui/h/<slug>/scripts/<script_id>/edit")
    def ui_edit(slug, script_id):
        if not session.get("user_id"): abort(401)
        expected = request.form.get("expected")
        try:
            exp_uuid, sc_uuid = uuid.UUID(expected), uuid.UUID(script_id)   # 폼 누락/오형식 → 400(500 방지)
        except (TypeError, ValueError):
            abort(400)
        edits = {k[6:]: v for k, v in request.form.items() if k.startswith("edit__")}
        try:
            with tenant(slug) as (conn, hid, mid):
                _require_role(conn, hid, mid, {"editor", "approver", "admin"})   # 편집 권한
                # 원문과 다른 블록만 편집으로 간주(apply_block_edit이 변경분만 처리)
                cur = {r.stable_block_key: r.text for r in conn.execute(text(
                    "select stable_block_key, text from script_blocks where hospital_id=:h and version_id=:v"),
                    {"h": hid, "v": exp_uuid})}
                changed = {k: v for k, v in edits.items() if cur.get(k) != v}
                if not changed:
                    return redirect(_u(f"/ui/h/{slug}/versions/{expected}"))
                res = repo.apply_block_edit(conn, hid, sc_uuid, exp_uuid, changed)
            return redirect(_u(f"/ui/h/{slug}/versions/{res['version_id']}?m=edited"))
        except repo.Conflict:
            return redirect(_u(f"/ui/h/{slug}/versions/{expected}?m=conflict"))

    @app.post("/ui/h/<slug>/versions/<version_id>/approve")
    def ui_approve(slug, version_id):
        if not session.get("user_id"): abort(401)
        try:
            with tenant(slug) as (conn, hid, mid):
                repo.approve_version(conn, hid, uuid.UUID(version_id), "policy-1")
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m=approved"))
        except Exception as e:
            code = _sqlstate(e)
            m = {"42501": "e403", "23514": "e422"}.get(code)
            if not m: raise
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m={m}"))

    # ── 원장 검수/반려: 주장별 사람 판정(human_review) — 자동판정보다 우선 ──
    @app.post("/ui/h/<slug>/claims/<claim_id>/review")
    def ui_review(slug, claim_id):
        if not session.get("user_id"): abort(401)
        try:
            cl_uuid = uuid.UUID(claim_id)
        except (TypeError, ValueError):
            abort(400)
        decision = request.form.get("decision")
        if decision == "confirm":
            sup, vf, risk = "direct", "verified", "low"
        elif decision == "reject":
            sup, vf, risk = "unsupported", "failed", "high"
        else:
            abort(400)
        with tenant(slug) as (conn, hid, mid):
            _require_role(conn, hid, mid, {"approver", "admin"})   # 근거 검수는 원장(approver/admin)
            row = conn.execute(text("select version_id from claims where hospital_id=:h and id=:c"),
                               {"h": hid, "c": cl_uuid}).first()
            if not row: abort(404)
            # 사람 판정 append(불변; effective view가 최신 human을 automated보다 우선)
            conn.execute(text(
                "insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,"
                "support_level,verification_status,medical_risk,rationale,created_by_membership_id) "
                "values(:i,:h,:c,'human_review',:ik,:sup,:vf,:risk,:ra,:mid)"),
                {"i": uuid.uuid4(), "h": hid, "c": cl_uuid, "ik": uuid.uuid4().hex,
                 "sup": sup, "vf": vf, "risk": risk,
                 "ra": ("원장 확정" if decision == "confirm" else "원장 반려"), "mid": mid})
            vid = row.version_id
        return redirect(_u(f"/ui/h/{slug}/versions/{vid}?m=reviewed"))

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
        if not session.get("user_id"):
            abort(401)
        feedback = (request.form.get("feedback") or "").strip()
        try:
            from assets.gen_images import gen_image_bytes
            from store.seed_images import web_jpeg_bytes
            with tenant(slug) as (conn, hid, mid):
                _require_role(conn, hid, mid, {"editor", "approver", "admin"})   # 이미지 재생성 권한
                row = conn.execute(text("select prompt, topic from scene_images "
                                        "where hospital_id=:h and block_key=:k limit 1"),
                                   {"h": hid, "k": block_key}).first()
            base = (row.prompt if row else None) or f"clean medical educational illustration for scene {block_key}"
            prompt = base + (f" Reviewer adjustment: {feedback}." if feedback else " Provide a fresh alternative composition.")
            jpg = web_jpeg_bytes(gen_image_bytes(prompt))       # OpenAI 호출은 DB 트랜잭션 밖
            with tenant(slug) as (conn, hid, mid):
                conn.execute(text("update scene_images set data=:d, updated_at=now() "
                                  "where hospital_id=:h and block_key=:k"),
                             {"d": jpg, "h": hid, "k": block_key})
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m=regen#{block_key}"))
        except Exception:
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m=regenfail"))

    return app
