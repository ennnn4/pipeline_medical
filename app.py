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
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024   # 업로드 최대 500MB

# ── 작업 상태: 메모리 대신 sqlite에 저장(재시작해도 유지) ──────────
DB = os.path.join(ROOT, "data", "jobs.db")
def _db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS jobs (hospital TEXT PRIMARY KEY, topic TEXT, status TEXT, ok INTEGER, log TEXT, updated TEXT)")
    return c
def job_get(h):
    c = _db(); r = c.execute("SELECT hospital,topic,status,ok,log,updated FROM jobs WHERE hospital=?", (h,)).fetchone(); c.close()
    if not r: return {"running": False, "status": "idle", "ok": None, "log": "", "topic": ""}
    return {"hospital": r[0], "topic": r[1], "status": r[2], "ok": (None if r[3] is None else bool(r[3])),
            "log": r[4] or "", "updated": r[5], "running": r[2] == "running"}
def job_set(h, topic=None, status=None, ok=None, log=None):
    cur = job_get(h)
    c = _db()
    c.execute("REPLACE INTO jobs (hospital,topic,status,ok,log,updated) VALUES (?,?,?,?,?,?)",
              (h, topic if topic is not None else cur.get("topic",""),
               status if status is not None else cur.get("status","idle"),
               (1 if ok else 0) if ok is not None else (None if cur.get("ok") is None else (1 if cur["ok"] else 0)),
               log if log is not None else cur.get("log",""),
               datetime.datetime.now().isoformat(timespec="seconds")))
    c.commit(); c.close()

# ── 인증(세션) ──────────────────────────────────────────────
_sk = os.path.join(ROOT, ".secret")
# 배포(Render 등)에선 SECRET_KEY 환경변수 우선 — 재시작해도 세션 유지. 없으면 로컬 .secret 파일.
app.secret_key = (os.environ.get("SECRET_KEY")
                  or (open(_sk).read().strip() if os.path.exists(_sk)
                      else (lambda s: (open(_sk,"w").write(s), s)[1])(secrets.token_hex(32))))
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
<div class=nav><div class=nav-in><div class=brand><span class=dot>본</span>병원 유튜브 대본 생성기</div>{{ userhtml|safe }}</div></div>
<div class=wrap>{{ body|safe }}</div>{{ script|safe }}</body></html>"""

def page(title, body, script=""):
    u = session.get("user")
    userhtml = f'<div class=navr><span>{u}</span><a class="btn ghost" href="/logout">로그아웃</a></div>' if u else ""
    return render_template_string(PAGE, title=title, css=CSS, body=body, script=script, userhtml=userhtml)

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
    raw = [os.path.basename(p) for p in glob.glob(os.path.join(data_dir(h,"raw"),"*")) if os.path.isfile(p)]
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
    outlist = "".join(f'<div class=out><span>{os.path.basename(o)[:-5]}</span><a class=btn href="/h/{h}/view/{os.path.basename(o)}" target=_blank>대시보드 열기</a></div>' for o in outs) or '<div class=muted>아직 만든 대본이 없어요.</div>'
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
      <form id=runf method=post action="/h/{h}/run">
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
    saved = 0
    for f in request.files.getlist("files"):
        if not f or not f.filename: continue
        name = safe_filename(f.filename)     # 한글 유지 + 경로탈출 방지
        if not name: continue
        if os.path.splitext(name)[1].lower() not in ALLOWED_EXT: continue  # 허용 확장자만
        f.save(os.path.join(dest, name))
        saved += 1
    return redirect(f"/h/{h}")

def _run_pipeline(h, topic, evidence=True):
    job_set(h, topic=topic, status="running", ok=None, log="")
    log = ""; ok = False
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
    job_set(h, status="done", ok=ok, log=log)

@app.route("/h/<h>/run", methods=["POST"])
def run_ep(h):
    if not os.path.exists(cfg_path(h)): abort(404)
    topic = (request.form.get("topic","").strip() or "주제")[:60]
    evidence = bool(request.form.get("evidence"))
    if not job_get(h).get("running"):
        threading.Thread(target=_run_pipeline, args=(h, topic, evidence), daemon=True).start()
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
  <div class=logo><span class=dot style="width:44px;height:44px;border-radius:13px;font-size:23px">본</span></div>
  <h1>로그인</h1><p class=s>병원 유튜브 대본 생성기</p>
  {{ err_html|safe }}
  <form method=post>
    <label>아이디</label><input type=text name=username placeholder="아이디" required autofocus>
    <label>비밀번호</label><input type=password name=password placeholder="비밀번호" required>
    <button class="btn pri" type=submit>로그인</button>
  </form>
</div></div></body></html>"""

def _login_page(err=""):
    err_html = f'<div class=err>{err}</div>' if err else ""
    return render_template_string(LOGIN, css=CSS, err_html=err_html)

@app.route("/login", methods=["GET","POST"])
def login():
    if session.get("user"): return redirect("/")
    err = ""
    load_users()  # 첫 실행 시 기본 계정 생성(콘솔에 안내 출력)
    if request.method == "POST":
        u = request.form.get("username","").strip(); p = request.form.get("password","")
        users = load_users()
        if u in users and check_password_hash(users[u], p):
            session["user"] = u; return redirect("/")
        err = "아이디 또는 비밀번호가 올바르지 않습니다."
    return _login_page(err)

# 공개 회원가입 없음. 팀원 계정은 관리자가 config/users.yaml 에 직접 추가하거나
# ADMIN_PW 환경변수로 관리(비밀번호는 해시로만 저장).

@app.route("/logout")
def logout():
    session.clear(); return redirect("/login")

if __name__ == "__main__":
    load_users()  # 기본 계정 보장 + 콘솔 안내
    port = int(os.environ.get("PORT", 5000))   # 배포 환경은 PORT 주입, 로컬은 5000
    print(f"브라우저에서 http://localhost:{port} 여세요. (로그인 필요)")
    app.run(host="0.0.0.0", port=port, debug=False)
