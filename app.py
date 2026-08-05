#!/usr/bin/env python
"""
boncure-pipeline 로컬 웹앱 — 터미널·yaml 없이 브라우저로 쓴다.
실행:  python app.py   → 브라우저에서 http://localhost:5000
기능: 병원 만들기(폼) · 자료 업로드(끌어놓기) · 대본 생성(버튼) · 대시보드 보기.
엔진(run.py)을 그대로 호출하므로 파이프라인 로직은 재사용.
"""
import os, sys, glob, subprocess, threading, re, io, secrets, sqlite3, datetime, unicodedata, hmac, time
from flask import Flask, request, redirect, send_file, abort, render_template_string, jsonify, url_for, session, g, Response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
try:
    from web.branding import LOGO_URI, ICON_URI     # medical.png 추출 로고(투명 PNG data URI)
except Exception:
    LOGO_URI = ICON_URI = ""
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024   # 업로드 최대 500MB

# ── 작업 상태(P1): 단일 원본 = PostgreSQL generation_jobs. 로그는 프로세스 메모리(실시간 뷰, 전이).
#   SQLite 신규쓰기 중단 — 상태가 두 곳(SQLite/PG)에 있던 문제 해소. 실행 가드도 PG 기반(재시작에도 유효).
_LOG = {}       # hospital(slug) → 실시간 스트리밍 로그(전이)
_MEMJOB = {}    # 비-PG 병원(전이 fallback) → {topic,status,ok}
_RUNNING_STATES = {"pending", "generating", "generated", "ingesting"}

def _pg_latest_job(h):
    hid = _pg_hospital_id(h)
    if not hid:
        return None
    try:
        from store.db import make_engine
        from sqlalchemy import text
        from store.repositories import tenant_conn
        with tenant_conn(make_engine(), hid) as cn:
            return cn.execute(text(
                "select status, phase, topic, version_id, error_message, "
                "extract(epoch from (now()-created_at)) as elapsed from generation_jobs "
                "where hospital_id=:h order by created_at desc limit 1"), {"h": hid}).mappings().first()
    except Exception:
        return None

def job_get(h):
    log = _LOG.get(h, "")
    j = _pg_latest_job(h)
    if j is not None:      # PG 병원 → generation_jobs가 상태의 단일 원본
        return {"hospital": h, "topic": j["topic"], "status": j["status"], "phase": j["phase"],
                "ok": (j["status"] == "completed"), "running": j["status"] in _RUNNING_STATES, "log": log,
                "elapsed": (int(j["elapsed"]) if j["elapsed"] is not None else 0),
                "version_id": (str(j["version_id"]) if j["version_id"] else None), "error": j["error_message"]}
    m = _MEMJOB.get(h, {})     # 비-PG 병원(전이)
    return {"hospital": h, "topic": m.get("topic", ""), "status": m.get("status", "idle"),
            "ok": m.get("ok"), "running": m.get("status") == "running", "log": log}

def job_set(h, topic=None, status=None, ok=None, log=None):
    """로그는 메모리. 상태의 단일 원본은 PG generation_jobs(PG 병원은 mark_job로 갱신).
    비-PG 병원만 메모리 상태 유지(전이). SQLite 미사용."""
    if log is not None:
        _LOG[h] = log
    if _pg_hospital_id(h) is None:
        m = _MEMJOB.setdefault(h, {})
        if topic is not None: m["topic"] = topic
        if status is not None: m["status"] = status
        if ok is not None: m["ok"] = ok

# ── 인증(세션) ──────────────────────────────────────────────
_sk = os.path.join(ROOT, ".secret")
# 배포(Render 등)에선 SECRET_KEY 환경변수 우선 — 재시작해도 세션 유지. 없으면 로컬 .secret 파일.
app.secret_key = (os.environ.get("SECRET_KEY")
                  or (open(_sk).read().strip() if os.path.exists(_sk)
                      else (lambda s: (open(_sk,"w").write(s), s)[1])(secrets.token_hex(32))))
# 쿠키 보안(스튜디오와 동일 설정으로 세션 공유 안전). 배포(SECRET_KEY 존재)면 Secure.
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("SECRET_KEY")))
def _users_path(): return os.path.join(ROOT, "config", "users.yaml")
def load_users():
    import yaml
    p = _users_path()
    if not os.path.exists(p):
        # 기본 비밀번호: ADMIN_PW 환경변수 우선, 없으면 매번 랜덤 생성(고정 기본값 금지)
        pw = os.environ.get("ADMIN_PW") or secrets.token_urlsafe(9)
        yaml.safe_dump({"admin": generate_password_hash(pw)}, open(p,"w",encoding="utf-8"))
        print(f"\n[초기 로그인 계정] 아이디: admin  비밀번호: {pw}"
              f"\n(config/users.yaml 에 해시로 저장됨. 바꾸려면 이 파일 지우고 ADMIN_PW 환경변수 지정 후 재시작)\n")
    return yaml.safe_load(open(p, encoding="utf-8")) or {}
def save_users(u):
    import yaml; yaml.safe_dump(u, open(_users_path(),"w",encoding="utf-8"), allow_unicode=True)

app.jinja_env.globals["csrf"] = lambda: session.get("_csrf", "")

@app.before_request
def _guard():
    g._t0 = time.perf_counter(); g.request_id = secrets.token_hex(16)   # 관측(latency·상관)
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(24)   # 세션당 CSRF 토큰
    if request.endpoint in ("login","static","bm_guide"): return   # 로그인·정적·공개 가이드는 면제
    if not session.get("user"): return redirect("/login")
    if request.method == "POST":   # 상태변경 요청 CSRF 검증(상수시간 비교 + JSON/fetch는 헤더 토큰)
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
        good = session.get("_csrf") or ""
        if not (sent and good and hmac.compare_digest(str(sent), str(good))):
            abort(400)


@app.after_request
def _obs_dashboard(resp):
    # 대시보드 http 관측 — 신규 canonical route(/scripts=dashboard_canonical) vs 기타 대시보드.
    # /studio(studio_legacy, compat)와 surface로 대비해 cutover·제거 판단(GPT).
    try:
        from services.observability import emit, mask_ids
        st = resp.status_code
        lat = round((time.perf_counter() - g._t0) * 1000, 1) if getattr(g, "_t0", None) is not None else None
        canonical = request.endpoint == "scripts_edit"
        loc = resp.headers.get("Location") if 300 <= st < 400 else None
        emit("http", app="dashboard",
             surface=("dashboard_canonical" if canonical else "dashboard"), compat=False,
             method=request.method,
             rule=(request.url_rule.rule if request.url_rule else mask_ids(request.path)),
             endpoint=request.endpoint, status=st,
             redirect=(300 <= st < 400) or None,
             redirect_target=(mask_ids(loc) if loc else None),
             request_id=getattr(g, "request_id", None), latency_ms=lat)
    except Exception:
        pass
    return resp

def _yaml():
    import yaml; return yaml
def hospitals():
    out = []
    RESERVED = {"users", "_template"}   # 병원이 아닌 설정 파일
    for p in glob.glob(os.path.join(ROOT, "config", "*.yaml")):
        n = os.path.splitext(os.path.basename(p))[0]
        if n.startswith("_") or n in RESERVED: continue
        try:
            cfg = _yaml().safe_load(open(p, encoding="utf-8")) or {}
            out.append({"id": n, "name": cfg.get("hospital", {}).get("name", n)})
        except Exception:
            out.append({"id": n, "name": n})
    return sorted(out, key=lambda x: x["id"])
def cfg_path(h): return os.path.join(ROOT, "config", f"{h}.yaml")
def data_dir(h, sub):
    d = os.path.join(ROOT, "data", h, sub); os.makedirs(d, exist_ok=True); return d
def safe_id(s): return re.sub(r"[^a-zA-Z0-9_-]", "-", (s or "").strip()) or "hospital"

CSS = """
:root{--bg:#fff;--surface:#f9fafb;--surface2:#f2f4f6;--card:#fff;--border:#e5e8eb;--ink:#191f28;--ink2:#4e5968;--muted:#8b95a1;--accent:#3182f6;--accw:#eaf2fe;--acci:#1b64da;--good:#12b886;--danger:#f04452;--font:'Pretendard',-apple-system,BlinkMacSystemFont,'Malgun Gothic',system-ui,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--font);line-height:1.6;letter-spacing:-.01em;-webkit-font-smoothing:antialiased}
a{color:var(--acci);text-decoration:none}
.nav{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.82);backdrop-filter:saturate(1.4) blur(12px);border-bottom:1px solid var(--border)}
.nav-in{max-width:900px;margin:0 auto;padding:15px 22px;display:flex;align-items:center;gap:10px}
.brand{font-weight:800;font-size:16px;letter-spacing:-.04em;display:flex;gap:9px;align-items:center}
.dot{width:24px;height:24px;border-radius:7px;background:var(--accent);display:grid;place-items:center;color:#fff;font-size:14px;font-weight:800}
.wrap{max-width:900px;margin:0 auto;padding:8px 22px 90px}
.hero{padding:48px 0 30px}
.hero h1{font-size:clamp(30px,5vw,46px);font-weight:800;letter-spacing:-.045em;line-height:1.15;margin:0}
.hero p{color:var(--ink2);font-size:clamp(15px,2vw,18px);font-weight:500;margin:16px 0 0;max-width:560px}
h1{font-size:26px;font-weight:800;letter-spacing:-.04em;margin:0 0 4px}
h2{font-size:17px;font-weight:800;letter-spacing:-.02em;margin:0 0 12px}
.sub{color:var(--muted);font-size:14px;margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:24px;box-shadow:0 1px 3px rgba(25,31,40,.03),0 10px 30px rgba(25,31,40,.04);margin-bottom:16px}
.hlist{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:26px}
@media(max-width:560px){.hlist{grid-template-columns:1fr}}
.hcard{display:block;background:var(--card);border:1px solid var(--border);border-radius:16px;padding:20px;transition:.15s}
.hcard:hover{border-color:var(--accent);transform:translateY(-1px);box-shadow:0 8px 24px rgba(49,130,246,.1)}
.hcard .n{font-weight:800;font-size:17px;color:var(--ink);letter-spacing:-.02em}
.hcard .i{font-size:12.5px;color:var(--muted);margin-top:3px}
.hcard.add{border-style:dashed;color:var(--acci);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:15px}
label{display:block;font-size:13px;font-weight:700;color:var(--ink2);margin:14px 0 6px}
input[type=text],input[type=password],textarea{width:100%;font-family:var(--font);font-size:15px;padding:12px 14px;border:1px solid var(--border);border-radius:12px;background:#fff;color:var(--ink)}
input[type=text]:focus,input[type=password]:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accw)}
textarea{min-height:64px;resize:vertical}
.btn{font-family:var(--font);font-weight:700;font-size:15px;padding:12px 20px;border-radius:12px;border:1px solid var(--border);background:#fff;color:var(--ink);cursor:pointer;text-decoration:none;display:inline-block;transition:.12s}
.btn:hover{background:var(--surface2)}
.btn.pri{background:var(--accent);color:#fff;border-color:transparent}.btn.pri:hover{background:#1b6fe0}
.btn.dz{padding:8px 14px;font-size:14px;border-radius:100px}
.btn:disabled{opacity:.5;cursor:not-allowed}
.drop{border:2px dashed var(--border);border-radius:16px;padding:30px;text-align:center;color:var(--muted);font-size:14px;background:var(--surface);cursor:pointer;transition:.15s}
.drop.over{border-color:var(--accent);background:var(--accw);color:var(--acci)}
.files{list-style:none;padding:0;margin:12px 0 0;font-size:13px;color:var(--ink2)}
.files li{padding:7px 0;border-top:1px solid var(--border)}
.chk{display:flex;flex-wrap:wrap;gap:8px}
.pill{font-size:12.5px;font-weight:700;padding:6px 12px;border-radius:100px;border:1px solid transparent}
.pill.ok{background:#e6f7f0;color:var(--good)}
.pill.no{background:#fdeaec;color:var(--danger)}
.pill.dim{background:var(--surface2);color:var(--muted)}
.out{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:13px 0;border-top:1px solid var(--border);font-size:14.5px;font-weight:600}
.log{white-space:pre-wrap;font-family:ui-monospace,Menlo,monospace;font-size:12.5px;background:#12151c;color:#cbd5e1;border-radius:12px;padding:15px;max-height:280px;overflow:auto;margin-top:14px}
.muted{color:var(--muted);font-size:12.5px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.note{font-size:12.5px;color:var(--ink2);background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 14px;margin-top:8px}
.navr{margin-left:auto;display:flex;gap:10px;align-items:center;font-size:13px;font-weight:600;color:var(--ink2)}
.btn.ghost{padding:7px 13px;font-size:13px;border-radius:9px}
.auth{min-height:calc(100vh - 56px);display:grid;place-items:center;padding:24px}
.authcard{width:100%;max-width:380px;background:var(--card);border:1px solid var(--border);border-radius:20px;padding:34px 30px;box-shadow:0 10px 40px rgba(25,31,40,.08)}
.authcard .logo{display:flex;justify-content:center;margin-bottom:16px}
.authcard h1{font-size:23px;text-align:center;margin:0 0 6px;letter-spacing:-.03em}
.authcard .s{text-align:center;color:var(--muted);font-size:14px;margin:0 0 22px}
.authcard .btn{width:100%;text-align:center;margin-top:20px}
.err{background:#fdeaec;color:var(--danger);font-size:13px;font-weight:600;padding:10px 13px;border-radius:10px;margin-bottom:14px;text-align:center}
.authfoot{text-align:center;font-size:13px;color:var(--muted);margin-top:16px}
"""

PAGE = """<!doctype html><html lang=ko><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{{title}}</title><style>{{css}}</style></head><body>
<div class=nav><div class=nav-in><div class=brand>{{ brand|safe }}</div>{{ userhtml|safe }}</div></div>
<div class=wrap>{{ body|safe }}</div>{{ script|safe }}</body></html>"""

def page(title, body, script=""):
    u = session.get("user")
    userhtml = f'<div class=navr><span>{u}</span><a class="btn ghost" href="/logout">로그아웃</a></div>' if u else ""
    brand = (f'<img src="{LOGO_URI}" style="height:30px" alt="Medical Pipeline">' if LOGO_URI
             else '<span class=dot>본</span>병원 유튜브 대본 생성기')
    return render_template_string(PAGE, title=title, css=CSS, body=body, script=script, userhtml=userhtml, brand=brand)

