#!/usr/bin/env python
"""
boncure-pipeline 로컬 웹앱 — 터미널·yaml 없이 브라우저로 쓴다.
실행:  python app.py   → 브라우저에서 http://localhost:5000
기능: 병원 만들기(폼) · 자료 업로드(끌어놓기) · 대본 생성(버튼) · 대시보드 보기.
엔진(run.py)을 그대로 호출하므로 파이프라인 로직은 재사용.
"""
import os, sys, glob, subprocess, threading, re, io, secrets, sqlite3, datetime, unicodedata
from flask import Flask, request, redirect, send_file, abort, render_template_string, jsonify, url_for, session
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
                "select status, phase, topic, version_id, error_message from generation_jobs "
                "where hospital_id=:h order by created_at desc limit 1"), {"h": hid}).mappings().first()
    except Exception:
        return None

def job_get(h):
    log = _LOG.get(h, "")
    j = _pg_latest_job(h)
    if j is not None:      # PG 병원 → generation_jobs가 상태의 단일 원본
        return {"hospital": h, "topic": j["topic"], "status": j["status"], "phase": j["phase"],
                "ok": (j["status"] == "completed"), "running": j["status"] in _RUNNING_STATES, "log": log,
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

@app.before_request
def _guard():
    if request.endpoint in ("login","static"): return   # 공개 회원가입 없음
    if not session.get("user"): return redirect("/login")

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
    body = f"""
    <div class=hero><h1>병원 유튜브를,<br>대본이 아니라 버튼으로.</h1>
      <p>자료를 올리고 버튼만 누르면 촬영용 대본 패키지가 나옵니다. 병원을 고르거나 새로 만드세요.</p></div>
    <div class=hlist>{cards}</div>
    <div class=card id=new>
      <h2>+ 새 병원 만들기</h2>
      <form method=post action="/new">
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

@app.route("/h/<h>")
def hospital(h):
    if not os.path.exists(cfg_path(h)): abort(404)
    cfg = _yaml().safe_load(open(cfg_path(h), encoding="utf-8")) or {}
    name = cfg.get("hospital",{}).get("name", h)
    diseases = cfg.get("diseases") or []
    raw = _material_names(h)     # 영속(PG) 우선, 없으면 disk
    outs = sorted(glob.glob(os.path.join(data_dir(h,"out"),"*.html")))
    filelist = "".join(f"<li>{f}</li>" for f in raw) or "<li class=muted>아직 업로드된 자료가 없어요.</li>"
    # 필요 자료 체크리스트 (config의 input_checklist 기준, 파일명 매칭)
    from ingest.extract import categorize
    checklist = cfg.get("input_checklist", [])
    counts = {}
    for fn in raw:
        k = categorize(fn, checklist); counts[k] = counts.get(k, 0) + 1
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
        misswarn += '<div class=note style="border-color:var(--danger);color:var(--danger)">⚠️ 40MB 넘는 파일은 제외됐어요(영구저장 한도). 나눠서 올려주세요.</div>'
    studio_url = _pg_studio_url(h)
    studio_cta = (f'<a class="btn pri" href="{studio_url}" style="display:block;text-align:center;margin-bottom:12px">'
                  f'✏️ 스튜디오에서 편집 · 근거검증 · 장면이미지 · 승인 →</a>') if studio_url else ''
    outlist = studio_cta + ("".join(f'<div class=out><span>{os.path.basename(o)[:-5]}</span><a class=btn href="/h/{h}/view/{os.path.basename(o)}" target=_blank>대시보드 열기</a></div>' for o in outs) or '<div class=muted>아직 만든 대본이 없어요.</div>')
    dz_opts = "".join(f'<button type=button class="btn dz" onclick="setTopic(this)">{d}</button>' for d in diseases)
    job = job_get(h)
    running = job.get("running")
    body = f"""
    <div class=row style="justify-content:space-between">
      <div><h1>{name}</h1><p class=sub>{h}</p></div><a href="/" class=btn>← 병원 목록</a>
    </div>

    <div class=card>
      <h2 style="margin-top:0">① 자료 업로드</h2>
      <div class=note>이런 자료를 넣어주세요 — <b>✓</b>=받음 · <b>없음</b>=필수인데 안 들어옴 · 회색=선택</div>
      <div class=chk style="margin:11px 0 6px">{chk}</div>
      {misswarn}
      <form id=upf method=post action="/h/{h}/upload" enctype=multipart/form-data>
        <div class=drop id=drop>파일을 여기로 끌어다 놓거나 클릭 (pdf·docx·txt·zip)
          <input id=fin type=file name=files multiple style="display:none">
        </div>
        <div class=row style="margin-top:12px"><button class="btn pri" type=submit>업로드</button>
        <span class=muted>설문지·인터뷰·논문·강의자료·기존 대본 등. zip 통째로도 OK</span></div>
      </form>
      <ul class=files>{filelist}</ul>
    </div>

    <div class=card>
      <h2 style="margin-top:0">② 대본 만들기</h2>
      <div class=note>주력 질환: {", ".join(diseases) or "설정에 없음"} — 아래 버튼 누르면 주제 자동 입력</div>
      <div class=chk style="margin:10px 0">{dz_opts}</div>
      <form id=runf method=post action="/h/{h}/run" onsubmit="var r=document.getElementById('reqkey');r.value=(window.crypto&&crypto.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random());document.getElementById('runbtn').disabled=true;">
        <input type=hidden name=reqkey id=reqkey>
        <label>주제</label><input type=text id=topic name=topic placeholder="예: 오십견" required>
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
    </div>"""
    script = """<script>
    var drop=document.getElementById('drop'),fin=document.getElementById('fin');
    drop.onclick=function(){fin.click()};
    fin.onchange=function(){if(fin.files.length)drop.textContent=fin.files.length+'개 선택됨 — 업로드를 누르세요'};
    ['dragover','dragenter'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('over')}));
    ['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('over')}));
    drop.addEventListener('drop',ev=>{fin.files=ev.dataTransfer.files;fin.onchange()});
    window.setTopic=function(b){document.getElementById('topic').value=b.textContent};
    // 생성 상태 폴링
    var HID=%HID%;
    function poll(){fetch('/h/'+HID+'/status').then(r=>r.json()).then(j=>{
      var s=document.getElementById('status'),btn=document.getElementById('runbtn');
      if(j.status==='running'){btn.disabled=true;btn.textContent='생성 중…';
        s.innerHTML='<div class=log>'+ (j.log||'시작하는 중…') +'</div>';setTimeout(poll,1500);}
      else if(j.status==='done'){
        var msg = j.ok
          ? '<p style="color:var(--good);font-weight:800;margin-top:10px">✅ 완료 — 아래 결과물에서 확인하세요.</p>'
          : '<p style="color:var(--danger);font-weight:800;margin-top:10px">⛔ 실패 — 의료광고 검수 불통과 또는 오류입니다. 로그를 확인하세요. (검수 통과 전엔 결과가 게시되지 않습니다)</p>';
        s.innerHTML='<div class=log>'+(j.log||'')+'</div>'+msg;
        btn.disabled=false;btn.textContent='대본 만들기';
        if(j.ok)setTimeout(()=>location.reload(),1500);}
    })}
    document.getElementById('runf').addEventListener('submit',function(){setTimeout(poll,1500)});
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
    hid = _pg_hospital_id(h)     # PG 병원이면 자료를 PostgreSQL에도 저장(영속, 재배포에도 안 사라짐)
    eng = None; _MAT_MAX = 40 * 1024 * 1024
    if hid:
        try:
            from store.db import make_engine
            from store.materials import save_material, MAX_BYTES as _MAT_MAX
            eng = make_engine()
        except Exception:
            eng = None
    saved = 0; rejected = []
    for f in request.files.getlist("files"):
        if not f or not f.filename: continue
        name = safe_filename(f.filename)     # 한글 유지 + 경로탈출 방지
        if not name: continue
        if os.path.splitext(name)[1].lower() not in ALLOWED_EXT: continue  # 허용 확장자만
        raw = f.read()
        if eng and len(raw) > _MAT_MAX:      # PG 영속 불가 크기 → 명시적 거부(임시디스크 fallback 안 함)
            rejected.append(name); continue
        with open(os.path.join(dest, name), "wb") as out:   # 즉시 사용용 disk 캐시
            out.write(raw)
        if eng:
            try:
                save_material(eng, hid, name, raw)           # 영속 저장(PostgreSQL bytea)
            except Exception:
                pass
        saved += 1
    q = f"?ok={saved}" + ("&big=1" if rejected else "")
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

def _pg_studio_url(h):
    """이 병원의 최신 PG 버전 편집 URL(스튜디오). 없으면 None."""
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
        return f"/studio/ui/h/{h}/versions/{vid}" if vid else None
    except Exception:
        return None

def _run_pipeline(h, topic, evidence=True, request_key=None):
    """GPT P0 반영: run.py '전에' PG generation_job(pending) 생성 → generating → generated →
    ingesting → completed. 각 상태는 별도 트랜잭션이라 실패해도 job에 흔적이 남음."""
    import uuid as _uuid, json as _json
    from store.db import make_engine
    from store import ingest as _ing
    job_set(h, topic=topic, status="running", ok=None, log="")
    log = ""; ok = False
    hid = _pg_hospital_id(h)
    request_key = request_key or _uuid.uuid4().hex
    job_id = None
    if hid:
        try:    # 재배포로 disk가 비었을 수 있으니 PG의 영속 자료를 raw/로 복원 후 생성
            from store.materials import materialize_to_disk
            materialize_to_disk(make_engine(), hid, data_dir(h, "raw"))
        except Exception:
            pass
        try:
            cj = _ing.create_job(make_engine(), hid, topic, request_key,
                                 target_script_id=_pg_script_id_for_topic(hid, topic))
            job_id = cj["job_id"]
            if cj["reused"] and cj["status"] in ("pending", "generating", "ingesting"):
                job_set(h, status="done", ok=False, log="[중복 요청] 이미 진행 중인 생성이 있습니다."); return
            _ing.mark_job(make_engine(), hid, job_id, "generating", allowed_from={"pending"}, phase="run.py", started=True)
        except Exception as e:
            log += f"\n[스튜디오 job 경고] {e}"
    try:
        cmd = [PY, "run.py", "all", "--hospital", h, "--topic", topic]
        if evidence: cmd.append("--evidence")   # 논문 근거 대조 + 시각자료 추출
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONUTF8": "1"})
        for line in io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="ignore"):
            log += line
            job_set(h, log=log)
        proc.wait()
        ok = (proc.returncode == 0)   # run.py all 이 검수 실패/오류 시 non-zero 반환
    except Exception as e:
        log += f"\n[오류] {e}"
        if hid and job_id:
            try: _ing.mark_job(make_engine(), hid, job_id, "failed", allowed_from={"pending","generating","generated","ingesting"},
                               error_code="run_error", error_message=str(e), finished=True)
            except Exception: pass
    if ok and hid and job_id:
        try:
            _ing.mark_job(make_engine(), hid, job_id, "generated", allowed_from={"generating"}, phase="parsed")
            pkg = _json.load(open(os.path.join(data_dir(h, "out"), f"{topic}_package.json"), encoding="utf-8"))
            _ing.mark_job(make_engine(), hid, job_id, "ingesting", allowed_from={"generated"}, phase="ingest")
            res = _ing.ingest_content(make_engine(), hid, job_id, pkg.get("script") or [])  # topic·script_id 모두 job에서
            log += f"\n[스튜디오] 편집·근거·이미지 준비 완료(블록 {res['blocks']}·주장 {res['claims']})."
            job_set(h, log=log)
        except Exception as e:
            log += f"\n[스튜디오 적재 오류] {e}"
            # completed는 덮지 않음(늦은 예외 방지)
            try: _ing.mark_job(make_engine(), hid, job_id, "failed", allowed_from={"pending","generating","generated","ingesting"},
                               error_code="ingest_error", error_message=str(e), finished=True)
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
    if not job_get(h).get("running"):
        threading.Thread(target=_run_pipeline, args=(h, topic, evidence, reqkey), daemon=True).start()
    return redirect(f"/h/{h}")

@app.route("/h/<h>/status")
def status(h):
    return jsonify(job_get(h))

@app.route("/h/<h>/view/<path:fn>")
def view(h, fn):
    p = os.path.join(data_dir(h,"out"), os.path.basename(fn))
    if not os.path.exists(p): abort(404)
    return send_file(p)

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
