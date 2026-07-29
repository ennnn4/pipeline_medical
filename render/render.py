"""
6단계 대시보드 렌더 (결정론). LLM 불필요.
패키지 JSON(director 출력 형태) → Toss 톤 단일 HTML. 스토리보드 프레임 자동 삽입.

사용: python -m render.render <package.json> [-o out.html]
"""
import json, sys, argparse, os, html
try:
    from .frames import frame_html
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from frames import frame_html

CSS = """
:root{--bg:#fff;--surface:#f9fafb;--card:#fff;--border:#e5e8eb;--ink:#191f28;--ink2:#4e5968;--muted:#8b95a1;--accent:#3182f6;--accw:#eaf2fe;--acci:#1b64da;--danger:#f04452;--dw:#fdeaec;--radius:20px;--font:'Pretendard',-apple-system,'Malgun Gothic',system-ui,sans-serif}
@media(prefers-color-scheme:dark){:root{--bg:#161719;--surface:#1c1d20;--card:#1d1e21;--border:#2e3034;--ink:#f2f4f6;--ink2:#b0b8c1;--muted:#868e96;--accent:#4593fc;--accw:#17263f;--acci:#9dc2ff;--danger:#ff6b78;--dw:#301418}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--font);line-height:1.65;letter-spacing:-.01em}
h1,h2,h3{margin:0;letter-spacing:-.035em;font-weight:800;text-wrap:balance}
.wrap{max-width:1080px;margin:0 auto;padding:0 24px}
.hero{padding:64px 0 32px}.eyebrow{display:inline-block;font-size:13px;font-weight:700;color:var(--acci);background:var(--accw);padding:7px 13px;border-radius:100px}
.hero h1{font-size:clamp(32px,5vw,52px);margin:20px 0 0;letter-spacing:-.045em}
.hook{background:var(--accw);border-radius:var(--radius);padding:28px;margin:28px 0}.hook .l{font-size:12px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:var(--acci)}.hook .q{font-size:clamp(20px,3vw,28px);font-weight:800;margin-top:12px;color:var(--ink)}
.sec{padding:20px 0}.sec-tag{font-size:12px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;color:var(--accent)}.sec h2{font-size:26px;margin-top:10px}
.script{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-top:16px}
.beat{display:grid;grid-template-columns:96px 1fr;border-top:1px solid var(--border)}.beat:first-child{border-top:0}
.tc{padding:20px 14px;background:var(--surface);border-right:1px solid var(--border);font-size:12px;font-weight:700;color:var(--acci)}
.body{padding:18px 20px}.bt{font-size:14px;font-weight:800;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.tag{font-size:11px;font-weight:700;padding:3px 8px;border-radius:6px;background:var(--surface);color:var(--muted)}
.frame{margin-top:11px;border-radius:12px;overflow:hidden;border:1px solid var(--border);position:relative;background:#0d1016;max-width:420px}
.frame svg{display:block;width:100%;height:auto}.frame .chrome{position:absolute;top:9px;left:11px;font-size:9.5px;font-weight:800;color:#fff;letter-spacing:.1em;opacity:.72}
.frame .rec{display:inline-block;width:7px;height:7px;border-radius:50%;background:#ff4d4f;margin-right:5px}.frame .ftc{position:absolute;top:9px;right:11px;font-size:9.5px;font-weight:700;color:#fff;opacity:.6}
.frame-cap{font-size:11.5px;color:var(--muted);font-weight:600;margin:6px 0 0}
.scene{display:grid;grid-template-columns:52px 1fr;gap:10px;margin-top:11px;padding:11px 13px;background:var(--surface);border:1px solid var(--border);border-radius:10px}
.scene .lab{font-size:11px;font-weight:800;color:var(--muted)}.scene .desc{font-size:13.5px;color:var(--ink2);font-weight:500}
.scene.talk{background:var(--card);border-left:3px solid var(--accent)}.scene.talk .lab{color:var(--acci)}.scene.talk .desc{color:var(--ink);font-size:14.5px}
details{background:var(--card);border:1px solid var(--border);border-radius:12px;margin-top:12px}summary{cursor:pointer;padding:16px 20px;font-weight:700;list-style:none}summary::-webkit-details-marker{display:none}
details .in{padding:0 20px 18px;color:var(--ink2);font-size:14px}details ol,details ul{padding-left:20px}
footer{padding:48px 24px;border-top:1px solid var(--border);color:var(--muted);text-align:center;font-size:13px;margin-top:40px}
"""

def esc(s): return html.escape(str(s))

def render(pkg):
    beats = ""
    for b in pkg.get("script", []):
        tc = b.get("tc","")
        tc_html = tc.replace("–","<br>").replace("-","<br>") if "–" in tc or "-" in tc else tc
        tags = "".join(f'<span class="tag">{esc(t)}</span>' for t in b.get("tags",[]))
        beats += f"""<div class="beat"><div class="tc">{esc(tc_html)}</div><div class="body">
          <div class="bt">{esc(b.get('block',''))} {tags}</div>
          {frame_html(b, esc(tc.split('–')[0].split('-')[0]))}
          <div class="scene"><span class="lab">🎬 화면</span><span class="desc">{esc(b.get('scene',''))}</span></div>
          <div class="scene talk"><span class="lab">🎙 대사</span><span class="desc">{esc(b.get('say',''))}</span></div>
        </div></div>"""

    def acc(title, items):
        if not items: return ""
        lis = "".join(f"<li>{esc(x)}</li>" for x in items)
        return f'<details><summary>{esc(title)} ({len(items)})</summary><div class="in"><ol>{lis}</ol></div></details>'

    dels = "".join([
        acc("제목 후보", pkg.get("titles")), acc("썸네일 문구", pkg.get("thumbnails")),
        acc("챕터", pkg.get("chapters")), acc("쇼츠", pkg.get("shorts")),
        acc("참고 논문", pkg.get("papers")), acc("화면자료", pkg.get("screen_assets")),
        acc("편집 포인트", pkg.get("edit_points")), acc("자막 강조", pkg.get("caption_emphasis")),
        acc("원장 촬영 포인트", pkg.get("shoot_points")),
    ])
    pinned = pkg.get("pinned_comment",""); desc = pkg.get("description","")

    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(pkg.get('episode_title','본큐어 유튜브 패키지'))}</title><style>{CSS}</style></head><body>
<header class="hero wrap"><span class="eyebrow">Episode Package · 본큐어 유튜브</span>
<h1>{esc(pkg.get('episode_title',''))}</h1>
<div class="hook"><div class="l">클릭 포인트 · Hook</div><div class="q">{esc(pkg.get('hook',''))}</div></div></header>
<section class="sec wrap"><div class="sec-tag">Script</div><h2>대본 · 화면 + 대사</h2>
<div class="script">{beats}</div></section>
<section class="sec wrap"><div class="sec-tag">Deliverables</div><h2>산출물</h2>{dels}
<details><summary>고정 댓글 · 설명란</summary><div class="in"><p><b>고정 댓글</b><br>{esc(pinned)}</p><p style="margin-top:12px"><b>설명란</b><br>{esc(desc)}</p></div></details>
</section>
<footer>본큐어 유튜브 패키지 · 화자 송정현 · 발행 전 원장 의학검수·의료광고 심의 필요</footer>
</body></html>"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("package")
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args()
    pkg = json.load(open(a.package, encoding="utf-8"))
    out = a.out or (os.path.splitext(a.package)[0] + ".html")
    open(out, "w", encoding="utf-8").write(render(pkg))
    print("렌더 완료 →", out)

if __name__ == "__main__":
    main()
