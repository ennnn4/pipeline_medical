"""
6단계 대시보드 렌더 (결정론). LLM 불필요.
패키지 JSON(director 출력) → Toss 톤 단일 HTML (나브·테마토글·히어로·스탯·훅·스크립트+프레임·산출물).

사용: python -m render.render <package.json> [-o out.html]
"""
import json, sys, argparse, os, html
try:
    from .frames import frame_html
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from frames import frame_html

CSS = """
:root{--bg:#fff;--surface:#f9fafb;--surface2:#f2f4f6;--card:#fff;--border:#e5e8eb;--ink:#191f28;--ink2:#4e5968;--muted:#8b95a1;--accent:#3182f6;--accw:#eaf2fe;--acci:#1b64da;--good:#12b886;--gw:#e6f7f0;--warn:#f59f00;--ww:#fff4e0;--danger:#f04452;--dw:#fdeaec;--radius:20px;--radius-sm:12px;--shadow:0 1px 3px rgba(25,31,40,.04),0 8px 24px rgba(25,31,40,.05);--font:'Pretendard',-apple-system,BlinkMacSystemFont,'Malgun Gothic',system-ui,sans-serif;--maxw:1080px}
@media(prefers-color-scheme:dark){:root{--bg:#161719;--surface:#1c1d20;--surface2:#232427;--card:#1d1e21;--border:#2e3034;--ink:#f2f4f6;--ink2:#b0b8c1;--muted:#868e96;--accent:#4593fc;--accw:#17263f;--acci:#9dc2ff;--good:#2ac08a;--gw:#10251c;--warn:#ffb84d;--ww:#2e2411;--danger:#ff6b78;--dw:#301418;--shadow:0 1px 3px rgba(0,0,0,.4)}}
:root[data-theme=light]{--bg:#fff;--surface:#f9fafb;--card:#fff;--border:#e5e8eb;--ink:#191f28;--ink2:#4e5968;--muted:#8b95a1;--accent:#3182f6;--accw:#eaf2fe;--acci:#1b64da}
:root[data-theme=dark]{--bg:#161719;--surface:#1c1d20;--card:#1d1e21;--border:#2e3034;--ink:#f2f4f6;--ink2:#b0b8c1;--muted:#868e96;--accent:#4593fc;--accw:#17263f;--acci:#9dc2ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--font);line-height:1.65;letter-spacing:-.01em;-webkit-font-smoothing:antialiased}
h1,h2,h3{margin:0;letter-spacing:-.035em;font-weight:800;text-wrap:balance}
.nav{position:sticky;top:0;z-index:50;background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:saturate(1.4) blur(14px);border-bottom:1px solid var(--border)}
.nav-in{max-width:var(--maxw);margin:0 auto;padding:14px 24px;display:flex;align-items:center;gap:20px}
.brand{display:flex;align-items:center;gap:10px;font-weight:800;font-size:16px;letter-spacing:-.04em}
.brand .dot{width:22px;height:22px;border-radius:7px;background:var(--accent);display:grid;place-items:center;color:#fff;font-size:13px}
.nav-links{display:flex;gap:4px;margin-left:auto;flex-wrap:wrap}
.nav-links a{font-size:14px;font-weight:600;color:var(--ink2);text-decoration:none;padding:7px 12px;border-radius:9px}
.nav-links a:hover{color:var(--ink);background:var(--surface2)}
.toggle{border:1px solid var(--border);background:var(--card);color:var(--ink2);width:38px;height:38px;border-radius:11px;cursor:pointer;font-size:16px}
@media(max-width:760px){.nav-links{display:none}}
.wrap{max-width:var(--maxw);margin:0 auto;padding:0 24px}section{scroll-margin-top:76px}
.hero{padding:64px 0 36px}.eyebrow{display:inline-block;font-size:13px;font-weight:700;color:var(--acci);background:var(--accw);padding:7px 13px;border-radius:100px}
.hero h1{font-size:clamp(32px,5.5vw,54px);margin:20px 0 0;letter-spacing:-.045em;line-height:1.15}
.hero .sub{font-size:clamp(15px,2vw,19px);color:var(--ink2);font-weight:500;margin-top:18px;max-width:640px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:36px}
.stat{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-sm);padding:20px 18px;box-shadow:var(--shadow)}
.stat .n{font-size:30px;font-weight:800;letter-spacing:-.05em}.stat .n small{font-size:15px;color:var(--muted);font-weight:700}.stat .l{font-size:13px;color:var(--muted);font-weight:600;margin-top:4px}
@media(max-width:680px){.stats{grid-template-columns:repeat(2,1fr)}}
.sec{padding:44px 0}.sec-tag{font-size:12px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;color:var(--accent)}.sec h2{font-size:clamp(22px,4vw,32px);margin-top:10px}.sec .lead{color:var(--ink2);font-weight:500;margin-top:10px;max-width:640px}
.hook{background:var(--accw);border-radius:var(--radius);padding:clamp(24px,4vw,32px);margin:24px 0}.hook .l{font-size:12px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:var(--acci)}.hook .q{font-size:clamp(20px,3.2vw,28px);font-weight:800;margin-top:12px;color:var(--ink);line-height:1.3}
.script{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-top:16px;box-shadow:var(--shadow)}
.beat{display:grid;grid-template-columns:96px 1fr;border-top:1px solid var(--border)}.beat:first-child{border-top:0}
.tc{padding:20px 14px;background:var(--surface);border-right:1px solid var(--border);font-size:12px;font-weight:700;color:var(--acci)}
.beat.crit .tc{color:var(--danger)}
.body{padding:18px 20px}.bt{font-size:14px;font-weight:800;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.tag{font-size:11px;font-weight:700;padding:3px 8px;border-radius:6px;background:var(--surface2);color:var(--muted)}.tag.bad{background:var(--dw);color:var(--danger)}
.frame{margin-top:11px;border-radius:12px;overflow:hidden;border:1px solid var(--border);position:relative;background:#0d1016;max-width:420px;box-shadow:var(--shadow)}
.frame svg{display:block;width:100%;height:auto}.frame .chrome{position:absolute;top:9px;left:11px;font-size:9.5px;font-weight:800;color:#fff;letter-spacing:.1em;opacity:.72}
.frame .rec{display:inline-block;width:7px;height:7px;border-radius:50%;background:#ff4d4f;margin-right:5px}.frame .ftc{position:absolute;top:9px;right:11px;font-size:9.5px;font-weight:700;color:#fff;opacity:.6}
.frame-cap{font-size:11.5px;color:var(--muted);font-weight:600;margin:6px 0 0}
.scene{display:grid;grid-template-columns:52px 1fr;gap:10px;margin-top:11px;padding:11px 13px;background:var(--surface);border:1px solid var(--border);border-radius:10px}
.scene .lab{font-size:11px;font-weight:800;color:var(--muted)}.scene .desc{font-size:13.5px;color:var(--ink2);font-weight:500;line-height:1.62}
.scene.talk{background:var(--card);border-left:3px solid var(--accent)}.scene.talk .lab{color:var(--acci)}.scene.talk .desc{color:var(--ink);font-size:14.5px;line-height:1.72}
details{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-sm);margin-top:12px;overflow:hidden}
summary{cursor:pointer;padding:16px 20px;font-weight:700;font-size:15px;list-style:none;display:flex;gap:10px;align-items:center}summary::-webkit-details-marker{display:none}
summary .arw{margin-left:auto;color:var(--muted);font-size:13px}details[open] summary .arw{transform:rotate(90deg)}
details .in{padding:0 20px 18px;color:var(--ink2);font-size:14px}details ol,details ul{padding-left:20px;display:flex;flex-direction:column;gap:6px}
.disc{max-width:var(--maxw);margin:32px auto 0;padding:18px 20px;background:var(--surface2);border-radius:var(--radius-sm);font-size:12.5px;color:var(--muted);font-weight:500;line-height:1.7}
footer{padding:48px 24px;border-top:1px solid var(--border);color:var(--muted);text-align:center;font-size:13px;margin-top:20px}
html{scroll-behavior:smooth}@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
"""