@app.route("/")
def home():
    hs = hospitals()
    cards = "".join(f'<a class=hcard href="/h/{h["id"]}"><div class=n>{h["name"]}</div><div class=i>{h["id"]}</div></a>' for h in hs)
    cards += '<a class="hcard add" href="#new">+ 새 병원 만들기</a>'
    _e = request.args.get("err")
    emsg = ""
    if _e == "exists":
        emsg = '<div class=note style="border-color:var(--danger);color:var(--danger)">⚠️ 같은 ID의 병원이 이미 있어요. 다른 이름을 써 주세요.</div>'
    elif _e == "taken":
        emsg = '<div class=note style="border-color:var(--danger);color:var(--danger)">⚠️ 이미 사용 중인 병원 이름이에요(다른 사용자 소유). 다른 이름을 써 주세요.</div>'
    _oplink = ('<div style="text-align:right;margin-bottom:8px"><a class="btn g" href="/admin/members">👥 팀·권한 관리</a></div>'
               if _is_platform_operator(session.get("user_id")) else "")
    body = f"""
    {_oplink}
    <div class=hero><h1>병원 유튜브를,<br>대본이 아니라 버튼으로.</h1>
      <p>자료를 올리고 버튼만 누르면 촬영용 대본 패키지가 나옵니다. 병원을 고르거나 새로 만드세요.</p></div>
    {emsg}
    <div class=hlist>{cards}</div>
    <div class=card id=new>
      <h2>+ 새 병원 만들기</h2>
      <form method=post action="/new"><input type=hidden name=_csrf value="{session.get('_csrf','')}">
        <label>병원명</label><input type=text name=name placeholder="예: 서울정형외과" required>
        <label>원장 이름 (기본 화자)</label><input type=text name=host placeholder="예: 김철수">
        <label>채널 슬로건 (대시보드 부제)</label><input type=text name=tagline placeholder="예: 무릎이 편해야 인생이 걷습니다">
        <label>주력 질환 (쉼표로 구분)</label><input type=text name=diseases placeholder="예: 오십견, 무릎관절염, 허리디스크">
        <div class=row style="margin-top:16px"><button class="btn pri" type=submit>만들기</button></div>
      </form>
    </div>"""
    return page("병원 선택", body)

