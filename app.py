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
    if request.endpoint in ("login","static"): return   # 공개 회원가입 없음(로그인 POST는 세션전이라 면제)
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
    body = f"""
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
    filelist = "".join(f"<li>{f}</li>" for f in raw) or "<li class=muted>아직 업로드된 자료가 없어요.</li>"
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
        _persist = "(영구 저장됨)" if _pg_hospital_id(h) else "(임시 저장 — 이 병원은 영구저장 미연결)"
        misswarn += f'<div class=note style="border-color:var(--good);color:var(--good)">✅ {_ok}개 자료가 업로드됐어요 {_persist}.</div>'
    elif _ok == "0":
        misswarn += '<div class=note style="border-color:var(--danger);color:var(--danger)">⚠️ 업로드된 파일이 없어요. 파일이 선택됐는지, 허용 형식(pdf·docx·txt·zip 등)인지 확인해 주세요.</div>'
    if request.args.get("big") == "1":
        misswarn += '<div class=note style="border-color:var(--warn,#e0a800);color:var(--warn,#b8860b)">ℹ️ 40MB 넘는 파일은 이번 생성엔 쓰이지만 영구저장은 안 돼요(재시작 시 소실). 나머지는 영구 저장됩니다.</div>'
    if request.args.get("err") == "nopg":
        misswarn += '<div class=note style="border-color:var(--danger);color:var(--danger)">⚠️ 이 병원은 영구저장(PostgreSQL)에 등록되지 않아 업로드를 막았어요(임시저장 방지). 관리자에게 병원 재등록을 요청하세요.</div>'
    # ③ 결과물: 목록. disk .html ∪ PG(script_artifacts) → 재배포로 디스크가 비어도 목록 유지.
    def _topic_of(fn):
        b = os.path.basename(fn)
        return b[:-len("_package.html")] if b.endswith("_package.html") else b[:-5]
    _seen = set(); _topics = []
    for _t in [_topic_of(o) for o in outs] + _pg_result_topics(h):
        if _t and _t not in _seen:
            _seen.add(_t); _topics.append(_t)
    def _esc_t(t): return t.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
    outlist = ("".join(f'<div class=out><span>{_esc_t(t)}</span>'
                       f'<a class=btn href="/h/{h}/edit/{_esc_t(t)}">✏️ 편집(대사·사진)</a>'
                       f'<a class="btn g" href="/h/{h}/view/{_esc_t(t)}_package.html" target=_blank>편집 완료된 최종본 미리보기</a></div>'
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
            onsubmit="var b=document.getElementById('upbtn');if(b){{b.disabled=true;b.textContent='⏳ 업로드 중…';}}var m=document.getElementById('upmsg');if(m)m.style.display='inline';"><input type=hidden name=_csrf value="{_csrf}">
        <div class=drop id=drop><span id=drophint>파일을 여기로 끌어다 놓거나 클릭 (pdf·docx·txt·zip)</span>
          <input id=fin type=file name=files multiple style="display:none">
        </div>
        <div id=fsel class=muted style="margin-top:8px;font-size:13px;line-height:1.7"></div>
        <div class=row style="margin-top:12px"><button class="btn pri" id=upbtn type=submit>업로드</button>
        <span id=upmsg class=muted style="display:none;color:var(--accent);font-weight:700">⏳ 파일 올리는 중이에요 — 창을 닫지 마세요(용량 크면 시간이 걸려요).</span>
        <span class=muted>설문지·인터뷰·논문·강의자료·기존 대본 등. zip 통째로도 OK</span></div>
      </form>
      <ul class=files>{filelist}</ul>
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

    <div id=genmodal style="display:none;position:fixed;inset:0;z-index:9999;background:rgba(12,17,28,.6);backdrop-filter:blur(4px);align-items:center;justify-content:center">
      <div style="background:var(--card,#fff);border-radius:20px;max-width:460px;width:90%;padding:32px 30px;box-shadow:0 24px 70px rgba(0,0,0,.35);text-align:center">
        <div style="font-size:44px;margin-bottom:2px">✍️</div>
        <div style="font-size:20px;font-weight:800;margin-bottom:2px">대본을 만들고 있어요</div>
        <div id=gmtopic class=muted style="font-size:13px;margin-bottom:10px"></div>
        <div id=gmelapsed style="font-size:38px;font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:1px;margin:4px 0;color:var(--accent)">0:00</div>
        <div style="height:8px;border-radius:6px;background:var(--surface,#eee);overflow:hidden;margin:10px 0 4px">
          <div id=gmbar style="height:100%;width:0%;background:var(--accent);transition:width .8s ease"></div>
        </div>
        <div id=gmstage style="font-size:12.5px;font-weight:700;min-height:16px;margin:6px 0"></div>
        <div class=muted style="font-size:12.5px;line-height:1.65;margin:10px 0 6px">최상의 대본을 만들기 위해 시간이 <b>15~20분</b> 소요됩니다.<br>이 페이지를 나가도 생성은 <b>계속</b>돼요 — 나중에 다시 들어와 결과를 확인하면 됩니다.</div>
        <div id=gmlog style="text-align:left;margin-top:10px;max-height:120px;overflow:auto;font-size:10.5px;font-family:monospace;color:var(--muted,#888);background:var(--surface,#f5f5f5);border-radius:9px;padding:9px;white-space:pre-wrap;line-height:1.5"></div>
      </div>
    </div>"""
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
    // 생성 상태 폴링 + 로딩 모달(가운데 팝업 · 경과시간 · 진행바)
    var HID=%HID%;
    var EST=1080;                 // 예상 총 소요(초) ≈ 18분 — 진행바 추정용
    var srvEl=0, anchor=Date.now(), tick=null;
    var modal=document.getElementById('genmodal');
    function fmt(t){t=Math.max(0,Math.floor(t));var m=Math.floor(t/60),s=t%60;return m+':'+(s<10?'0':'')+s;}
    function stageText(j){
      if(j.phase==='ingest'||j.status==='ingesting')return '거의 다 됐어요 — 편집·근거·이미지 정리 중';
      if(j.phase==='parsed'||j.status==='generated')return '대본 정리 중';
      if(j.status==='pending')return '준비 중 — 자료 봉인';
      return '자료 수집 → 지식정리(KB) → 대본 집필 중';
    }
    function showModal(j){
      modal.style.display='flex';
      document.getElementById('gmtopic').textContent=(j.topic?('주제: '+j.topic):'');
      document.getElementById('gmstage').textContent=stageText(j);
      var gl=document.getElementById('gmlog');gl.textContent=(j.log||'시작하는 중…');gl.scrollTop=gl.scrollHeight;
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
      if(j.running){btn.disabled=true;btn.textContent='생성 중…';showModal(j);
        s.innerHTML='<div class=log>'+(j.log||'시작하는 중…')+'</div>';setTimeout(poll,2500);}
      else{hideModal();
        var msg = j.ok
          ? '<p style="color:var(--good);font-weight:800;margin-top:10px">✅ 완료 — 아래 결과물에서 확인하세요.</p>'
          : ((j.status==='failed'||j.error) ? '<p style="color:var(--danger);font-weight:800;margin-top:10px">⛔ 실패 — '+(j.error||'의료광고 검수 불통과 또는 오류')+' (검수 통과 전엔 게시되지 않아요)</p>' : '');
        s.innerHTML=(j.log?'<div class=log>'+j.log+'</div>':'')+msg;
        btn.disabled=false;btn.textContent='대본 만들기';
        if(j.ok)setTimeout(()=>location.reload(),1600);}
    }).catch(function(){setTimeout(poll,4000);})}   // 폴링 실패해도 멈추지 않고 재시도(생성은 서버에서 계속)
    document.getElementById('runf').addEventListener('submit',function(){
      modal.style.display='flex';document.getElementById('gmstage').textContent='생성을 시작하고 있어요…';
      setTimeout(poll,1200);});
    if(%RUNNING%)poll();
    </script>""".replace("%HID%", '"'+h+'"').replace("%RUNNING%", "true" if running else "false")
    return page(name, body, script)

ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md", ".csv", ".hwp", ".pptx", ".zip"}

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
    saved = 0; toobig = []
    for f in request.files.getlist("files"):
        if not f or not f.filename: continue
        name = safe_filename(f.filename)     # 한글 유지 + 경로탈출 방지
        if not name: continue
        if os.path.splitext(name)[1].lower() not in ALLOWED_EXT: continue  # 허용 확장자만
        path = os.path.join(dest, name)
        f.save(path)                         # 스트리밍 저장(대용량도 메모리 부담↓). 이번 생성에 즉시 사용.
        sz = os.path.getsize(path)
        if eng and sz <= _MAT_MAX:           # 한도 이하 → PG 영구 저장(재시작에도 유지)
            try:
                with open(path, "rb") as fh:
                    save_material(eng, hid, name, fh.read(), created_by=session.get("user_id"))
            except Exception:
                pass
        elif eng:                            # 한도 초과 → 임시(disk)로만. 막지 않고 이번 생성엔 사용.
            toobig.append(name)
        saved += 1
    q = f"?ok={saved}" + ("&big=1" if toobig else "")
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
        if res.get("no_change"):
            return redirect(_ret(f"/scripts/{h}/{expected}"))
        return redirect(_ret(f"/scripts/{h}/{res['version_id']}", "edited"))
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
        _svc.regenerate_scene(make_engine(), _dash_ctx(h), block_key, feedback, version_id=version_id)
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
        _svc.revert_scene(make_engine(), _dash_ctx(h), block_key, seq=seq)   # 지정 seq(그 사진으로) 또는 최신
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
        _svc.upload_scene(make_engine(), _dash_ctx(h), block_key, raw, version_id=version_id)
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
    with tenant_conn(make_engine(), ctx.hospital_id, membership_id=ctx.membership_id) as cn:
        row = cn.execute(_t("select mime, data from scene_image_versions "
                            "where hospital_id=:h and block_key=:k and seq=:s limit 1"),
                         {"h": ctx.hospital_id, "k": block_key, "s": seq}).first()
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
    with tenant_conn(make_engine(), ctx.hospital_id, membership_id=ctx.membership_id) as cn:
        row = cn.execute(_t("select mime, data from scene_images where hospital_id=:h and block_key=:k limit 1"),
                         {"h": ctx.hospital_id, "k": block_key}).first()
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
        return redirect(f"/h/{h}/view/{os.path.basename(topic)}_package.html")
    with tenant_conn(eng, ctx.hospital_id) as cn:
        row = cn.execute(_t("select sv.id vid, sv.script_id sid from script_versions sv join scripts s "
                            "on s.id=sv.script_id where sv.hospital_id=:h and s.current_version_id=sv.id "
                            "order by sv.created_at desc limit 1"), {"h": ctx.hospital_id}).first()
        if not row:
            return redirect(f"/h/{h}/view/{os.path.basename(topic)}_package.html")
        vid, sid = row.vid, row.sid
        blocks = cn.execute(_t("select order_index, stable_block_key, text from script_blocks "
                               "where hospital_id=:h and version_id=:v order by order_index"),
                            {"h": ctx.hospital_id, "v": vid}).all()
        imgkeys = {r[0] for r in cn.execute(_t("select block_key from scene_images where hospital_id=:h"), {"h": ctx.hospital_id})}
        # 갤러리: 블록별 이전 이미지 seq 목록(오래된→최신). 현재본 + 이 히스토리를 나란히 보여준다.
        hist_by_key = {}
        for r in cn.execute(_t("select block_key, seq from scene_image_versions where hospital_id=:h order by block_key, seq"), {"h": ctx.hospital_id}):
            hist_by_key.setdefault(r[0], []).append(r[1])
    csrf = f'<input type="hidden" name="_csrf" value="{session.get("_csrf","")}">'
    rt = f'<input type="hidden" name="return_to" value="/h/{h}/edit/{os.path.basename(topic)}">'
    vid_s = str(vid)
    edit = {"by_idx": {b.order_index: {"key": b.stable_block_key, "text": b.text} for b in blocks},
            "csrf": csrf, "rt": rt, "version_id": vid_s,
            "edit_url": f"/scripts/{h}/{sid}/edit",
            "img_url": (lambda k: f"/scripts/{h}/img/{k}"),
            "has_img": (lambda k: k in imgkeys),
            "hist": (lambda k: hist_by_key.get(k, [])),   # 이전 이미지 seq 목록(갤러리)
            "imgv_url": (lambda k, s: f"/scripts/{h}/imgv/{k}/{s}"),
            "regen_url": (lambda k: f"/scripts/{h}/versions/{vid_s}/blocks/{k}/regen-image"),
            "revert_url": (lambda k: f"/scripts/{h}/versions/{vid_s}/blocks/{k}/revert-image"),
            "upload_url": (lambda k: f"/scripts/{h}/versions/{vid_s}/blocks/{k}/upload-image")}
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

if __name__ == "__main__":
    load_users()  # 기본 계정 보장 + 콘솔 안내
    port = int(os.environ.get("PORT", 5000))   # 배포 환경은 PORT 주입, 로컬은 5000
    print(f"브라우저에서 http://localhost:{port} 여세요. (로그인 필요)")
    app.run(host="0.0.0.0", port=port, debug=False)