def esc(s): return html.escape(str(s))

def render(pkg, meta=None):
    meta = meta or {}
    script = pkg.get("script", [])
    say_chars = sum(len((b.get("say") or "").replace(" ","")) for b in script)
    mins = round(say_chars/450, 1)
    crit_words = ("응급","하지 말","119")

    beats = ""
    for b in script:
        tc = b.get("tc","")
        tc_html = tc.replace("–","<br>").replace(" - ","<br>")
        crit = " crit" if any(w in (b.get("block","")) for w in crit_words) else ""
        tags = "".join(f'<span class="tag{" bad" if "0" in t or "없음" in t else ""}">{esc(t)}</span>' for t in b.get("tags",[]))
        beats += f"""<div class="beat{crit}"><div class="tc">{esc(tc_html)}</div><div class="body">
          <div class="bt">{esc(b.get('block',''))} {tags}</div>
          {frame_html(b, esc(tc.split('–')[0].split(' - ')[0].strip()))}
          <div class="scene"><span class="lab">🎬 화면</span><span class="desc">{esc(b.get('scene',''))}</span></div>
          <div class="scene talk"><span class="lab">🎙 대사</span><span class="desc">{esc(b.get('say',''))}</span></div>
        </div></div>"""

    def acc(title, items):
        if not items: return ""
        lis = "".join(f"<li>{esc(x)}</li>" for x in items)
        return f'<details><summary>{esc(title)} ({len(items)})<span class="arw">▶</span></summary><div class="in"><ol>{lis}</ol></div></details>'
    dels = "".join([
        acc("제목 후보", pkg.get("titles")), acc("썸네일 문구", pkg.get("thumbnails")),
        acc("챕터", pkg.get("chapters")), acc("쇼츠", pkg.get("shorts")),
        acc("참고 논문", pkg.get("papers")), acc("화면자료", pkg.get("screen_assets")),
        acc("편집 포인트", pkg.get("edit_points")), acc("자막 강조", pkg.get("caption_emphasis")),
        acc("원장 촬영 포인트", pkg.get("shoot_points")),
    ])
    extra = ""
    if pkg.get("pinned_comment") or pkg.get("description"):
        extra = f'<details><summary>고정 댓글 · 설명란<span class="arw">▶</span></summary><div class="in"><p><b>고정 댓글</b><br>{esc(pkg.get("pinned_comment",""))}</p><p style="margin-top:12px"><b>설명란</b><br>{esc(pkg.get("description",""))}</p></div></details>'

    title = pkg.get("episode_title","본큐어 유튜브 패키지")
    host = meta.get("host","송정현")
    files_n = meta.get("files_n","—")
    kb_n = meta.get("kb_n","—")
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 본큐어 유튜브</title><style>{CSS}</style></head><body>
<nav class="nav"><div class="nav-in"><div class="brand"><span class="dot">본</span>본큐어 유튜브 · 대본 패키지</div>
<div class="nav-links"><a href="#hook">기획</a><a href="#script">대본</a><a href="#deliverables">산출물</a></div>
<button class="toggle" id="tg" aria-label="테마 전환">◐</button></div></nav>
<header class="hero wrap"><span class="eyebrow">Episode Package · 콘텐츠 디렉터 시스템</span>
<h1>{esc(title)}</h1>
<p class="sub">파이프라인이 KB를 근거로 생성한 대본 패키지입니다. 화자는 항상 <b style="color:var(--ink)">{esc(host)} 대표원장</b>. 발행 전 원장 의학검수·의료광고 심의 필요.</p>
<div class="stats">
<div class="stat"><div class="n">{len(script)}<small>비트</small></div><div class="l">대본 블록</div></div>
<div class="stat"><div class="n">{mins}<small>분</small></div><div class="l">예상 러닝타임(발화 {say_chars}자)</div></div>
<div class="stat"><div class="n">12<small>종</small></div><div class="l">산출물</div></div>
<div class="stat"><div class="n">{esc(kb_n)}<small>종</small></div><div class="l">참조 KB 문서</div></div>
</div></header>
<section class="sec wrap" id="hook"><div class="sec-tag">Episode Brief</div><h2>이 영상의 클릭 포인트</h2>
<div class="hook"><div class="l">Hook</div><div class="q">{esc(pkg.get('hook',''))}</div></div></section>
<section class="sec wrap" id="script"><div class="sec-tag">Script</div><h2>대본 · 화면 + 대사</h2>
<p class="lead">타임코드마다 🎬화면(무엇이 보이나) + 🎙대사(원장이 말하는 완성 문장). 스토리보드 프레임은 자동 생성.</p>
<div class="script">{beats}</div></section>
<section class="sec wrap" id="deliverables"><div class="sec-tag">Deliverables</div><h2>산출물 12종</h2>{dels}{extra}</section>
<div class="disc">본 페이지는 본큐어한의원 유튜브 대본 패키지입니다. 대본 속 의학 정보는 교육·정보 제공 목적이며, 효과는 개인마다 다르고 부작용 가능성이 있습니다. 논문 증례는 단일 증례일 수 있으며 모든 환자에게 동일 결과를 보장하지 않습니다. 발행 전 원장 의학검수와 의료광고 심의 확인이 필요합니다.</div>
<footer>본큐어 유튜브 · 화자 {esc(host)} · 파이프라인 자동 생성 (render.py)</footer>
<script>(function(){{var r=document.documentElement,b=document.getElementById('tg');function c(){{return r.getAttribute('data-theme')||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light')}}b.addEventListener('click',function(){{r.setAttribute('data-theme',c()==='dark'?'light':'dark')}})}})();</script>
</body></html>"""

def _meta():
    """corpus/kb 개수를 세서 스탯에 넣는다(있으면)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    m = {"host":"송정현"}
    man = os.path.join(root,"data","corpus","_MANIFEST.tsv")
    if os.path.exists(man):
        m["files_n"] = max(0, len(open(man,encoding="utf-8").read().splitlines())-1)
    kb = os.path.join(root,"data","kb")
    if os.path.isdir(kb):
        m["kb_n"] = len([f for f in os.listdir(kb) if f.endswith(".json")])
    return m

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("package"); ap.add_argument("-o","--out",default=None)
    a = ap.parse_args()
    pkg = json.load(open(a.package, encoding="utf-8"))
    out = a.out or (os.path.splitext(a.package)[0] + ".html")
    open(out,"w",encoding="utf-8").write(render(pkg, _meta()))
    print("렌더 완료 →", out)

if __name__ == "__main__":
    main()