def _esc(s):
    return (str(s if s is not None else "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def _is_platform_operator(uid):
    """세션 사용자가 활성 대행사 운영자(platform_access_grant)인지."""
    if not uid:
        return False
    try:
        from store.db import make_engine
        from sqlalchemy import text
        with make_engine().connect() as cn:
            return cn.execute(text("select 1 from platform_access_grants where user_id=:u and status='active' limit 1"),
                              {"u": uid}).first() is not None
    except Exception:
        return False

@app.route("/admin/members")
def admin_members():
    """팀·권한 관리(대행사 전용) — 이미 있는 계정에 병원 역할(editor/approver/admin) 부여·제거."""
    uid = session.get("user_id")
    if not _is_platform_operator(uid):
        return page("접근 불가", '<div class=card><h1>권한 없음</h1>'
                    '<p class=muted>이 페이지는 대행사 운영자 계정만 사용할 수 있어요.</p>'
                    '<a class=btn href="/">← 홈</a></div>'), 403
    from store.db import make_engine
    from store import member_admin as _ma
    eng = make_engine()
    sel = request.args.get("h", "")
    _csrf = session.get("_csrf", "")
    opts = "".join(f'<a class="btn dz{" pri" if h["id"]==sel else ""}" href="/admin/members?h={h["id"]}">{_esc(h["name"])}</a>'
                   for h in hospitals())
    body = ('<div class=row style="justify-content:space-between"><div><h1>팀 · 권한 관리</h1>'
            '<p class=sub>대행사 전용 — 계정에 병원 역할 주기</p></div><a href="/" class=btn>← 홈</a></div>')
    body += f'<div class=card><div class=note>병원을 고르면 그 병원의 멤버·역할을 관리해요.</div><div class=chk style="margin-top:10px">{opts or "<span class=muted>병원이 없어요.</span>"}</div></div>'
    if sel:
        hid = _pg_hospital_id(sel)
        if not hid:
            body += '<div class=card><p class=muted>이 병원은 PostgreSQL에 등록되지 않았어요.</p></div>'
        else:
            code = request.args.get("m"); msg = ""
            _m = {"added": ('good', '✅ 역할이 추가됐어요.'), "removed": ('', '역할을 제거했어요.'),
                  "nouser": ('danger', '⚠️ 그 이메일의 계정이 없어요. 계정이 먼저 있어야 역할을 줄 수 있어요.'),
                  "err": ('danger', '⚠️ 처리 실패 — 권한·입력을 확인하세요.')}
            if code in _m:
                col = f"color:var(--{_m[code][0]})" if _m[code][0] else ""
                msg = f'<div class=note style="{col}">{_m[code][1]}</div>'
            members = _ma.list_members(eng, hid, uid)
            ROLES = ["editor", "approver", "admin"]
            rows = ""
            for mb in members:
                rl = set(mb["roles"]); tg = ""
                for role in ROLES:
                    has = role in rl
                    act = "remove" if has else "add"
                    tg += (f'<form method=post action="/admin/members/set" style="display:inline-block;margin:2px">'
                           f'<input type=hidden name=_csrf value="{_csrf}"><input type=hidden name=h value="{_esc(sel)}">'
                           f'<input type=hidden name=user value="{_esc(mb["user_id"])}"><input type=hidden name=role value="{role}">'
                           f'<input type=hidden name=action value="{act}">'
                           f'<button class="btn {"pri" if has else "g"}" style="padding:4px 11px;font-size:12px">'
                           f'{"✓ " if has else "+ "}{role}</button></form>')
                rows += (f'<tr><td style="padding:9px;border-top:1px solid var(--border)">{_esc(mb["email"])}'
                         f'<br><span class=muted style="font-size:12px">{_esc(mb["name"])}</span></td>'
                         f'<td style="padding:9px;border-top:1px solid var(--border)">{tg}</td></tr>')
            if not members:
                rows = '<tr><td colspan=2 class=muted style="padding:12px">아직 멤버가 없어요. 아래에서 이메일로 추가하세요.</td></tr>'
            body += (f'<div class=card>{msg}<h2 style="margin-top:0">멤버 · 역할 <span class=muted style="font-size:13px">(역할 클릭해서 켜고 끄기)</span></h2>'
                     f'<table style="width:100%;border-collapse:collapse"><tr style="text-align:left"><th style="padding:9px">계정</th><th style="padding:9px">역할</th></tr>{rows}</table>'
                     f'<h3 style="margin-top:20px">+ 멤버 추가 (이메일)</h3>'
                     f'<form method=post action="/admin/members/set"><input type=hidden name=_csrf value="{_csrf}"><input type=hidden name=h value="{_esc(sel)}"><input type=hidden name=action value="addemail">'
                     f'<div class=row><input type=email name=email placeholder="추가할 계정 이메일" required style="flex:1;min-width:200px">'
                     f'<select name=role style="padding:8px"><option value=editor>editor(편집)</option><option value=approver>approver(승인)</option><option value=admin>admin(관리자)</option></select>'
                     f'<button class="btn pri" type=submit>추가</button></div>'
                     f'<div class=muted style="font-size:12px;margin-top:7px">이미 계정이 있는 사람만 추가돼요. 계정 생성·비밀번호는 여기서 하지 않아요.</div></form></div>')
    return page("팀·권한 관리", body)

@app.route("/admin/members/set", methods=["POST"])
def admin_members_set():
    uid = session.get("user_id")
    if not _is_platform_operator(uid):
        abort(403)
    from store.db import make_engine
    from store import member_admin as _ma
    eng = make_engine()
    sel = request.form.get("h", "")
    hid = _pg_hospital_id(sel)
    if not hid:
        return redirect(f"/admin/members?h={sel}&m=err")
    action = request.form.get("action"); role = request.form.get("role", "")
    try:
        if action == "addemail":
            u = _ma.find_user(eng, hid, uid, (request.form.get("email") or "").strip())
            if not u:
                return redirect(f"/admin/members?h={sel}&m=nouser")
            _ma.set_member_role(eng, hid, uid, u["user_id"], role, "add")
            return redirect(f"/admin/members?h={sel}&m=added")
        if action in ("add", "remove"):
            _ma.set_member_role(eng, hid, uid, request.form.get("user"), role, action)
            return redirect(f"/admin/members?h={sel}&m={'added' if action == 'add' else 'removed'}")
        abort(400)
    except Exception:
        return redirect(f"/admin/members?h={sel}&m=err")

@app.route("/new", methods=["POST"])
def new():
    name = request.form.get("name","").strip()
    hid = safe_id(name if re.match(r"^[a-zA-Z0-9_-]+$", name or "") else None)
    # 한글 병원명이면 id는 자동 생성(hosp-N)
    if not re.match(r"^[a-zA-Z0-9_-]+$", name or ""):
        n = 1
        while os.path.exists(cfg_path(f"hosp-{n}")): n += 1
        hid = f"hosp-{n}"
    elif os.path.exists(cfg_path(hid)):
        return redirect(f"/?err=exists")   # 기존 병원 ID 덮어쓰기 금지(데이터 손실·탈취 방지)
    # PG 먼저 provisioning → 충돌(다른 사람 소유 slug)이면 로컬 config 만들기 전에 차단
    try:
        _provision_pg(hid, name)
    except _ProvConflict:
        return redirect(f"/?err=taken")
    tpl = os.path.join(ROOT, "config", "_template.yaml")
    src = open(tpl, encoding="utf-8").read() if os.path.exists(tpl) else "hospital:\n  id: __HOSPITAL_ID__\n"
    src = src.replace("__HOSPITAL_ID__", hid)
    # 폼 값 반영 (간단 치환)
    def setline(txt, key, val):
        return re.sub(rf'(\n\s*{key}:\s*).*', rf'\g<1>"{val}"', txt, count=1)
    src = setline(src, "name", name)
    src = setline(src, "host", request.form.get("host","원장"))
    src = setline(src, "tagline", request.form.get("tagline","건강한 하루를 함께"))
    diseases = [d.strip() for d in request.form.get("diseases","").split(",") if d.strip()]
    src = re.sub(r'\ndiseases:.*', "\ndiseases: [" + ", ".join(diseases) + "]", src, count=1)
    open(cfg_path(hid), "w", encoding="utf-8").write(src)
    for s in ("raw","corpus","kb","out"): data_dir(hid, s)
    return redirect(f"/h/{hid}")

class _ProvConflict(Exception):
    """provisioning 충돌(다른 사용자 소유 slug)을 /new 상위로 전달."""

def _provision_pg(slug, name):
    """새 병원을 PostgreSQL에 provisioning(SECURITY DEFINER 함수). 로그인 PG유저면 creator admin.
    slug가 다른 사용자 소유면 _ProvConflict. 그 외 오류(DB 미연결 등)는 삼켜서 파일 흐름 유지."""
    try:
        from store.db import make_engine
        from store.provision import provision_hospital, ProvisionConflict
    except Exception:
        return
    try:
        provision_hospital(make_engine(), slug, name, owner_user=session.get("user_id"))
    except ProvisionConflict:
        raise _ProvConflict(slug)
    except Exception:
        pass   # DB 미연결 등은 비-PG 파일 흐름으로 진행(fallback 차단은 생성 시점에서)

def _clear_raw_dir(raw_dir):
    """생성 전 raw/ 의 기존 파일 제거(스냅샷 정확 복원 전 stale 혼입 방지). 하위 폴더는 건드리지 않음."""
    if not os.path.isdir(raw_dir):
        os.makedirs(raw_dir, exist_ok=True); return
    for nm in os.listdir(raw_dir):
        p = os.path.join(raw_dir, nm)
        if os.path.isfile(p):
            try: os.remove(p)
            except OSError: pass

def _pg_membership_id(hid):
    """세션 사용자의 이 병원 membership(생성 요청자 결착용). 요청 컨텍스트에서만 유효."""
    try:
        from store.db import make_engine
        from sqlalchemy import text
        from store.repositories import tenant_conn
        uid = session.get("user_id")
        if not hid or not uid:
            return None
        with tenant_conn(make_engine(), hid) as cn:
            return cn.execute(text("select id from hospital_memberships "
                                   "where hospital_id=:h and user_id=:u and archived_at is null"),
                              {"h": hid, "u": uid}).scalar()
    except Exception:
        return None

def _pg_required():
    """이 배포가 PostgreSQL을 단일 원본으로 쓰는가(DATABASE_URL 설정). 순수 로컬 개발이면 False."""
    return bool(os.environ.get("DATABASE_URL"))

def _require_pg(h):
    """병원의 PG hospital_id 확보. 없으면 config로 provision 자동복구 시도 후 재확인.
    반환 None이면 이 병원은 PG에 없음(→ _pg_required 배포에선 신규쓰기 차단)."""
    hid = _pg_hospital_id(h)
    if hid:
        return hid
    try:   # 자동복구: config 있으면 provision 시도(멱등)
        if os.path.exists(cfg_path(h)):
            cfg = _yaml().safe_load(open(cfg_path(h), encoding="utf-8")) or {}
            _provision_pg(h, cfg.get("hospital", {}).get("name", h))
    except Exception:
        pass
    return _pg_hospital_id(h)

@app.route("/h/<h>")
def hospital(h):
    if not os.path.exists(cfg_path(h)): abort(404)
    cfg = _yaml().safe_load(open(cfg_path(h), encoding="utf-8")) or {}
    name = cfg.get("hospital",{}).get("name", h)
    diseases = cfg.get("diseases") or []
    _csrf = session.get("_csrf", "")
    raw = _material_names(h)     # 영속(PG) 우선, 없으면 disk
    outs = sorted(glob.glob(os.path.join(data_dir(h,"out"),"*.html")))
    _files_li = "".join(f"<li>{f}</li>" for f in raw)
    if _files_li:      # 자료 많으면 접어두기(기본 접힘) — 개수만 보이고 펼치면 목록
        filelist = (f'<details style="margin-top:6px"><summary style="cursor:pointer;font-weight:700;color:var(--accent)">'
                    f'📎 업로드된 자료 {len(raw)}개 · 펼쳐 보기</summary><ul class=files style="margin-top:8px">{_files_li}</ul></details>')
    else:
        filelist = '<p class=muted style="margin-top:6px">아직 업로드된 자료가 없어요.</p>'
    # 필요 자료 체크리스트 (config의 input_checklist 기준, 파일명 매칭)
    from ingest.extract import categorize
    checklist = cfg.get("input_checklist", [])
    counts = {}
    for fn in raw:
        k = categorize(fn, checklist); counts[k] = counts.get(k, 0) + 1
        if fn.lower().endswith(".zip"):      # zip 안 자료도 카테고리 인식(예: 유튜브참고자료.zip 안 원장인터뷰)
            for inner in _zip_inner_names(h, fn):
                ik = categorize(inner, checklist)
                if ik != "기타":
                    counts[ik] = counts.get(ik, 0) + 1
    chk = ""; miss = []
    for it in checklist:
        k = it["key"]; req = it.get("required"); n = counts.get(k, 0)
        if k == "기타": continue
        cls = "ok" if n else ("no" if req else "dim")
        label = k + ("(필수)" if req else "") + (f" ✓{n}" if n else (" 없음" if req else ""))
        chk += f'<span class="pill {cls}">{label}</span>'
        if req and not n: miss.append(k)
    misswarn = (f'<div class=note style="border-color:var(--danger);color:var(--danger)">⚠️ 필수 자료가 빠졌어요: {", ".join(miss)} — 넣을수록 대본 품질이 올라가요.</div>' if miss else "")
    _ok = request.args.get("ok")
    if _ok and _ok.isdigit() and int(_ok) > 0:
        # 여기 도달 = upload가 PG(hid) 확보 후 저장 성공분. (미연결이면 애초에 ?err=nopg로 빠짐)
        misswarn += f'<div class=note style="border-color:var(--good);color:var(--good)">✅ {_ok}개 자료가 영구 저장됐어요(재시작·재배포에도 유지).</div>'
    elif _ok == "0":
        misswarn += '<div class=note style="border-color:var(--danger);color:var(--danger)">⚠️ 업로드된 파일이 없어요. 파일이 선택됐는지, 허용 형식(pdf·docx·txt·zip 등)인지 확인해 주세요.</div>'
    if request.args.get("big") == "1":
        misswarn += '<div class=note style="border-color:var(--warn,#e0a800);color:var(--warn,#b8860b)">⚠️ 40MB 넘는 파일은 <b>저장도 생성도 안 돼요</b>. zip은 풀어서 안의 파일이 각각 저장돼요 — 그래도 40MB 넘는 개별 파일(영상 등)은 제외됩니다.</div>'
    if request.args.get("err") == "nopg":
        misswarn += '<div class=note style="border-color:var(--danger);color:var(--danger)">⚠️ 이 병원은 영구저장(PostgreSQL)에 등록되지 않아 업로드를 막았어요(임시저장 방지). 관리자에게 병원 재등록을 요청하세요.</div>'
    _uprep = session.pop("_upreport", None)   # 업로드 파일별 결과(투명 공개) — 무엇이 저장/스킵/실패했는지
    if _uprep:
        _ic = {"ok": "✅", "big": "🚫", "skip": "⚠️", "fail": "❌"}
        _rows = "".join(
            f'<div style="display:flex;gap:8px;font-size:12.5px;padding:3px 0;border-bottom:1px solid var(--border)">'
            f'<span>{_ic.get(st, "•")}</span><span style="flex:1;word-break:break-all">{nm.replace("&","&amp;").replace("<","&lt;")}</span>'
            f'<span class=muted style="white-space:nowrap">{dt.replace("&","&amp;").replace("<","&lt;")}</span></div>'
            for (nm, st, dt) in _uprep)
        _nok = sum(1 for r in _uprep if r[1] == "ok")
        misswarn += (f'<details class=note open style="border-color:var(--border)"><summary style="cursor:pointer;font-weight:700">'
                     f'📋 업로드 결과 — 영구저장 {_nok}개 / 전체 {len(_uprep)}개 (자세히)</summary>'
                     f'<div style="margin-top:8px">{_rows}</div></details>')
    # ③ 결과물: 목록. disk .html ∪ PG(script_artifacts) → 재배포로 디스크가 비어도 목록 유지.
    def _topic_of(fn):
        b = os.path.basename(fn)
        return b[:-len("_package.html")] if b.endswith("_package.html") else b[:-5]
    _seen = set(); _topics = []
    for _t in [_topic_of(o) for o in outs] + _pg_result_topics(h):
        if _t and _t not in _seen:
            _seen.add(_t); _topics.append(_t)
    def _esc_t(t): return t.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
    # 버튼 하나로: '결과 보기'가 곧 편집 화면(예쁜 스토리보드 + 대사·AI사진·논문사진 수정). 미리보기 분리 제거.
    outlist = ("".join(f'<div class=out><span>{_esc_t(t)}</span>'
                       f'<a class="btn pri" href="/h/{h}/edit/{_esc_t(t)}">📄 결과 보기 · 수정</a></div>'
                       for t in _topics) or '<div class=muted>아직 만든 대본이 없어요.</div>')
    dz_opts = "".join(f'<button type=button class="btn dz" onclick="setTopic(this)">{d}</button>' for d in diseases)
    job = job_get(h)
    running = job.get("running")
    # 생성 눌러도 주제가 리셋된 것처럼 보이지 않게 — 직전/현재 job의 주제를 입력칸에 유지
    topic_val = (job.get("topic") or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    body = f"""
    <div class=row style="justify-content:space-between">
      <div><h1>{name}</h1><p class=sub>{h}</p></div><a href="/" class=btn>← 병원 목록</a>
    </div>

    <div class=card>
      <h2 style="margin-top:0">① 자료 업로드</h2>
      <div class=note>이런 자료를 넣어주세요 — <b>✓</b>=받음 · <b>없음</b>=필수인데 안 들어옴 · 회색=선택</div>
      <div class=chk style="margin:11px 0 6px">{chk}</div>
      {misswarn}
      <form id=upf method=post action="/h/{h}/upload" enctype=multipart/form-data
            onsubmit="return showUp();"><input type=hidden name=_csrf value="{_csrf}">
        <div class=drop id=drop><span id=drophint>파일을 여기로 끌어다 놓거나 클릭 (pdf·docx·txt·zip)</span>
          <input id=fin type=file name=files multiple style="display:none">
        </div>
        <div id=fsel class=muted style="margin-top:8px;font-size:13px;line-height:1.7"></div>
        <div class=row style="margin-top:12px"><button class="btn pri" id=upbtn type=submit>업로드</button>
        <span id=upmsg class=muted style="display:none;color:var(--accent);font-weight:700">⏳ 파일 올리는 중이에요 — 창을 닫지 마세요(용량 크면 시간이 걸려요).</span>
        <span class=muted>설문지·인터뷰·논문·강의자료·기존 대본 등. zip 통째로도 OK</span></div>
      </form>
      {filelist}
    </div>

    <div class=card>
      <h2 style="margin-top:0">② 대본 만들기</h2>
      <div class=note>주력 질환: {", ".join(diseases) or "설정에 없음"} — 아래 버튼 누르면 주제 자동 입력</div>
      <div class=chk style="margin:10px 0">{dz_opts}</div>
      <form id=runf method=post action="/h/{h}/run" onsubmit="var r=document.getElementById('reqkey');r.value=(window.crypto&&crypto.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random());document.getElementById('runbtn').disabled=true;">
        <input type=hidden name=reqkey id=reqkey><input type=hidden name=_csrf value="{_csrf}">
        <label>주제</label><input type=text id=topic name=topic value="{topic_val}" placeholder="예: 오십견" required>
        <label class=opt style="display:flex;gap:9px;align-items:flex-start;margin-top:14px;padding:12px 14px;background:var(--surface);border:1px solid var(--border);border-radius:10px;cursor:pointer;font-weight:600">
          <input type=checkbox name=evidence value=1 checked style="margin-top:3px;width:17px;height:17px;accent-color:var(--accent)">
          <span>📄 의학 논문으로 근거 강화 <span class=muted style="font-weight:500">(선택)</span><br>
          <span class=muted style="font-weight:500;font-size:13px">업로드 자료에 논문이 있으면 대본 수치를 <b>원문과 대조</b>하고 <b>전후 그림</b>을 추출해요. 논문이 없으면 근거 없이 생성돼요(없는 논문은 절대 안 지어냄).</span></span>
        </label>
        <div class=row style="margin-top:14px">
          <button class="btn pri" id=runbtn type=submit {'disabled' if running else ''}>{'생성 중…' if running else '대본 만들기'}</button>
          <span class=muted>수집→KB→대본→검사→대시보드까지 자동. 몇 분 걸려요.</span>
        </div>
      </form>
      <div id=status></div>
    </div>

    <div class=card>
      <h2 style="margin-top:0">③ 결과물</h2>
      {outlist}
    </div>

    <div class=card>
      <h2 style="margin-top:0">🔍 유튜브 벤치마킹 <span class=muted style="font-size:13px;font-weight:500">(신규)</span></h2>
      <div class=note>잘나가는 채널을 분석해 '흥행 공식'을 뽑고 우리 기획안으로 이어가요. 자료가 부족한 신규 광고주에 특히 유용해요.</div>
      <div class=row style="margin-top:12px"><a class="btn pri" href="/h/{h}/benchmark">벤치마킹 열기 →</a>
        <a class="btn g" href="/guide/benchmark" target="_blank" rel="noopener">📖 사용 가이드</a></div>
    </div>

    <div id=genmodal style="display:none;position:fixed;inset:0;z-index:9999;background:rgba(12,17,28,.6);backdrop-filter:blur(4px);align-items:center;justify-content:center">
      <div style="background:var(--card,#fff);border-radius:20px;max-width:460px;width:90%;padding:32px 30px;box-shadow:0 24px 70px rgba(0,0,0,.35);text-align:center">
        <div style="font-size:44px;margin-bottom:2px">✍️</div>
        <div style="font-size:20px;font-weight:800;margin-bottom:2px">대본을 만들고 있어요</div>
        <div id=gmtopic class=muted style="font-size:13px;margin-bottom:10px"></div>
        <div id=gmelapsed style="font-size:38px;font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:1px;margin:4px 0;color:var(--accent)">0:00</div>
        <div style="height:8px;border-radius:6px;background:var(--surface,#eee);overflow:hidden;margin:10px 0 4px">
          <div id=gmbar style="height:100%;width:0%;background:var(--accent);transition:width .8s ease"></div>
        </div>
        <div id=gmstage style="font-size:15px;font-weight:800;min-height:20px;margin:8px 0;color:var(--ink,#222)"></div>
        <div class=muted style="font-size:12.5px;line-height:1.65;margin:10px 0 6px">최상의 대본을 만들기 위해 시간이 <b>15~20분</b> 소요됩니다.<br>이 페이지를 나가도 생성은 <b>계속</b>돼요 — 나중에 다시 들어와 결과를 확인하면 됩니다.</div>
        <details style="margin-top:8px;text-align:left"><summary class=muted style="cursor:pointer;font-size:11.5px">자세한 로그 보기</summary>
          <div id=gmlog style="margin-top:6px;max-height:120px;overflow:auto;font-size:10.5px;font-family:monospace;color:var(--muted,#888);background:var(--surface,#f5f5f5);border-radius:9px;padding:9px;white-space:pre-wrap;line-height:1.5"></div>
        </details>
      </div>
    </div>

    <div id=upmodal style="display:none;position:fixed;inset:0;z-index:9999;background:rgba(12,17,28,.6);backdrop-filter:blur(4px);align-items:center;justify-content:center">
      <div style="background:var(--card,#fff);border-radius:20px;max-width:440px;width:90%;padding:32px 30px;box-shadow:0 24px 70px rgba(0,0,0,.35);text-align:center">
        <div style="font-size:44px;margin-bottom:2px">📦</div>
        <div style="font-size:19px;font-weight:800;margin-bottom:6px">자료를 올리고 있어요</div>
        <div id=upel style="font-size:34px;font-weight:800;font-variant-numeric:tabular-nums;color:var(--accent);margin:4px 0">0:00</div>
        <div class=spin style="width:34px;height:34px;border:3px solid var(--border,#ddd);border-top-color:var(--accent);border-radius:50%;margin:8px auto;animation:sp 0.9s linear infinite"></div>
        <div class=muted style="font-size:12.5px;line-height:1.65;margin:8px 0">zip은 <b>풀어서 안의 파일을 하나하나 분석·저장</b>하느라 시간이 좀 걸려요.<br>파일이 많거나 크면 <b>1~3분, 더 걸릴 수도</b> 있어요.<br><b>창을 닫지 마세요</b> — 끝나면 결과가 자동으로 떠요.</div>
      </div>
    </div>
    <style>@keyframes sp{{to{{transform:rotate(360deg)}}}}</style>"""
    script = """<script>
    var drop=document.getElementById('drop'),fin=document.getElementById('fin');
    var BAG=new DataTransfer();   // 여러 번 나눠 골라도 계속 누적(input은 원래 덮어써서)
    function render(){var n=BAG.files.length;
      document.getElementById('drophint').textContent=n?(n+'개 선택됨 — 업로드를 누르세요'):'파일을 여기로 끌어다 놓거나 클릭 (pdf·docx·txt·zip)';
      var names=[].map.call(BAG.files,function(f){return '📎 '+f.name;}).join('<br>');
      document.getElementById('fsel').innerHTML=n?('선택된 파일 '+n+'개:<br>'+names+'<br><a href="#" id=fclr style="font-size:12px">전체 지우기</a>'):'';
      if(n){document.getElementById('fclr').onclick=function(e){e.preventDefault();BAG=new DataTransfer();fin.files=BAG.files;render();};}}
    function addFiles(list){for(var i=0;i<list.length;i++){BAG.items.add(list[i]);}fin.files=BAG.files;render();}
    drop.onclick=function(){fin.click()};
    fin.onchange=function(){ // 이번에 새로 고른 것만 누적에 추가(중복 이름은 제외)
      var have={};for(var j=0;j<BAG.files.length;j++)have[BAG.files[j].name]=1;
      var add=[];for(var i=0;i<fin.files.length;i++){if(!have[fin.files[i].name])add.push(fin.files[i]);}
      if(add.length)addFiles(add);else render();};
    ['dragover','dragenter'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('over')}));
    ['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('over')}));
    drop.addEventListener('drop',ev=>{addFiles(ev.dataTransfer.files)});
    window.setTopic=function(b){document.getElementById('topic').value=b.textContent};
    // 업로드 팝업(가운데) + 경과시간 — zip은 서버가 풀어서 분석하느라 오래 걸릴 수 있음을 안내
    window.showUp=function(){
      if(!BAG.files.length){return true;}   // 파일 없으면 그냥 진행(서버가 처리)
      var m=document.getElementById('upmodal'); if(m) m.style.display='flex';
      var b=document.getElementById('upbtn'); if(b){b.disabled=true;b.textContent='⏳ 업로드 중…';}
      var t0=Date.now();
      setInterval(function(){var e=Math.floor((Date.now()-t0)/1000);
        var el=document.getElementById('upel'); if(el) el.textContent=Math.floor(e/60)+':'+('0'+(e%60)).slice(-2);},1000);
      return true;
    };
    // 생성 상태 폴링 + 로딩 모달(가운데 팝업 · 경과시간 · 진행바)
    var HID=%HID%;
    var EST=1080;                 // 예상 총 소요(초) ≈ 18분 — 진행바 추정용
    var srvEl=0, anchor=Date.now(), tick=null;
    var modal=document.getElementById('genmodal');
    var sawRunning=%RUNNING%, submitAt=(%RUNNING%?0:null);   // 완료 깜빡 방지: 방금 제출했는데 아직 running 못 봤으면 '완료' 처리 보류
    function fmt(t){t=Math.max(0,Math.floor(t));var m=Math.floor(t/60),s=t%60;return m+':'+(s<10?'0':'')+s;}
    // 원시 로그(모델명 등) 대신 친근한 단계만 뽑아 보여줌
    function stages(log){log=log||'';
      var S=[['자료 수집·정리',/코퍼스|정규화|추출|ingest/i],['지식 정리(KB)',/\bKB\b|profile|disease|competitor|생성 시작/i],
             ['대본 집필',/director|대본 패키지|대본 만들|러닝타임/i],['논문 근거 대조',/근거 대조|근거 강화|시각자료/i],
             ['편집·이미지 준비',/적재|편집·근거·이미지/i]];
      var done=[],cur='준비 중';
      S.forEach(function(x){if(x[1].test(log)){done.push('✓ '+x[0]);cur=x[0]+' 중…';}});
      return {list:done.length?done.join('\n'):'시작하는 중…', cur:cur};
    }
    function showModal(j){
      modal.style.display='flex';
      document.getElementById('gmtopic').textContent=(j.topic?('주제: '+j.topic):'');
      var st=stages(j.log);
      document.getElementById('gmstage').textContent=st.cur;
      var gl=document.getElementById('gmlog');gl.textContent=st.list;
      srvEl=(j.elapsed||0);anchor=Date.now();
      if(!tick){tick=setInterval(function(){
        var e=srvEl+(Date.now()-anchor)/1000;
        document.getElementById('gmelapsed').textContent=fmt(e);
        document.getElementById('gmbar').style.width=Math.min(97,Math.round(e/EST*100))+'%';
      },1000);}
    }
    function hideModal(){modal.style.display='none';if(tick){clearInterval(tick);tick=null;}}
    function poll(){fetch('/h/'+HID+'/status').then(r=>r.json()).then(j=>{
      var s=document.getElementById('status'),btn=document.getElementById('runbtn');
      if(j.running){sawRunning=true;btn.disabled=true;btn.textContent='생성 중…';showModal(j);
        s.innerHTML='<div class=log>'+stages(j.log).list+'</div>';setTimeout(poll,2500);}
      else{
        if(submitAt!==null && !sawRunning && (Date.now()-submitAt)<25000){setTimeout(poll,1500);return;}  // 새 job 아직 안 뜸 → 완료 오인 방지
        hideModal();
        var msg = j.ok
          ? '<p style="color:var(--good);font-weight:800;margin-top:10px">✅ 완료 — 아래 결과물에서 확인하세요.</p>'
          : ((j.status==='failed'||j.error) ? '<p style="color:var(--danger);font-weight:800;margin-top:10px">⛔ 실패 — '+(j.error||'의료광고 검수 불통과 또는 오류')+' (검수 통과 전엔 게시되지 않아요)</p>' : '');
        s.innerHTML=msg;
        btn.disabled=false;btn.textContent='대본 만들기';
        if(j.ok)setTimeout(()=>location.reload(),1600);}
    }).catch(function(){setTimeout(poll,4000);})}   // 폴링 실패해도 멈추지 않고 재시도(생성은 서버에서 계속)
    document.getElementById('runf').addEventListener('submit',function(){
      submitAt=Date.now();sawRunning=false;
      modal.style.display='flex';document.getElementById('gmstage').textContent='생성을 시작하고 있어요…';
      setTimeout(poll,1200);});
    if(%RUNNING%)poll();
    </script>""".replace("%HID%", '"'+h+'"').replace("%RUNNING%", "true" if running else "false")
    return page(name, body, script)

IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}   # 이미지=논문 그림으로 직접 사용(장면에 붙임)
ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md", ".csv", ".hwp", ".pptx", ".zip"} | IMG_EXT

def safe_filename(fn):
    """한글 보존 안전 파일명. werkzeug secure_filename은 비ASCII를 통째로 날려
    '설문지.pdf'→'pdf'로 만들어 한글 자료가 유실됨. 경로탈출·제어문자만 제거하고 한글은 유지."""
    fn = (fn or "").replace("\\", "/").split("/")[-1]     # 경로 제거(디렉터리 탈출 방지)
    fn = unicodedata.normalize("NFC", fn)
    fn = re.sub(r"[\x00-\x1f\x7f]", "", fn)               # 제어문자 제거
    fn = fn.replace("/", "").strip().lstrip(".")          # 구분자·선행 점 제거
    fn = re.sub(r"\.{2,}", ".", fn)                       # '..' 붕괴
    return fn[:200]

@app.route("/h/<h>/upload", methods=["POST"])
def upload(h):
    if not os.path.exists(cfg_path(h)): abort(404)
    dest = data_dir(h, "raw")
    hid = _require_pg(h)         # PG 병원 확보(자동복구 시도). 자료를 PostgreSQL에 영속 저장.
    if _pg_required() and not hid:   # PG 단일원본 배포인데 이 병원이 PG에 없음 → 임시디스크 전용 쓰기 차단
        return redirect(f"/h/{h}?err=nopg")
    eng = None; _MAT_MAX = 40 * 1024 * 1024
    if hid:
        try:
            from store.db import make_engine
            from store.materials import save_material, MAX_BYTES as _MAT_MAX
            eng = make_engine()
        except Exception:
            eng = None
    import zipfile as _zip
    _INNER_OK = ALLOWED_EXT - {".zip"}       # zip 안 leaf(문서·이미지). zip은 재귀로 따로 처리.
    saved = 0; toobig = []; report = []      # report: (표시명, 상태, 상세) — 사용자에게 투명 공개
    _uid = session.get("user_id")

    def _persist(nm, data, tag=""):
        """bytes 하나를 PG 영구저장. 상태를 report에 남김. 성공 시 True."""
        nonlocal saved
        if len(data) > _MAT_MAX:
            toobig.append(nm); report.append((tag + nm, "big", f"{len(data)//(1024*1024)}MB(40MB 초과) — 저장·생성 제외"))
            return False
        if not eng:
            report.append((tag + nm, "fail", "DB 연결 없음")); return False
        try:
            save_material(eng, hid, nm, data, created_by=_uid)
            saved += 1; report.append((tag + nm, "ok", f"영구저장 {max(1,len(data)//1024)}KB")); return True
        except Exception as e:
            report.append((tag + nm, "fail", f"PG 저장 실패: {type(e).__name__}: {e}")); return False

    def _walk_zipfile(zf, prefix, depth):
        """열린 zip의 leaf 파일을 각각 _persist. 중첩 zip(논문그림_전후사진.zip 등)은 depth까지 재귀."""
        members = [zi for zi in zf.infolist() if not zi.is_dir()]
        if not members:
            report.append((prefix + "(빈 zip)", "skip", "내용 없음")); return
        for zi in members:
            raw_nm = _decode_zipname(zi)
            inner = safe_filename(os.path.basename(raw_nm.replace("\\", "/")))
            if not inner or inner.startswith(".") or "__MACOSX" in raw_nm:
                continue
            iext = os.path.splitext(inner)[1].lower()
            if iext == ".zip":                       # 중첩 zip → 한 겹 더 풀기
                if depth <= 0:
                    report.append((prefix + inner, "skip", "중첩이 너무 깊어요")); continue
                if zi.file_size > 300 * 1024 * 1024:
                    report.append((prefix + inner, "skip", "중첩 zip이 너무 큼")); continue
                try:
                    with _zip.ZipFile(io.BytesIO(zf.read(zi))) as zf2:
                        _walk_zipfile(zf2, prefix + inner + " ▸ ", depth - 1)
                except _zip.BadZipFile:
                    report.append((prefix + inner, "fail", "손상된 중첩 zip"))
                except Exception as e:
                    report.append((prefix + inner, "fail", f"중첩 zip 오류: {e}"))
                continue
            if iext not in _INNER_OK:
                report.append((prefix + inner, "skip", f"형식({iext or '없음'})")); continue
            if zi.file_size > _MAT_MAX:              # 읽기 전에 크기로 걸러 메모리 폭증 방지
                toobig.append(inner); report.append((prefix + inner, "big", f"{zi.file_size//(1024*1024)}MB(40MB 초과)")); continue
            try:
                data = zf.read(zi)
            except Exception as e:
                report.append((prefix + inner, "fail", f"압축해제 실패: {e}")); continue
            _persist(inner, data, tag=prefix)

    for f in request.files.getlist("files"):
        if not f or not f.filename: continue
        name = safe_filename(f.filename)     # 한글 유지 + 경로탈출 방지
        if not name: continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXT:           # 허용 확장자만(눈에 보이게 스킵 기록)
            report.append((name, "skip", f"허용 안 되는 형식({ext or '없음'})")); continue
        path = os.path.join(dest, name)
        try:
            f.save(path)                     # 스트리밍 저장(대용량도 메모리 부담↓)
        except Exception as e:
            report.append((name, "fail", f"디스크 저장 실패: {e}")); continue
        if ext == ".zip":
            # zip은 통째로 PG에 넣지 않고(대개 40MB↑) '안의 파일들'을 각각 영구저장(중첩 zip도 재귀).
            # 이미지(png/jpg 등)도 leaf로 저장 → 생성 때 '논문 그림'으로 장면에 붙음. disk lazy 읽기로 메모리 절약.
            try:
                with _zip.ZipFile(path) as zf:
                    _walk_zipfile(zf, name + " ▸ ", 2)
            except _zip.BadZipFile:
                report.append((name, "fail", "손상된 zip"))
            except Exception as e:
                report.append((name, "fail", f"zip 처리 오류: {e}"))
        else:
            try:
                with open(path, "rb") as fh:
                    _persist(name, fh.read())
            except Exception as e:
                report.append((name, "fail", f"읽기 실패: {e}"))

    session["_upreport"] = report[:120]      # 병원 페이지에서 파일별 결과 공개(이미지 많을 수 있어 넉넉히)
    q = f"?ok={saved}" + ("&big=1" if toobig else "") + ("&uperr=1" if any(r[1] == "fail" for r in report) else "")
    return redirect(f"/h/{h}{q}")

def _pg_hospital_id(slug):
    """대시보드 병원 slug ↔ PostgreSQL hospital 매핑. 없으면 None(구식 파일 흐름만)."""
    try:
        from store.db import make_engine
        from sqlalchemy import text
        with make_engine().connect() as cn:
            return cn.execute(text("select id from hospitals where slug=:s"), {"s": slug}).scalar()
    except Exception:
        return None

def _material_names(h):
    """업로드 자료 목록 — PG(영속) ∪ disk. 재배포로 disk가 비어도 PG에 남아있음."""
    names = {os.path.basename(p) for p in glob.glob(os.path.join(data_dir(h, "raw"), "*")) if os.path.isfile(p)}
    hid = _pg_hospital_id(h)
    if hid:
        try:
            from store.db import make_engine
            from store.materials import list_materials
            names |= {m["filename"] for m in list_materials(make_engine(), hid)}
        except Exception:
            pass
    return sorted(names)

def _decode_zipname(zi):
    """zip 내부 파일명 디코딩 — Windows에서 만든 zip은 한글이 cp437로 저장돼 mojibake가 됨. 복원 시도."""
    if zi.flag_bits & 0x800:        # UTF-8 플래그면 그대로
        return zi.filename
    for enc in ("cp949", "euc-kr", "utf-8"):
        try:
            return zi.filename.encode("cp437").decode(enc)
        except Exception:
            continue
    return zi.filename

def _zip_inner_names(h, name):
    """zip 자료의 내부 파일명들(체크리스트가 zip '안' 자료도 인식하게). disk 우선→PG. 실패 시 []."""
    import io as _io, zipfile as _zip
    p = os.path.join(data_dir(h, "raw"), name)
    try:
        if os.path.isfile(p):
            with _zip.ZipFile(p) as zf:
                return [_decode_zipname(zi) for zi in zf.infolist() if not zi.is_dir()]
    except Exception:
        return []
    hid = _pg_hospital_id(h)
    if hid:
        try:
            from store.db import make_engine
            from store.materials import get_material
            _, data = get_material(make_engine(), hid, name)
            if data:
                with _zip.ZipFile(_io.BytesIO(data)) as zf:
                    return [_decode_zipname(zi) for zi in zf.infolist() if not zi.is_dir()]
        except Exception:
            return []
    return []

def _pg_result_topics(h):
    """PG(script_artifacts)에 결과물이 있는 topic 목록 — 재배포로 disk가 비어도 ③ 결과물에 표시."""
    hid = _pg_hospital_id(h)
    if not hid:
        return []
    try:
        from store.db import make_engine
        from store.artifacts import list_topics
        return list_topics(make_engine(), hid)
    except Exception:
        return []

def _restore_out_artifacts(h, topic):
    """디스크에 결과물(html·패키지)이 없을 때 PG에서 out/로 복원 — 미리보기·편집용. 복원 파일 수 반환."""
    hid = _pg_hospital_id(h)
    if not hid:
        return 0
    try:
        from store.db import make_engine
        from store.artifacts import restore_to_out_dir
        return restore_to_out_dir(make_engine(), hid, os.path.basename(topic), data_dir(h, "out"))
    except Exception:
        return 0

def _pg_script_id_for_topic(hid, topic):
    """이 병원에서 해당 topic의 기존 대본 script_id(있으면) → 재생성 시 그 대본의 새 버전으로 이어짐(topic은 표시용)."""
    try:
        from store.db import make_engine
        from sqlalchemy import text
        from store.repositories import tenant_conn
        with tenant_conn(make_engine(), hid) as cn:
            return cn.execute(text("select id from scripts where hospital_id=:h and topic=:t "
                                   "order by created_at limit 1"), {"h": hid, "t": topic}).scalar()
    except Exception:
        return None

def _inline_workspace(h):
    """병원 페이지에 박아넣을 인라인 편집기 — 현재 대본을 대사별(텍스트+그 장면 AI 사진)로 이 화면에서
    바로 수정. 저장·이미지 다시뽑기·되돌리기 모두 여기서(return_to로 이 페이지 복귀). PG 계정만."""
    if not session.get("user_id"):
        return ('<div class=card><p class=muted>대본 편집은 병원 담당 PostgreSQL 계정으로 로그인해야 열립니다'
                '(현재 계정은 편집 권한이 없어요).</p></div>')
    try:
        from services.context import ActorContext
        from services import workspace as _ws
        from services.exceptions import ServiceError
        from presentation import render as _render
        from presentation.urls import DashboardUrls
        from store.db import make_engine
        from store.repositories import tenant_conn
        from sqlalchemy import text
        eng = make_engine()
        ctx = ActorContext.resolve(eng, session.get("user_id"), h, getattr(g, "request_id", None))
        with tenant_conn(eng, ctx.hospital_id) as cn:
            vid = cn.execute(text("select current_version_id from scripts where hospital_id=:h "
                                  "and current_version_id is not null order by updated_at desc limit 1"),
                             {"h": ctx.hospital_id}).scalar()
        if not vid:
            return ""      # 아직 대본 없음
        wsd = _ws.get_version_workspace(eng, ctx, vid)
        return _render.version_page(wsd, DashboardUrls(h), session.get("_csrf", ""),
                                    msg_code=request.args.get("m"), return_to=f"/h/{h}", embed=True)
    except ServiceError:
        return ""
    except Exception:
        return ""


def _pg_studio_url(h):
    """이 병원의 최신 PG 버전 편집 URL(대시보드 canonical /scripts, Step 7B부터 대시보드가 직접 렌더). 없으면 None."""
    try:
        from store.db import make_engine
        from sqlalchemy import text
        from store.repositories import tenant_conn
        hid = _pg_hospital_id(h)
        if not hid:
            return None
        with tenant_conn(make_engine(), hid) as cn:
            vid = cn.execute(text("select current_version_id from scripts where hospital_id=:h "
                                  "and current_version_id is not null order by updated_at desc limit 1"),
                             {"h": hid}).scalar()
        return f"/scripts/{h}/{vid}" if vid else None
    except Exception:
        return None

@app.route("/scripts/<h>/<version_id>")
@app.route("/scripts/<h>/<version_id>/<section>")
def scripts_edit(h, version_id, section="edit"):
    """Step 7B: 버전페이지를 대시보드가 canonical로 '직접 렌더'(workspace service + 공유 presentation).
    쓰기·자산 액션은 전환기 동안 /studio compat 엔드포인트로(세션·CSRF 공유). Step 9에서 대시보드로 이전.
    인증은 _guard(before_request)가 session['user'] 강제 + 여기서 user_id로 ActorContext 해석."""
    from services.context import ActorContext
    from services import workspace as workspace_service
    from services.exceptions import ServiceError
    from presentation import render as _render
    from presentation.urls import DashboardUrls
    from store.db import make_engine
    eng = make_engine()
    if not session.get("user_id"):
        # 레거시 users.yaml 계정(admin 등)은 PostgreSQL 신원이 없어 편집화면(PG 멤버십 필요)을 못 엶.
        # 홈으로 조용히 튕기지 않고 원인·해결을 안내(계정 이슈이지 기능 오류 아님).
        return _render.page("편집 권한 계정 필요",
            '<div class=card><h1>이 계정으로는 편집화면을 열 수 없어요</h1>'
            '<p>편집 · 근거검증 · 장면이미지 · 승인 화면은 <b>PostgreSQL 계정</b>(병원 담당 이메일)으로 '
            '로그인해야 열립니다. 지금 로그인한 <b>admin</b>은 관리용 레거시 계정이라 병원 멤버십이 없습니다.</p>'
            '<p>로그아웃 후 <b>병원 담당 이메일 계정</b>(예: <code>demo@boncure.kr</code>)으로 다시 로그인하면 '
            '편집화면과 장면이미지가 바로 열립니다.</p>'
            '<a class="btn" href="/logout">로그아웃하고 다시 로그인</a> '
            '<a class="btn g" href="/" style="margin-left:6px">← 대시보드</a></div>'), 200
    try:
        ctx = ActorContext.resolve(eng, session.get("user_id"), h, getattr(g, "request_id", None))
        ws = workspace_service.get_version_workspace(eng, ctx, version_id)
    except ServiceError as e:
        if e.http_status == 401:
            return redirect("/login?next=" + f"/scripts/{h}/{version_id}")
        abort(e.http_status)
    return _render.version_page(ws, DashboardUrls(h), session.get("_csrf", ""),
                                msg_code=request.args.get("m"))


# ── Step 9(선반영): 편집·검수·승인·이미지 쓰기를 대시보드가 직접 소유(studio 미경유) ──
#   라우트=파싱+ctx resolve+공통 service+예외매핑만. CSRF는 _guard(POST)가 검증. redirect는 대시보드로.
def _dash_ctx(h):
    from services.context import ActorContext
    from store.db import make_engine
    return ActorContext.resolve(make_engine(), session.get("user_id"), h, getattr(g, "request_id", None))


def _ret(default_path, msg=None, frag=""):
    """저장 후 복귀 경로. 폼의 return_to(대시보드 인라인)면 그리로, 아니면 default(/scripts…).
    안전한 로컬 경로만 허용(/h/ 또는 /scripts/)."""
    rt = request.form.get("return_to") or ""
    base = rt if (rt.startswith("/h/") or rt.startswith("/scripts/")) else default_path
    if msg:
        base += ("&" if "?" in base else "?") + "m=" + msg
    return base + frag


@app.post("/scripts/<h>/<script_id>/edit")
def d_edit(h, script_id):
    import uuid as _uuid
    from services import scripts as _svc
    from services.exceptions import ServiceError
    from store.db import make_engine
    expected = request.form.get("expected")
    try:
        _uuid.UUID(expected); _uuid.UUID(script_id)
    except (TypeError, ValueError):
        abort(400)
    edits = {k[6:]: v for k, v in request.form.items() if k.startswith("edit__")}
    try:
        res = _svc.edit_blocks(make_engine(), _dash_ctx(h), script_id, expected, edits)
        _frag = f"#say_{next(iter(edits))}" if edits else ""   # 저장 후 그 대사 위치로(맨 위 스크롤 방지)
        if res.get("no_change"):
            return redirect(_ret(f"/scripts/{h}/{expected}", None, _frag))
        return redirect(_ret(f"/scripts/{h}/{res['version_id']}", "edited", _frag))
    except ServiceError as e:
        if e.http_status == 401:
            return redirect("/login")
        if e.http_status == 409:
            return redirect(_ret(f"/scripts/{h}/{expected}", "conflict"))
        abort(e.http_status)


def _dash_approval(h, version_id, fn, ok_msg):
    import uuid as _uuid
    from services.exceptions import ServiceError
    try:
        _uuid.UUID(version_id)
    except (TypeError, ValueError):
        abort(400)
    try:
        fn(_dash_ctx(h))
        return redirect(_ret(f"/scripts/{h}/{version_id}", ok_msg))
    except ServiceError as e:
        m = {403: "e403", 422: "e422", 409: "conflict"}.get(e.http_status)
        if m is None:
            if e.http_status == 401:
                return redirect("/login")
            abort(e.http_status)
        return redirect(_ret(f"/scripts/{h}/{version_id}", m))


@app.post("/scripts/<h>/versions/<version_id>/approve")
def d_approve(h, version_id):
    from services import approvals as _svc
    from store.db import make_engine
    return _dash_approval(h, version_id, lambda ctx: _svc.approve(make_engine(), ctx, version_id), "approved")


@app.post("/scripts/<h>/versions/<version_id>/reject")
def d_reject(h, version_id):
    from services import approvals as _svc
    from store.db import make_engine
    reason = (request.form.get("reason") or "").strip()
    return _dash_approval(h, version_id, lambda ctx: _svc.reject(make_engine(), ctx, version_id, reason), "rejected")


@app.post("/scripts/<h>/versions/<version_id>/revoke")
def d_revoke(h, version_id):
    from services import approvals as _svc
    from store.db import make_engine
    reason = (request.form.get("reason") or "").strip()
    return _dash_approval(h, version_id, lambda ctx: _svc.revoke(make_engine(), ctx, version_id, reason), "revoked")


@app.post("/scripts/<h>/versions/<version_id>/self-approve")
def d_self_approve(h, version_id):
    from services import approvals as _svc
    from store.db import make_engine
    reason = (request.form.get("reason") or "").strip()
    return _dash_approval(h, version_id, lambda ctx: _svc.self_approve(make_engine(), ctx, version_id, reason), "approved")


@app.post("/scripts/<h>/claims/<claim_id>/review")
def d_review(h, claim_id):
    import uuid as _uuid
    from services import evidence as _svc
    from services.exceptions import ServiceError
    from store.db import make_engine
    version_id = request.form.get("version_id"); script_id = request.form.get("script_id")
    decision = request.form.get("decision")
    try:
        _uuid.UUID(claim_id); _uuid.UUID(version_id); _uuid.UUID(script_id)
    except (TypeError, ValueError):
        abort(400)
    try:
        _svc.assess_claim(make_engine(), _dash_ctx(h), script_id, version_id, claim_id, decision)
        return redirect(_ret(f"/scripts/{h}/{version_id}", "reviewed"))
    except ServiceError as e:
        if e.http_status == 409:
            return redirect(_ret(f"/scripts/{h}/{version_id}", "conflict"))
        if e.http_status == 401:
            return redirect("/login")
        abort(e.http_status)


@app.post("/scripts/<h>/versions/<version_id>/blocks/<block_key>/regen-image")
def d_regen(h, version_id, block_key):
    from services import images as _svc
    from services.exceptions import ServiceError
    from store.db import make_engine
    feedback = (request.form.get("feedback") or "").strip()
    try:
        _svc.regenerate_scene(make_engine(), _dash_ctx(h), block_key, feedback, version_id=version_id, topic=request.args.get("topic"))
        return redirect(_ret(f"/scripts/{h}/{version_id}", "regen", f"#img_{block_key}"))
    except ServiceError as e:
        if e.http_status == 401:
            return redirect("/login")
        return redirect(_ret(f"/scripts/{h}/{version_id}", "regenfail"))
    except Exception:
        return redirect(_ret(f"/scripts/{h}/{version_id}", "regenfail"))


@app.post("/scripts/<h>/versions/<version_id>/blocks/<block_key>/revert-image")
def d_revert(h, version_id, block_key):
    from services import images as _svc
    from services.exceptions import ServiceError
    from store.db import make_engine
    seq = request.form.get("seq")
    try:
        seq = int(seq) if seq else None
    except (TypeError, ValueError):
        seq = None
    try:
        _svc.revert_scene(make_engine(), _dash_ctx(h), block_key, seq=seq, version_id=version_id, topic=request.args.get("topic"))   # topic 스코프
        return redirect(_ret(f"/scripts/{h}/{version_id}", "reverted", f"#img_{block_key}"))
    except ServiceError as e:
        if e.http_status == 401:
            return redirect("/login")
        return redirect(_ret(f"/scripts/{h}/{version_id}", "regenfail"))
    except Exception:
        return redirect(_ret(f"/scripts/{h}/{version_id}", "regenfail"))


@app.post("/scripts/<h>/versions/<version_id>/blocks/<block_key>/upload-image")
def d_upload(h, version_id, block_key):
    from services import images as _svc
    from services.exceptions import ServiceError
    from store.db import make_engine
    f = request.files.get("photo")
    raw = f.read() if f else b""
    try:
        _svc.upload_scene(make_engine(), _dash_ctx(h), block_key, raw, version_id=version_id, topic=request.args.get("topic"))
        return redirect(_ret(f"/scripts/{h}/{version_id}", "uploaded", f"#img_{block_key}"))
    except ServiceError as e:
        if e.http_status == 401:
            return redirect("/login")
        return redirect(_ret(f"/scripts/{h}/{version_id}", "regenfail"))
    except Exception:
        return redirect(_ret(f"/scripts/{h}/{version_id}", "regenfail"))


@app.get("/scripts/<h>/imgv/<block_key>/<int:seq>")
def d_imgv(h, block_key, seq):
    """이전(히스토리) 장면 이미지 서빙 — 갤러리에서 나란히 보여주기용."""
    from sqlalchemy import text as _t
    from store.repositories import tenant_conn
    from store.db import make_engine
    try:
        ctx = _dash_ctx(h)
    except Exception:
        abort(403)
    topic = request.args.get("topic")
    with tenant_conn(make_engine(), ctx.hospital_id, membership_id=ctx.membership_id) as cn:
        row = cn.execute(_t("select mime, data from scene_image_versions "
                            "where hospital_id=:h and block_key=:k and seq=:s and (cast(:t as text) is null or topic=cast(:t as text)) limit 1"),
                         {"h": ctx.hospital_id, "k": block_key, "s": seq, "t": topic}).first()
    if not row:
        abort(404)
    return Response(bytes(row.data), mimetype=row.mime or "image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/scripts/<h>/img/<block_key>")
def d_img(h, block_key):
    from sqlalchemy import text as _t
    from store.repositories import tenant_conn
    from store.db import make_engine
    try:
        ctx = _dash_ctx(h)
    except Exception:
        abort(403)
    topic = request.args.get("topic")
    with tenant_conn(make_engine(), ctx.hospital_id, membership_id=ctx.membership_id) as cn:
        row = cn.execute(_t("select mime, data from scene_images "
                            "where hospital_id=:h and block_key=:k and (cast(:t as text) is null or topic=cast(:t as text)) limit 1"),
                         {"h": ctx.hospital_id, "k": block_key, "t": topic}).first()
    if not row:
        abort(404)
    return Response(bytes(row.data), mimetype=row.mime or "image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/scripts/<h>/<script_id>/versions/<version_id>/export")
def d_export(h, script_id, version_id):
    from services import exports as _svc
    from services.exceptions import ServiceError
    from store.db import make_engine
    try:
        return jsonify(_svc.prepare_export(make_engine(), _dash_ctx(h), script_id, version_id)), 200
    except ServiceError as e:
        return jsonify(error=e.code, detail=str(e)), e.http_status

def _run_pipeline(h, topic, evidence=True, request_key=None, membership_id=None):
    """GPT P0 반영: run.py '전에' PG generation_job(pending) 생성 → generating → generated →
    ingesting → completed. 각 상태는 별도 트랜잭션이라 실패해도 job에 흔적이 남음.
    membership_id=생성 요청자(작성자) — ai version의 created_by로 결착(provenance)."""
    import uuid as _uuid, json as _json
    from store.db import make_engine
    from store import ingest as _ing
    job_set(h, topic=topic, status="running", ok=None, log="")
    log = ""; ok = False
    hid = _require_pg(h)
    if _pg_required() and not hid:   # PG 단일원본 배포인데 병원이 PG에 없음 → 신규 생성 차단(단일원본 원칙)
        job_set(h, status="done", ok=False,
                log="[생성 차단] 이 병원이 PostgreSQL에 등록되지 않았습니다. 자료·작업 보존을 위해 관리자에게 병원 재등록을 요청하세요.")
        return
    request_key = request_key or _uuid.uuid4().hex
    job_id = None; worker_token = _uuid.uuid4().hex
    if hid:
        try:
            from store.materials import snapshot_job_materials, materialize_job_snapshot
            # 1) job 생성(pending)
            cj = _ing.create_job(make_engine(), hid, topic, request_key,
                                 target_script_id=_pg_script_id_for_topic(hid, topic),
                                 membership_id=membership_id)
            job_id = cj["job_id"]
            if cj["reused"] and cj["status"] in ("pending", "generating", "ingesting"):
                job_set(h, status="done", ok=False, log="[중복 요청] 이미 진행 중인 생성이 있습니다."); return
            # 2) pending에서 자료 스냅샷 봉인(FOR UPDATE 잠금 + material_snapshot_at) — 이후 seal이 변경 차단
            snapshot_job_materials(make_engine(), hid, job_id)
            # 3) 실행권 원자적 획득(pending&봉인완료→generating, worker_token). 병원당 active 1개 강제.
            acquired, reason = _ing.claim_job(make_engine(), hid, job_id, worker_token)
            if not acquired:
                msg = ("[생성 중] 이 병원에서 다른 대본을 생성 중입니다. 완료 후 다시 실행해 주세요."
                       if reason == "hospital_busy" else "[중복 요청] 이미 진행 중인 생성이 있습니다.")
                job_set(h, status="done", ok=False, log=msg); return
            # 4) 스냅샷된 '정확한 원본'을 raw/로 복원(checksum 검증). stale 제거 후 기록.
            _clear_raw_dir(data_dir(h, "raw"))
            materialize_job_snapshot(make_engine(), hid, job_id, data_dir(h, "raw"))
        except Exception as e:
            log += f"\n[스튜디오 job 경고] {e}"
            if job_id:   # 스냅샷/복원 실패 시 job을 failed로(정합 깨진 상태로 생성 안 함)
                try: _ing.mark_job(make_engine(), hid, job_id, "failed",
                                   allowed_from={"pending","generating"}, error_code="snapshot_error",
                                   error_message=str(e), finished=True)
                except Exception: pass
                job_set(h, status="done", ok=False, log=log); return
    _hb_stop = threading.Event()
    try:
        cmd = [PY, "run.py", "all", "--hospital", h, "--topic", topic]
        if evidence: cmd.append("--evidence")   # 논문 근거 대조 + 시각자료 추출
        # PYTHONUNBUFFERED: run.py의 print가 버퍼에 갇히지 않고 즉시 흘러나옴(진행 로그·heartbeat 실시간)
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
        # 타이머 heartbeat — stdout이 없어도(긴 LLM 호출) 25초마다 살아있음 표시 → reap 오판·'멈춘 것처럼 보임' 방지
        # + 자가 킵얼라이브: 생성 도는 동안 ~4분마다 자기 URL을 쳐서 Render 무료 idle sleep을 막음(누구 생성이든 자동).
        _self_url = (os.environ.get("SELF_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
        def _hb_loop():
            i = 0
            while not _hb_stop.wait(25):
                i += 1
                if hid and job_id:
                    try: _ing.heartbeat_job(make_engine(), hid, job_id, worker_token)
                    except Exception: pass
                if _self_url and i % 10 == 0:      # ~250초마다 자가 핑
                    try:
                        import urllib.request as _u
                        _u.urlopen(_self_url + "/login", timeout=10).read(1)
                    except Exception: pass
        if hid and job_id:
            threading.Thread(target=_hb_loop, daemon=True).start()
        for line in io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="ignore"):
            log += line
            job_set(h, log=log)
        proc.wait()
        ok = (proc.returncode == 0)   # run.py all 이 검수 실패/오류 시 non-zero 반환
        _hb_stop.set()
        try:      # 이번 생성 API 원가 합산([COST] 라인) → 로그 + generation_jobs.total_cost_usd(투명 원가·계측)
            _usds = re.findall(r"\[COST\][^\n]*?usd=([0-9.]+)", log)
            if _usds:
                _total = round(sum(float(x) for x in _usds), 4)
                log += f"\n💰 이번 생성 API 비용: ${_total:.2f} (약 ₩{int(_total*1380):,}) · LLM {len(_usds)}콜"
                job_set(h, log=log)
                if hid and job_id:
                    from store.repositories import tenant_conn
                    from sqlalchemy import text as _txt
                    with tenant_conn(make_engine(), hid) as _cn:
                        _cn.execute(_txt("update generation_jobs set total_cost_usd=:c where id=:j and hospital_id=:h"),
                                    {"c": _total, "j": job_id, "h": hid})
        except Exception:
            pass
    except Exception as e:
        _hb_stop.set()
        log += f"\n[오류] {e}"
        if hid and job_id:
            try: _ing.mark_job(make_engine(), hid, job_id, "failed", allowed_from={"pending","generating","generated","ingesting"},
                               worker_token=worker_token, error_code="run_error", error_message=str(e), finished=True)
            except Exception: pass
            try:
                from services.observability import emit, hid as _hid
                emit("generation_failed", hospital=_hid(hid), stage="run")
            except Exception: pass
    if ok and hid and job_id:
        try:
            _ing.mark_job(make_engine(), hid, job_id, "generated", allowed_from={"generating"}, worker_token=worker_token, phase="parsed")
            pkg = _json.load(open(os.path.join(data_dir(h, "out"), f"{topic}_package.json"), encoding="utf-8"))
            _ing.mark_job(make_engine(), hid, job_id, "ingesting", allowed_from={"generated"}, worker_token=worker_token, phase="ingest")
            res = _ing.ingest_content(make_engine(), hid, job_id, pkg.get("script") or [])  # topic·script_id 모두 job에서
            log += f"\n[스튜디오] 편집·근거·이미지 준비 완료(블록 {res['blocks']}·주장 {res['claims']})."
            job_set(h, log=log)
            try:      # 결과물(html·풀패키지) PG 영속 → 재배포로 디스크 날아가도 ③ 결과물 목록·미리보기 유지
                from store.artifacts import save_from_out_dir
                _sv = save_from_out_dir(make_engine(), hid, topic, data_dir(h, "out"))
                log += f"\n[결과물 저장] {'·'.join(_sv) if _sv else '없음'} 영구 저장됨(재배포에도 유지)."
                job_set(h, log=log)
            except Exception as _ae:
                log += f"\n[결과물 저장 경고] {_ae}"; job_set(h, log=log)
            try:                            # 성공 이벤트(GPT): 병원 해시·블록/주장 수만(내용 미포함)
                from services.observability import emit, hid as _hid
                emit("generation_completed", hospital=_hid(hid), blocks=res.get("blocks"), claims=res.get("claims"))
            except Exception: pass
        except Exception as e:
            log += f"\n[스튜디오 적재 오류] {e}"
            # completed는 덮지 않음(늦은 예외 방지)
            try: _ing.mark_job(make_engine(), hid, job_id, "failed", allowed_from={"pending","generating","generated","ingesting"},
                               error_code="ingest_error", error_message=str(e), finished=True)
            except Exception: pass
            try:
                from services.observability import emit, hid as _hid
                emit("generation_failed", hospital=_hid(hid), stage="ingest")
            except Exception: pass
    elif ok and not hid:
        log += "\n[안내] 이 병원은 스튜디오(PostgreSQL) 연결이 없어 파일만 생성했습니다."
    job_set(h, status="done", ok=ok, log=log)

@app.route("/h/<h>/run", methods=["POST"])
def run_ep(h):
    if not os.path.exists(cfg_path(h)): abort(404)
    topic = (request.form.get("topic","").strip() or "주제")[:60]
    evidence = bool(request.form.get("evidence"))
    import uuid as _uuid
    reqkey = (request.form.get("reqkey", "").strip() or str(_uuid.uuid4()))   # 브라우저 UUID 우선(더블클릭 방지)
    hid = _pg_hospital_id(h)
    if hid:      # 크래시로 멈춘 job은 stale 처리 → 새 생성이 영원히 막히지 않게
        try:
            from store.db import make_engine
            from store.ingest import reap_stale
            reap_stale(make_engine(), hid)
        except Exception:
            pass
    req_mid = _pg_membership_id(hid)   # 생성 요청자(작성자) — 스레드 전 요청 컨텍스트에서 해석
    if not job_get(h).get("running"):
        threading.Thread(target=_run_pipeline, args=(h, topic, evidence, reqkey, req_mid), daemon=True).start()
    return redirect(f"/h/{h}")

@app.route("/h/<h>/status")
def status(h):
    return jsonify(job_get(h))

@app.route("/h/<h>/view/<path:fn>")
def view(h, fn):
    p = os.path.join(data_dir(h,"out"), os.path.basename(fn))
    if not os.path.exists(p):      # 재배포로 디스크 비었으면 PG에서 복원 후 서빙
        b = os.path.basename(fn)
        topic = b[:-len("_package.html")] if b.endswith("_package.html") else os.path.splitext(b)[0]
        _restore_out_artifacts(h, topic)
    if not os.path.exists(p): abort(404)
    return send_file(p)

@app.route("/h/<h>/edit/<topic>")
def edit_story(h, topic):
    """예쁜 스토리보드를 '그대로' 동적 재렌더 + 대사 ✏️수정 + 장면 AI사진(다시·이전·업로드).
    논문그림·화면·타임코드 등 비주얼은 package/assets로 유지, 대사는 PG 현재본, 이미지는 PG scene_images."""
    import json as _json
    from render.render import render as _render, _meta
    from services.context import ActorContext
    from services.exceptions import ServiceError
    from store.db import make_engine
    from store.repositories import tenant_conn
    from sqlalchemy import text as _t
    base = os.path.join(data_dir(h, "out"), f"{os.path.basename(topic)}_package")
    if not os.path.exists(base + ".json"):
        _restore_out_artifacts(h, topic)      # 재배포로 디스크 비었으면 PG에서 복원
    if not os.path.exists(base + ".json"):
        abort(404)
    pkg = _json.load(open(base + ".json", encoding="utf-8"))
    def _ld(suf, key=None):
        p = base + suf
        if not os.path.exists(p):
            return None
        try:
            d = _json.load(open(p, encoding="utf-8")); return d.get(key) if key else d
        except Exception:
            return None
    evidence = _ld(".evidence.json", "results"); images = _ld(".assets.json")
    # PG 계정 아니면(또는 병원 미연결) 예쁜 정적 미리보기로 안내
    try:
        eng = make_engine()
        ctx = ActorContext.resolve(eng, session.get("user_id"), h, getattr(g, "request_id", None))
    except ServiceError:
        # 편집 권한(PG 계정) 없어도 '결과 보기'는 항상 예쁜 스토리보드로 보이게(정적 렌더). 편집 컨트롤만 빠짐.
        return _render(pkg, _meta(h), evidence=evidence, images=images)
    _topic = os.path.basename(topic)      # 이 페이지가 편집하는 '주제' — 이미지·버전을 이 주제로 스코프
    from urllib.parse import quote as _q
    _tq = _q(_topic)
    with tenant_conn(eng, ctx.hospital_id) as cn:
        # 이 '주제'의 현재 버전(다른 주제 최신본이 섞이지 않게 topic 필터)
        row = cn.execute(_t("select sv.id vid, sv.script_id sid from script_versions sv join scripts s "
                            "on s.id=sv.script_id where sv.hospital_id=:h and s.topic=:tp and s.current_version_id=sv.id "
                            "order by sv.created_at desc limit 1"), {"h": ctx.hospital_id, "tp": _topic}).first()
        if not row:
            return _render(pkg, _meta(h), evidence=evidence, images=images)
        vid, sid = row.vid, row.sid
        blocks = cn.execute(_t("select order_index, stable_block_key, text from script_blocks "
                               "where hospital_id=:h and version_id=:v order by order_index"),
                            {"h": ctx.hospital_id, "v": vid}).all()
        imgkeys = {r[0] for r in cn.execute(_t("select block_key from scene_images where hospital_id=:h and topic=:tp"),
                                            {"h": ctx.hospital_id, "tp": _topic})}
        # 갤러리: 블록별 이전 이미지 seq 목록(오래된→최신). 이 주제의 이력만.
        hist_by_key = {}
        for r in cn.execute(_t("select block_key, seq from scene_image_versions where hospital_id=:h and topic=:tp order by block_key, seq"),
                            {"h": ctx.hospital_id, "tp": _topic}):
            hist_by_key.setdefault(r[0], []).append(r[1])
    csrf = f'<input type="hidden" name="_csrf" value="{session.get("_csrf","")}">'
    rt = f'<input type="hidden" name="return_to" value="/h/{h}/edit/{_tq}">'
    vid_s = str(vid)
    edit = {"by_idx": {b.order_index: {"key": b.stable_block_key, "text": b.text} for b in blocks},
            "csrf": csrf, "rt": rt, "version_id": vid_s,
            "edit_url": f"/scripts/{h}/{sid}/edit",
            "img_url": (lambda k: f"/scripts/{h}/img/{k}?topic={_tq}"),
            "has_img": (lambda k: k in imgkeys),
            "hist": (lambda k: hist_by_key.get(k, [])),   # 이전 이미지 seq 목록(갤러리)
            "imgv_url": (lambda k, s: f"/scripts/{h}/imgv/{k}/{s}?topic={_tq}"),
            "regen_url": (lambda k: f"/scripts/{h}/versions/{vid_s}/blocks/{k}/regen-image?topic={_tq}"),
            "revert_url": (lambda k: f"/scripts/{h}/versions/{vid_s}/blocks/{k}/revert-image?topic={_tq}"),
            "upload_url": (lambda k: f"/scripts/{h}/versions/{vid_s}/blocks/{k}/upload-image?topic={_tq}")}
    return _render(pkg, _meta(h), evidence=evidence, images=images, edit=edit)

LOGIN = """<!doctype html><html lang=ko><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>로그인 · 병원 유튜브 대본 생성기</title><style>{{css}}</style></head><body>
<div class=auth><div class=authcard>
  <div class=logo>{{ logo|safe }}</div>
  <h1>로그인</h1><p class=s>Medical Pipeline · 병원 유튜브 대본 생성기</p>
  {{ err_html|safe }}
  <form method=post>
    <label>아이디</label><input type=text name=username placeholder="아이디" required autofocus>
    <label>비밀번호</label><input type=password name=password placeholder="비밀번호" required>
    <button class="btn pri" type=submit>로그인</button>
  </form>
</div></div></body></html>"""

def _login_page(err=""):
    err_html = f'<div class=err>{err}</div>' if err else ""
    logo = (f'<img src="{ICON_URI}" style="width:64px;height:64px" alt="Medical Pipeline">' if ICON_URI
            else '<span class=dot style="width:44px;height:44px;border-radius:13px;font-size:23px">본</span>')
    return render_template_string(LOGIN, css=CSS, err_html=err_html, logo=logo)

def _pg_login(email, pw):
    """스튜디오와 동일한 PostgreSQL 사용자로 인증(이메일/비번). 성공 시 PG user_id(str) 반환.
    DATABASE_URL 미설정/오류면 None → 레거시 users.yaml로 폴백."""
    try:
        from store.db import make_engine
        from sqlalchemy import text
        with make_engine().connect() as cn:
            row = cn.execute(text("select id, pw_hash from lookup_user_for_login(:e)"), {"e": email}).first()
        if row and row.pw_hash and check_password_hash(row.pw_hash, pw):
            return str(row.id)
    except Exception:
        pass
    return None

@app.route("/login", methods=["GET","POST"])
def login():
    if session.get("user"): return redirect("/")
    err = ""
    load_users()  # 첫 실행 시 기본 계정 생성(콘솔에 안내 출력)
    if request.method == "POST":
        u = request.form.get("username","").strip(); p = request.form.get("password","")
        # 1) PostgreSQL 사용자(스튜디오와 단일 계정) — 한 번 로그인으로 대시보드+스튜디오
        pg_uid = _pg_login(u, p)
        if pg_uid:
            session.clear()   # 세션 고정 공격 방지
            session["user"] = u; session["user_id"] = pg_uid; return redirect("/")
        # 2) 레거시 users.yaml 폴백 — DISABLE_YAML_FALLBACK=1 이면 비활성(PG 단일화 후 제거 예정)
        if os.environ.get("DISABLE_YAML_FALLBACK") != "1":
            users = load_users()
            if u in users and check_password_hash(users[u], p):
                session.clear(); session["user"] = u; return redirect("/")
        err = "아이디 또는 비밀번호가 올바르지 않습니다."
    return _login_page(err)

# 공개 회원가입 없음. 팀원 계정은 관리자가 config/users.yaml 에 직접 추가하거나
# ADMIN_PW 환경변수로 관리(비밀번호는 해시로만 저장).

@app.route("/logout")
def logout():
    session.clear(); return redirect("/login")

# ── 스튜디오(PostgreSQL 기반 편집·버전·승인 UI)를 /studio 에 마운트 ──
# 예전 sqlite 대시보드(위 라우트)는 그대로 두고, 새 store/ 시스템(web/api.py)을 같은 프로세스에서 서빙.
# DATABASE_URL(app_rw) 미설정이어도 import·마운트는 안전(엔진 생성은 지연, 실제 접속은 /studio 라우트 진입 시).
# 마운트가 실패해도 기존 대시보드는 계속 동작하도록 try/except로 감싼다.
try:
    from werkzeug.middleware.dispatcher import DispatcherMiddleware
    from web.api import create_app as _create_studio
    _studio_app = _create_studio()
    app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/studio": _studio_app})
    print("[studio] /studio 에 편집·승인 UI 마운트 완료")
except Exception as _e:  # pragma: no cover - 배포 안전장치
    print(f"[studio] 마운트 실패(기존 대시보드는 정상 동작): {_e!r}")

# ── 죽은 생성 job 자동 정리(배포·재시작·hung으로 죽은 subprocess가 'generating'으로 방치되는 것 방지) ──
#   타이머 heartbeat(25s)가 있어 'heartbeat 멈춤=서버/워커 죽음'이 확실 → 부팅 즉시 + 주기적으로 stale 회수.
#   회수 시 error_message를 남겨 화면(poll)이 '중단됨, 다시 생성'을 보여준다.
def _reaper_loop():
    import time as _t
    from store.db import make_engine
    from store.ingest import reap_stale_all
    try:
        n = reap_stale_all(make_engine(), 10)     # 부팅 직후: 재시작으로 죽은(orphan) active job 즉시 정리
        if n: print(f"[reaper] 부팅 정리: 중단된 생성 {n}건 정리")
    except Exception:
        pass
    while True:
        _t.sleep(90)
        try:
            reap_stale_all(make_engine(), 240)    # 주기: heartbeat 4분↑ 멈춘 job = 죽음
        except Exception:
            pass

import sys as _sys
if "pytest" not in _sys.modules:                  # 테스트 중엔 미실행(테스트 job 오정리 방지)
    try:
        threading.Thread(target=_reaper_loop, daemon=True).start()
        print("[reaper] 죽은 생성 job 자동 정리 스레드 시작(부팅 즉시+주기)")
    except Exception as _e:
        print(f"[reaper] 시작 실패(무시): {_e!r}")

# ═══════════════════════════════════════════════════════════════════
# 유튜브 벤치마킹 UI (C10) — 모든 라우트는 services.benchmark 만 호출(직접 SQL·상태전이 금지).
# 단계: 프로젝트 → 영상등록 → (메타) → 자막 → 분석 → 교차종합 → 주장후보 → 기획승인 → 브리핑 → 유사도.
# ═══════════════════════════════════════════════════════════════════
_BM_MSG = {
    "created": "프로젝트를 만들었어요.", "video_added": "영상을 추가했어요.",
    "meta": "메타데이터를 가져왔어요.", "no_api": "YouTube API 키가 없어 자동 검색/메타를 건너뛰었어요. URL을 직접 넣어 주세요.",
    "found": "주제로 인기 영상을 찾아 등록했어요.", "notfound": "관련 영상을 못 찾았어요. 다른 주제어로 시도해 보세요.",
    "transcript": "자막을 저장했어요.", "manual_required": "외부 자막을 못 가져왔어요 — 자막을 직접 붙여넣어 주세요.",
    "analyzed": "영상 분석을 마쳤어요.", "synth": "교차 종합을 마쳤어요.",
    "claims": "검증 대상 주장을 정리했어요(전부 '검증 전' 상태).", "plan": "기획안 초안을 만들었어요.",
    "approved": "기획안을 승인했어요.", "rejected": "기획안을 반려했어요.",
    "sim": "유사도 검사를 마쳤어요.",
    "e403": "권한이 없어요(승인은 승인자/관리자만).", "e409": "지금 상태에서는 처리할 수 없어요.",
    "e400": "입력을 확인해 주세요.", "err": "처리 중 문제가 발생했어요.",
}

def _bm_run(h, call, ok_key, redirect_path):
    """service 호출 공통 래퍼: 성공→ok_key, ServiceError→상태별 메시지로 redirect."""
    from services.exceptions import ServiceError
    try:
        res = call()
        key = ok_key(res) if callable(ok_key) else ok_key
        return redirect(f"{redirect_path}?m={key}")
    except ServiceError as e:
        if e.http_status == 401:
            return redirect("/login")
        m = {403: "e403", 409: "e409", 404: "e400", 400: "e400", 422: "e400"}.get(e.http_status, "err")
        return redirect(f"{redirect_path}?m={m}")

def _bm_badge(status):
    c = {"draft": "#94a3b8", "analyzing": "#f59e0b", "planned": "#6366f1",
         "scripted": "#16a34a", "approved": "#16a34a", "rejected": "#dc2626",
         "pending_verification": "#f59e0b", "available": "#16a34a",
         "manual_required": "#dc2626", "low": "#16a34a", "medium": "#f59e0b", "high": "#dc2626"}.get(status, "#94a3b8")
    return f'<span style="background:{c};color:#fff;font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px">{_esc(status)}</span>'

try:
    with open(os.path.join(ROOT, "web", "guide_benchmark.html"), encoding="utf-8") as _gf:
        _BM_GUIDE_HTML = _gf.read()
except Exception:
    _BM_GUIDE_HTML = "<!doctype html><meta charset=utf-8><h1>가이드를 불러오지 못했습니다.</h1>"

@app.get("/guide/benchmark")
def bm_guide():
    """유튜브 벤치마킹 사용 가이드(광고주·운영자 겸용). 로그인 없이 새 창으로 열림."""
    return Response(_BM_GUIDE_HTML, mimetype="text/html; charset=utf-8")

@app.get("/h/<h>/benchmark")
def bm_home(h):
    from services import benchmark as bm
    from services.exceptions import ServiceError
    from store.db import make_engine
    try:
        ctx = _dash_ctx(h)
        projects = bm.list_projects(make_engine(), ctx)
    except ServiceError as e:
        return redirect("/login") if e.http_status == 401 else abort(e.http_status)
    _csrf = session.get("_csrf", "")
    m = request.args.get("m"); note = f'<div class=note>{_esc(_BM_MSG.get(m, ""))}</div>' if m in _BM_MSG else ""
    rows = "".join(
        f'<a class=hcard href="/h/{_esc(h)}/benchmark/{p["project_id"]}">'
        f'<div class=n>{_esc(p["title"])} {_bm_badge(p["status"])}</div>'
        f'<div class=i>영상 {p["videos"]}개 · {_esc(p["created_at"][:10])}</div></a>'
        for p in projects) or '<div class=muted>아직 벤치마킹 프로젝트가 없어요.</div>'
    body = f"""
    <div class=row style="margin-bottom:8px"><a class="btn ghost" href="/h/{_esc(h)}">← 대시보드</a></div>
    <div class=hero><h1>유튜브 벤치마킹</h1>
      <p>잘나가는 채널 영상을 등록·분석해 '흥행 공식'을 뽑고, 우리 기획안으로 이어가요.
      의학 주장은 여기서 사실로 확정하지 않고 '검증 대상'으로만 모읍니다.</p></div>
    {note}
    <div class=card><h2>+ 새 벤치마킹 프로젝트</h2>
      <form method=post action="/h/{_esc(h)}/benchmark/new"><input type=hidden name=_csrf value="{_csrf}">
        <label>프로젝트 제목</label><input type=text name=title placeholder="예: 이명 벤치마킹 3편" required>
        <div class=row style="margin-top:14px"><button class="btn pri" type=submit>만들기</button></div>
      </form></div>
    <div class=hlist style="margin-top:16px">{rows}</div>"""
    return page("유튜브 벤치마킹", body)

@app.post("/h/<h>/benchmark/new")
def bm_new(h):
    from services import benchmark as bm
    from store.db import make_engine
    title = request.form.get("title", "")
    return _bm_run(h, lambda: bm.create_project(make_engine(), _dash_ctx(h), title),
                   lambda r: "created", f"/h/{h}/benchmark") \
        if title.strip() else redirect(f"/h/{h}/benchmark?m=e400")

@app.get("/h/<h>/benchmark/<pid>")
def bm_project(h, pid):
    from services import benchmark as bm
    from services.exceptions import ServiceError
    from store.db import make_engine
    try:
        ctx = _dash_ctx(h); eng = make_engine()
        proj = bm.get_project(eng, ctx, pid)
        analyses = bm.list_analyses(eng, ctx, pid)
        syn = bm.get_latest_synthesis(eng, ctx, pid)
        claims = bm.list_claim_candidates(eng, ctx, pid)
        plans = bm.list_plans(eng, ctx, pid)
        scripts = bm.list_recent_scripts(eng, ctx)
        sim = bm.latest_similarity_report(eng, ctx, pid)
    except ServiceError as e:
        return redirect("/login") if e.http_status == 401 else abort(e.http_status)
    _csrf = session.get("_csrf", "")
    base = f"/h/{_esc(h)}/benchmark/{_esc(pid)}"
    m = request.args.get("m"); note = f'<div class=note>{_esc(_BM_MSG.get(m, ""))}</div>' if m in _BM_MSG else ""
    analyzed_refs = {a["video_ref"] for a in analyses}

    # 1) 영상들
    vrows = ""
    for v in proj["videos"]:
        ts = v["transcript_status"]; done = v["video_ref"] in analyzed_refs
        tstat = _bm_badge(ts) if ts else '<span class=muted>자막 없음</span>'
        meta = (f'조회수 {v["view_count"]:,}' if v["view_count"] else "메타 미수집")
        vrows += f"""<div class=card style="padding:14px">
          <div class=row><b>{_esc(v["title"] or v["url"])}</b> {tstat} {"✅분석완료" if done else ""}</div>
          <div class=muted style="margin:4px 0 8px">{_esc(v["url"])} · {_esc(meta)} · {_esc(v["caption_status"] or "")}</div>
          <form method=post action="{base}/video/{v["video_ref"]}/transcript" enctype=multipart/form-data style="margin-bottom:6px">
            <input type=hidden name=_csrf value="{_csrf}">
            <textarea name=pasted rows=2 placeholder="자막 붙여넣기(외부 수집 실패 시)" style="width:100%;font-size:13px"></textarea>
            <div class=row style="margin-top:6px">
              <input type=file name=file accept=".srt,.vtt,.txt" style="font-size:12px">
              <label class=muted style="font-size:12px"><input type=checkbox name=try_external value=1> 외부 자동수집 시도</label>
              <button class="btn ghost" type=submit>자막 저장</button></div>
          </form>
          <form method=post action="{base}/video/{v["video_ref"]}/analyze" style="display:inline">
            <input type=hidden name=_csrf value="{_csrf}">
            <button class="btn" type=submit {"disabled" if not ts=="available" else ""}>이 영상 분석{"(자막 필요)" if ts!="available" else ""}</button>
          </form></div>"""

    # 2) 종합 결과
    syn_html = '<div class=muted>아직 없음 — 영상 1개 이상 분석 후 종합하세요.</div>'
    if syn:
        s = syn["synthesis"] or {}
        def _lst(x): return "".join(f"<li>{_esc(i)}</li>" for i in (x or [])[:8])
        vf = s.get("virality_formula") or {}
        syn_html = f"""<div class=note>
          <b>흥행 공식</b><br>훅: {_esc(vf.get('hook',''))} · 구성: {_esc(vf.get('structure',''))} · 화법: {_esc(vf.get('narration',''))}
          <br><b>공통 패턴</b><ul>{_lst(s.get('common_patterns'))}</ul>
          <b>차별화 기회(gaps)</b><ul>{_lst(s.get('content_gaps'))}</ul>
          <b>복제 금지 표현</b><ul>{_lst(s.get('forbidden_expressions'))}</ul></div>"""

    # 3) 주장 후보
    claim_rows = "".join(
        f"<tr><td>{_esc(c['claim_text'])}</td><td>{_esc(c['claim_type'] or '')}</td><td>{_bm_badge(c['status'])}</td></tr>"
        for c in claims) or '<tr><td colspan=3 class=muted>없음</td></tr>'

    # 4) 기획안들
    plan_rows = ""
    for pl in plans:
        approve = f"""<form method=post action="{base}/plan/{pl['plan_id']}/approve" style="display:inline">
            <input type=hidden name=_csrf value="{_csrf}"><button class="btn" type=submit>승인</button></form>
          <form method=post action="{base}/plan/{pl['plan_id']}/reject" style="display:inline">
            <input type=hidden name=_csrf value="{_csrf}"><button class="btn ghost" type=submit>반려</button></form>""" if pl["status"] == "draft" else ""
        brief = f' · <a href="{base}/plan/{pl["plan_id"]}/brief">생성 브리핑 보기</a>' if pl["status"] == "approved" else ""
        plan_rows += f'<div class=row style="margin:4px 0"><a href="{base}/plan/{pl["plan_id"]}">기획안</a> {_bm_badge(pl["status"])} {approve}{brief}</div>'
    plan_rows = plan_rows or '<div class=muted>아직 없음</div>'

    # ④ 유사도: 최근 결과 표시 + 생성 대본 자동 선택 검사(붙여넣기 불필요)
    _risk_ko = {"low": "낮음 · 안전", "medium": "주의", "high": "높음 · 수정 필요"}
    if sim:
        _rep = sim.get("report") or {}
        _worst = (_rep.get("verbatim") or {}).get("worst") or {}
        _fl = _rep.get("flagged") or []
        _sem = (f' · 의미유사 {_rep.get("semantic_score")}' if _rep.get("llm_used") else '')
        _flnote = ('<br>겹침 지적: ' + _esc("; ".join((f.get("why") or "") for f in _fl[:3]))) if _fl else ''
        sim_html = (f'<div class=note>최근 검사 결과: {_bm_badge(sim["risk"])} <b>{_risk_ko.get(sim["risk"], sim["risk"])}</b>'
                    f' · 최대 연속일치 {_worst.get("longest_run_words", 0)}단어 · 원본 {_rep.get("source_count", 0)}편{_sem}{_flnote}</div>')
    else:
        sim_html = '<div class=muted>아직 검사하지 않았어요. 아래에서 대본을 골라 검사하세요.</div>'
    if scripts:
        _opts = "".join(
            f'<option value="{s["version_id"]}">{_esc(s["topic"] or "대본")} v{s["version_no"]}'
            f'{" · 현재본" if s["is_current"] else ""}</option>' for s in scripts)
        simform = (f'<form method=post action="{base}/similarity-version" class=row style="margin-top:10px">'
                   f'<input type=hidden name=_csrf value="{_csrf}">'
                   f'<select name=version_id style="flex:1;padding:8px;border-radius:9px;border:1px solid var(--border)">{_opts}</select>'
                   f'<label class=muted style="font-size:12px"><input type=checkbox name=use_llm value=1> 심층검사</label>'
                   f'<button class="btn pri" type=submit>이 대본으로 검사</button></form>')
    else:
        simform = '<div class=muted style="margin-top:10px">아직 생성된 대본이 없어요 — 대본을 만든 뒤 여기서 자동 검사할 수 있어요.</div>'

    body = f"""
    <div class=row style="margin-bottom:8px"><a class="btn ghost" href="/h/{_esc(h)}/benchmark">← 프로젝트 목록</a></div>
    <div class=hero><h1>{_esc(proj["title"])} {_bm_badge(proj["status"])}</h1></div>
    {note}
    <div class=card><h2>① 영상 등록 · 자막 · 분석</h2>
      <form method=post action="{base}/search-videos" class=row style="margin-bottom:10px"><input type=hidden name=_csrf value="{_csrf}">
        <input type=text name=topic placeholder="주제로 인기 영상 자동 찾기 (예: 이명 치료, 자율신경실조증)" style="flex:1" required>
        <select name=count style="padding:8px;border-radius:9px;border:1px solid var(--border)">
          <option value=1>1개</option><option value=2>2개</option><option value=3 selected>3개</option><option value=5>5개</option></select>
        <button class="btn pri" type=submit>🔎 인기 영상 자동 찾기</button></form>
      <div class=muted style="font-size:12px;margin:-4px 0 10px">주제를 넣으면 조회수 높은 관련 영상을 골라 자동 등록해요. 또는 아래에 URL을 직접 넣어도 됩니다.</div>
      <form method=post action="{base}/add-video" class=row><input type=hidden name=_csrf value="{_csrf}">
        <input type=text name=url placeholder="유튜브 URL 직접 입력" style="flex:1" required>
        <button class="btn ghost" type=submit>영상 추가</button></form>
      <form method=post action="{base}/metadata" style="margin-top:8px"><input type=hidden name=_csrf value="{_csrf}">
        <button class="btn ghost" type=submit>📊 메타데이터 다시 가져오기</button></form>
      <div style="margin-top:12px">{vrows}</div></div>
    <div class=card><h2>② 교차 종합 <span class=muted>(분석 {len(analyses)}개)</span></h2>
      <form method=post action="{base}/synthesize"><input type=hidden name=_csrf value="{_csrf}">
        <button class="btn pri" type=submit>교차 종합 실행</button></form>
      <div style="margin-top:10px">{syn_html}</div>
      <form method=post action="{base}/claims" style="margin-top:8px"><input type=hidden name=_csrf value="{_csrf}">
        <button class="btn ghost" type=submit>검증 대상 주장 정리</button></form>
      <table style="margin-top:8px;width:100%;font-size:13px"><tr><th align=left>주장(검증 전)</th><th>유형</th><th>상태</th></tr>{claim_rows}</table></div>
    <div class=card><h2>③ 기획안 · 승인</h2>
      <form method=post action="{base}/plan"><input type=hidden name=_csrf value="{_csrf}">
        <button class="btn pri" type=submit>기획안 생성</button></form>
      <div style="margin-top:10px">{plan_rows}</div></div>
    <div class=card><h2>④ 원본 유사도 검사(표절 방지)</h2>
      {sim_html}
      {simform}
      <details style="margin-top:10px"><summary class=muted style="cursor:pointer;font-size:13px">또는 대본을 직접 붙여넣어 검사</summary>
        <form method=post action="{base}/similarity" style="margin-top:8px"><input type=hidden name=_csrf value="{_csrf}">
          <textarea name=script rows=5 placeholder="완성한 대본을 붙여넣으면 원본 자막과의 축자·의미 유사도를 검사합니다" style="width:100%"></textarea>
          <div class=row style="margin-top:8px">
            <label class=muted style="font-size:12px"><input type=checkbox name=use_llm value=1> 의미/구조까지 LLM 심층검사</label>
            <button class="btn" type=submit>유사도 검사</button></div></form></details></div>"""
    return page(f"벤치마킹 · {proj['title']}", body)

@app.post("/h/<h>/benchmark/<pid>/add-video")
def bm_add_video(h, pid):
    from services import benchmark as bm
    from store.db import make_engine
    url = request.form.get("url", "")
    return _bm_run(h, lambda: bm.add_video(make_engine(), _dash_ctx(h), pid, url),
                   "video_added", f"/h/{h}/benchmark/{pid}")

@app.post("/h/<h>/benchmark/<pid>/search-videos")
def bm_search_videos(h, pid):
    from services import benchmark as bm
    from store.db import make_engine
    topic = request.form.get("topic", ""); count = request.form.get("count", "3")
    def _msg(r):
        if r.get("no_api"): return "no_api"
        return "found" if r.get("added") else "notfound"
    return _bm_run(h, lambda: bm.add_videos_by_topic(make_engine(), _dash_ctx(h), pid, topic, count),
                   _msg, f"/h/{h}/benchmark/{pid}") if topic.strip() \
        else redirect(f"/h/{h}/benchmark/{pid}?m=e400")

@app.post("/h/<h>/benchmark/<pid>/metadata")
def bm_metadata(h, pid):
    from services import benchmark as bm
    from store.db import make_engine
    return _bm_run(h, lambda: bm.fetch_metadata(make_engine(), _dash_ctx(h), pid),
                   lambda r: "no_api" if r.get("no_api") else "meta", f"/h/{h}/benchmark/{pid}")

@app.post("/h/<h>/benchmark/<pid>/video/<vref>/transcript")
def bm_transcript(h, pid, vref):
    from services import benchmark as bm
    from store.db import make_engine
    pasted = request.form.get("pasted") or None
    try_external = bool(request.form.get("try_external"))
    fb = None; fn = None
    f = request.files.get("file")
    if f and f.filename:
        fb = f.read(); fn = f.filename
    return _bm_run(h, lambda: bm.fetch_transcript(make_engine(), _dash_ctx(h), vref,
                   pasted_text=pasted, file_bytes=fb, filename=fn, try_external=try_external),
                   lambda r: "transcript" if r["status"] == "available" else "manual_required",
                   f"/h/{h}/benchmark/{pid}")

@app.post("/h/<h>/benchmark/<pid>/video/<vref>/analyze")
def bm_analyze(h, pid, vref):
    from services import benchmark as bm
    from store.db import make_engine
    return _bm_run(h, lambda: bm.analyze_video(make_engine(), _dash_ctx(h), vref),
                   "analyzed", f"/h/{h}/benchmark/{pid}")

@app.post("/h/<h>/benchmark/<pid>/synthesize")
def bm_synthesize(h, pid):
    from services import benchmark as bm
    from store.db import make_engine
    return _bm_run(h, lambda: bm.synthesize_project(make_engine(), _dash_ctx(h), pid),
                   "synth", f"/h/{h}/benchmark/{pid}")

@app.post("/h/<h>/benchmark/<pid>/claims")
def bm_claims(h, pid):
    from services import benchmark as bm
    from store.db import make_engine
    return _bm_run(h, lambda: bm.extract_claim_candidates(make_engine(), _dash_ctx(h), pid),
                   "claims", f"/h/{h}/benchmark/{pid}")

@app.post("/h/<h>/benchmark/<pid>/plan")
def bm_plan(h, pid):
    from services import benchmark as bm
    from store.db import make_engine
    return _bm_run(h, lambda: bm.generate_plan(make_engine(), _dash_ctx(h), pid),
                   "plan", f"/h/{h}/benchmark/{pid}")

@app.post("/h/<h>/benchmark/<pid>/plan/<plan_id>/approve")
def bm_plan_approve(h, pid, plan_id):
    from services import benchmark as bm
    from store.db import make_engine
    return _bm_run(h, lambda: bm.approve_plan(make_engine(), _dash_ctx(h), plan_id),
                   "approved", f"/h/{h}/benchmark/{pid}")

@app.post("/h/<h>/benchmark/<pid>/plan/<plan_id>/reject")
def bm_plan_reject(h, pid, plan_id):
    from services import benchmark as bm
    from store.db import make_engine
    return _bm_run(h, lambda: bm.reject_plan(make_engine(), _dash_ctx(h), plan_id),
                   "rejected", f"/h/{h}/benchmark/{pid}")

@app.get("/h/<h>/benchmark/<pid>/plan/<plan_id>")
def bm_plan_view(h, pid, plan_id):
    from services import benchmark as bm
    from services.exceptions import ServiceError
    from store.db import make_engine
    import json as _json
    try:
        p = bm.get_plan(make_engine(), _dash_ctx(h), plan_id)
    except ServiceError as e:
        return redirect("/login") if e.http_status == 401 else abort(e.http_status)
    pretty = _esc(_json.dumps(p["plan"], ensure_ascii=False, indent=2))
    body = f"""<div class=row style="margin-bottom:8px"><a class="btn ghost" href="/h/{_esc(h)}/benchmark/{_esc(pid)}">← 프로젝트</a></div>
    <div class=hero><h1>기획안 {_bm_badge(p["status"])}</h1></div>
    <div class=card><pre style="white-space:pre-wrap;font-size:13px;line-height:1.6">{pretty}</pre></div>"""
    return page("기획안", body)

@app.get("/h/<h>/benchmark/<pid>/plan/<plan_id>/brief")
def bm_plan_brief(h, pid, plan_id):
    from services import benchmark as bm
    from services.exceptions import ServiceError
    from store.db import make_engine
    try:
        b = bm.build_generation_brief(make_engine(), _dash_ctx(h), plan_id)
    except ServiceError as e:
        if e.http_status == 401:
            return redirect("/login")
        return redirect(f"/h/{h}/benchmark/{pid}?m=e409")
    body = f"""<div class=row style="margin-bottom:8px"><a class="btn ghost" href="/h/{_esc(h)}/benchmark/{_esc(pid)}">← 프로젝트</a></div>
    <div class=hero><h1>생성 브리핑</h1><p>이 브리핑을 참고해 기존 대본 생성기로 제작하세요(의학 내용은 근거검증을 따릅니다).</p></div>
    <div class=card><div class=muted>주제: {_esc(b["topic"])}</div>
      <pre style="white-space:pre-wrap;font-size:13px;line-height:1.7;margin-top:8px">{_esc(b["brief_text"])}</pre></div>"""
    return page("생성 브리핑", body)

@app.post("/h/<h>/benchmark/<pid>/similarity")
def bm_similarity(h, pid):
    from services import benchmark as bm
    from store.db import make_engine
    script = request.form.get("script", "")
    use_llm = bool(request.form.get("use_llm"))
    gen = None
    if use_llm:
        from llm.runner import generate as _g
        gen = _g
    return _bm_run(h, lambda: bm.check_similarity(make_engine(), _dash_ctx(h), pid, script, generator=gen),
                   lambda r: "sim", f"/h/{h}/benchmark/{pid}")

@app.post("/h/<h>/benchmark/<pid>/similarity-version")
def bm_similarity_version(h, pid):
    from services import benchmark as bm
    from store.db import make_engine
    vid = request.form.get("version_id", ""); use_llm = bool(request.form.get("use_llm"))
    if not vid:
        return redirect(f"/h/{h}/benchmark/{pid}?m=e400")
    return _bm_run(h, lambda: bm.check_similarity_version(make_engine(), _dash_ctx(h), pid, vid, use_llm),
                   "sim", f"/h/{h}/benchmark/{pid}")


if __name__ == "__main__":
    load_users()  # 기본 계정 보장 + 콘솔 안내
    port = int(os.environ.get("PORT", 5000))   # 배포 환경은 PORT 주입, 로컬은 5000
    print(f"브라우저에서 http://localhost:{port} 여세요. (로그인 필요)")
    app.run(host="0.0.0.0", port=port, debug=False)
