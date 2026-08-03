"""
6단계 대시보드 렌더 (결정론). LLM 불필요.
패키지 JSON(director 출력) → Toss 톤 단일 HTML (나브·테마토글·히어로·스탯·훅·스크립트+프레임·산출물).

사용: python -m render.render <package.json> [-o out.html]
"""
import json, sys, argparse, os, html, re
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
.stage{display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start}
.sidecol{display:flex;flex-direction:column;gap:12px;margin-top:11px;max-width:320px}
.planbox{border:1px dashed var(--border);border-radius:12px;padding:12px 14px;background:var(--surface)}
.pb-h{font-size:13px;font-weight:800;color:var(--ink);display:flex;gap:7px;align-items:center}
.pb-tag{font-size:10px;font-weight:800;color:var(--muted);background:var(--surface2);padding:2px 7px;border-radius:5px}
.pb-d{background:transparent;border:0;margin:8px 0 0}
.pb-d summary{padding:8px 11px;background:var(--accw);color:var(--acci);border-radius:8px;font-size:12.5px;font-weight:700;display:flex;gap:8px;align-items:center}
.pb-copy{margin-left:auto;font-size:11px;font-weight:700;border:1px solid var(--border);background:var(--card);color:var(--ink2);border-radius:6px;padding:3px 8px;cursor:pointer}
.pb-p{font-size:12px;color:var(--ink2);font-weight:500;line-height:1.55;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-top:7px;white-space:pre-wrap}
.pb-buy{display:inline-block;margin-top:9px;font-size:12.5px;font-weight:700;color:var(--acci)}
.refthumbs{margin-top:0}
.rth-row{display:flex;gap:7px;flex-wrap:wrap;max-width:300px}
.rth{width:66px;height:66px;object-fit:cover;border-radius:9px;border:1px solid var(--border);background:var(--surface);cursor:zoom-in;transition:border-color .12s,transform .12s}
.rth:hover{border-color:var(--accent);transform:scale(1.04)}
.rth-lab{font-size:11px;color:var(--muted);font-weight:600;margin-top:7px}
.lb{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.88);display:none;align-items:center;justify-content:center;flex-direction:column;gap:16px;padding:24px}
.lb.on{display:flex}
.lb img{max-width:92vw;max-height:74vh;border-radius:10px;box-shadow:0 12px 48px rgba(0,0,0,.55);background:#fff}
.lb-cap{color:#e5e8eb;font-size:13px;font-weight:600;text-align:center;max-width:82vw;line-height:1.5}
.lb-x{position:absolute;top:14px;right:20px;background:none;border:none;color:#fff;font-size:36px;line-height:1;cursor:pointer;opacity:.8}
.lb-x:hover{opacity:1}
.lb-nav{position:absolute;top:50%;transform:translateY(-50%);background:rgba(255,255,255,.14);border:none;color:#fff;font-size:30px;width:54px;height:66px;border-radius:11px;cursor:pointer}
.lb-nav:hover{background:rgba(255,255,255,.26)}
.lb-prev{left:18px}.lb-next{right:18px}
@media(max-width:600px){.lb-nav{width:44px;height:56px;font-size:24px}}
.scene{display:grid;grid-template-columns:52px 1fr;gap:10px;margin-top:11px;padding:11px 13px;background:var(--surface);border:1px solid var(--border);border-radius:10px}
.scene .lab{font-size:11px;font-weight:800;color:var(--muted)}.scene .desc{font-size:13.5px;color:var(--ink2);font-weight:500;line-height:1.62}
.scene.talk{background:var(--card);border-left:3px solid var(--accent)}.scene.talk .lab{color:var(--acci)}.scene.talk .desc{color:var(--ink);font-size:14.5px;line-height:1.72}
details{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-sm);margin-top:12px;overflow:hidden}
summary{cursor:pointer;padding:16px 20px;font-weight:700;font-size:15px;list-style:none;display:flex;gap:10px;align-items:center}summary::-webkit-details-marker{display:none}
summary .arw{margin-left:auto;color:var(--muted);font-size:13px}details[open] summary .arw{transform:rotate(90deg)}
details .in{padding:0 20px 18px;color:var(--ink2);font-size:14px}details ol,details ul{padding-left:20px;display:flex;flex-direction:column;gap:6px}
.rv{background:var(--card);border:1px solid var(--border);border-left:3px solid var(--warn);border-radius:12px;padding:16px 18px;margin-top:12px}
.rv-h{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.rv-n{font-size:12px;font-weight:800;color:var(--warn);background:var(--ww);padding:3px 9px;border-radius:6px}
.rv-tc{font-size:12px;font-weight:700;color:var(--acci)}.rv-b{font-size:13.5px;font-weight:700;color:var(--ink)}
.rv-say{font-size:14px;color:var(--ink2);font-weight:500;margin-top:9px;line-height:1.65}
.rv-ox{font-size:13px;color:var(--ink2);font-weight:600;margin-top:11px;padding-top:10px;border-top:1px dashed var(--border);display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.rv-ox label{display:inline-flex;gap:5px;align-items:center;cursor:pointer}
.rv-ox input[type=radio]{accent-color:var(--accent);width:16px;height:16px}
.rv-fix{flex:1;min-width:180px;font-family:var(--font);font-size:13px;padding:8px 11px;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--ink)}
.rv-tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px}
.btn{font-family:var(--font);font-weight:700;font-size:14px;padding:10px 18px;border-radius:10px;border:1px solid var(--border);background:var(--card);color:var(--ink);cursor:pointer}
.btn.pri{background:var(--accent);color:#fff;border-color:transparent}
.rv-status{font-size:12.5px;color:var(--muted);font-weight:600}
.rv.done-o{border-left-color:var(--good)}.rv.done-x{border-left-color:var(--danger)}
.rv-flag{font-size:10.5px;font-weight:800;color:var(--acci);background:var(--accw);padding:2px 7px;border-radius:5px}
.disc{max-width:var(--maxw);margin:32px auto 0;padding:18px 20px;background:var(--surface2);border-radius:var(--radius-sm);font-size:12.5px;color:var(--muted);font-weight:500;line-height:1.7}
footer{padding:48px 24px;border-top:1px solid var(--border);color:var(--muted);text-align:center;font-size:13px;margin-top:20px}
html{scroll-behavior:smooth}@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
.ev-op{font-size:13px;color:var(--ink2);font-weight:600;margin-top:9px;line-height:1.6}
.ev-src{font-size:12px;color:var(--muted);font-weight:600;margin-top:6px}
.ev-src b{color:var(--ink2)}
.vb{font-size:12px;font-weight:800;padding:3px 9px;border-radius:6px}
.vb.OK{background:var(--gw);color:var(--good)}.vb.PARTIAL{background:var(--ww);color:var(--warn)}
.vb.EXTERNAL{background:var(--accw);color:var(--acci)}.vb.UNVERIFIED{background:var(--surface2);color:var(--muted)}
.imgs{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin-top:16px}
.imgc{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden;box-shadow:var(--shadow)}
.imgc img{display:block;width:100%;height:130px;object-fit:cover;background:var(--surface2)}
.imgc .cap{padding:9px 11px;font-size:11.5px;color:var(--ink2);font-weight:600}
.imgc .lic{font-size:10.5px;font-weight:800;color:var(--warn);background:var(--ww);padding:2px 6px;border-radius:5px;display:inline-block;margin-top:5px}
.vplan{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:13px 15px;margin-top:10px}
.vplan .vp-h{font-size:13px;font-weight:800;color:var(--acci)}
.vplan .vp-p{font-size:12.5px;color:var(--ink2);font-weight:500;margin-top:6px;background:var(--surface);border-radius:8px;padding:9px 11px;line-height:1.55;white-space:pre-wrap}
.vplan .vp-l{font-size:12px;font-weight:700;margin-top:8px;display:inline-flex;gap:5px}
"""

def esc(s): return html.escape(str(s))

# 원장이 '정확히 검수해야 할 부분'을 강조: 수치·비교 데이터 주장 + 효과 단정어(흔한 단어는 제외해 오탐↓)
_EMPH_KW = ("호전","개선","완치","효과","부작용","정상입니다","치료됩니다","낫습니다","사라집니다")
def _emph(text):
    t = esc(text)
    # 데이터 주장: 단위 붙은 수치 / 54→2 / 176 대 208
    t = re.sub(r"(\d+(?:\.\d+)?\s*(?:점|dB|데시벨|%|배|회|mm|주|개월|명|례|kHz|헤르츠))", r"<b>\1</b>", t)
    t = re.sub(r"(\d+(?:\.\d+)?\s*(?:→|->|~|대)\s*\d+(?:\.\d+)?)", r"<b>\1</b>", t)
    for kw in _EMPH_KW:
        t = t.replace(kw, f"<b>{kw}</b>")
    return t

# ── 편집 오버레이(대사 ✏️수정 + 장면 AI사진 다시/이전/업로드) — 예쁜 스토리보드는 그대로, 편집만 얹음 ──
_ED_CSS = """
.stage .sidecol{max-width:none;flex:1 1 340px;flex-direction:row;flex-wrap:wrap;align-items:flex-start;gap:14px}
.stage .sidecol .refthumbs{flex:0 0 auto}
.stage .sidecol .ai-box{flex:1 1 260px}
.ai-img{max-width:100%}
.scene.talk{display:block}
.scene.talk .lab{display:block;margin-bottom:5px}
.ed-btn{font-size:11px;padding:2px 9px;border-radius:6px;border:1px solid var(--border);background:var(--card);color:var(--acci);cursor:pointer;margin-left:8px;vertical-align:middle;font-weight:700}
.ed-f{margin-top:8px;width:100%}
.ed-f textarea{display:block;width:100%;min-height:110px;font-family:inherit;font-size:14.5px;line-height:1.7;padding:11px 13px;border:1px solid var(--border);border-radius:9px;background:var(--card);color:var(--ink);box-sizing:border-box;resize:vertical}
.eb{font-size:12px;font-weight:700;padding:6px 12px;border-radius:8px;border:1px solid transparent;background:#3182f6;color:#fff;cursor:pointer}
.eb.g{background:var(--card);color:var(--ink);border:1px solid var(--border)}
.ai-box{border:1px solid var(--border);border-radius:12px;padding:12px 13px;background:var(--surface)}
.ai-box .pb-h{font-size:13px;font-weight:800;color:var(--acci);margin-bottom:6px}
.ai-img{width:100%;max-width:190px;border-radius:8px;display:block;cursor:zoom-in}
.ai-none{height:100px;border:1px dashed var(--border);border-radius:8px;display:grid;place-items:center;color:var(--muted);font-size:12px}
.ai-r{display:flex;gap:5px;flex-wrap:wrap;margin-top:6px}
"""
_ED_JS = """<script>
function edE(k){document.getElementById('say_'+k).style.display='none';document.getElementById('edf_'+k).style.display='block';}
function edC(k){document.getElementById('edf_'+k).style.display='none';document.getElementById('say_'+k).style.display='inline';}
</script>"""


def _ed_say(say_esc, key, edit):
    """대사: 평소엔 완성문장, ✏️수정 누르면 그 자리서 편집·저장(대시보드 edit 라우트로)."""
    ek = esc(key)
    return (f'<span class="desc" id="say_{ek}">{say_esc}</span>'
            f'<button class="ed-btn" type="button" onclick="edE(\'{ek}\')">✏️ 수정</button>'
            f'<form class="ed-f" id="edf_{ek}" method="post" action="{esc(edit["edit_url"])}" style="display:none">'
            f'{edit["csrf"]}{edit["rt"]}<input type="hidden" name="expected" value="{esc(edit["version_id"])}">'
            f'<textarea name="edit__{ek}">{say_esc}</textarea>'
            f'<div style="margin-top:7px;display:flex;gap:6px"><button class="eb" type="submit">💾 저장</button>'
            f'<button class="eb g" type="button" onclick="edC(\'{ek}\')">취소</button></div></form>')


def _ed_img(key, edit):
    """이 장면의 AI 사진 + 🎨 AI 다시 · ↩ 이전 · ⬆ 내 사진 업로드(논문 그림 썸네일과 나란히)."""
    ek = esc(key); csrf = edit["csrf"]; rt = edit["rt"]
    if edit["has_img"](key):
        u = esc(edit["img_url"](key))
        img = f'<a href="{u}" target="_blank"><img class="ai-img" src="{u}" alt="AI 사진"></a>'
    else:
        img = '<div class="ai-none">AI 사진 없음 — 만들거나 올리세요</div>'
    regen = (f'<form method="post" action="{esc(edit["regen_url"](key))}" style="margin:0">{csrf}{rt}'
             f'<button class="eb g" onclick="this.innerHTML=\'생성중…\'">🎨 AI 다시</button></form>')
    revert = ((f'<form method="post" action="{esc(edit["revert_url"](key))}" style="margin:0">{csrf}{rt}'
               f'<button class="eb g">↩ 이전</button></form>') if edit["has_prev"](key) else "")
    upload = (f'<form method="post" action="{esc(edit["upload_url"](key))}" enctype="multipart/form-data" style="margin-top:6px">{csrf}{rt}'
              f'<input type="file" name="photo" accept="image/*" style="font-size:11px;max-width:170px">'
              f'<button class="eb g" style="margin-top:4px">⬆ 내 사진 올리기</button></form>')
    return f'<div class="ai-box"><div class="pb-h">🖼 이 장면 AI 사진</div>{img}<div class="ai-r">{regen}{revert}</div>{upload}</div>'


def render(pkg, meta=None, evidence=None, images=None, edit=None):
    meta = meta or {}
    script = pkg.get("script", [])
    say_chars = sum(len((b.get("say") or "").replace(" ","")) for b in script)
    mins = round(say_chars/450, 1)
    crit_words = ("응급","하지 말","119")

    beat_figs = (images or {}).get("beat_figures", {})
    plan_by_tc = {p.get("tc",""): p for p in (images or {}).get("plans", [])}
    gallery = []   # 라이트박스용 전체 그림 목록(순서대로 넘김)
    beats = ""
    for idx, b in enumerate(script):
        tc = b.get("tc","")
        tc_html = tc.replace("–","<br>").replace(" - ","<br>")
        crit = " crit" if any(w in (b.get("block","")) for w in crit_words) else ""
        tags = "".join(f'<span class="tag{" bad" if "0" in t or "없음" in t else ""}">{esc(t)}</span>' for t in b.get("tags",[]))
        # 이 장면에 매칭된 논문 그림 → 작은 썸네일 줄(클릭 시 라이트박스 확대·넘기기)
        thumbs = ""
        for g in beat_figs.get(str(idx), []):
            gi = len(gallery)
            gallery.append({"src": g.get("src",""), "cap": g.get("caption","")})
            thumbs += f'<img class="rth" src="{esc(g.get("src",""))}" data-i="{gi}" loading="lazy" alt="논문 그림" title="{esc(g.get("caption",""))}">'
        n = len(beat_figs.get(str(idx), []))
        refwrap = f'<div class="refthumbs"><div class="rth-row">{thumbs}</div><div class="rth-lab">📄 논문 그림 {n}장 · 클릭해 확대</div></div>' if thumbs else ""
        # (제거 요청) '이 장면 이미지/AI 생성 프롬프트' planbox 삭제 — 편집화면의 실제 AI 사진 셀로 대체.
        planbox = ""
        # 편집 오버레이: 이 비트↔PG 블록(order_index=idx) 매핑. 대사=PG 현재본, AI 사진 셀 추가.
        say = b.get('say', '')
        ekey = None
        if edit:
            bi = edit["by_idx"].get(idx)
            if bi:
                ekey = bi["key"]; say = bi.get("text") or say
        ai_cell = _ed_img(ekey, edit) if (edit and ekey) else ""
        say_html = _ed_say(esc(say), ekey, edit) if (edit and ekey) else f'<span class="desc">{esc(say)}</span>'
        sidecol = f'<div class="sidecol">{refwrap}{ai_cell}{planbox}</div>' if (thumbs or planbox or ai_cell) else ""
        beats += f"""<div class="beat{crit}"><div class="tc">{esc(tc_html)}</div><div class="body">
          <div class="bt">{esc(b.get('block',''))} {tags}</div>
          <div class="stage">{frame_html(b, esc(tc.split('–')[0].split(' - ')[0].strip()))}{sidecol}</div>
          <div class="scene"><span class="lab">🎬 화면</span><span class="desc">{esc(b.get('scene',''))}</span></div>
          <div class="scene talk"><span class="lab">🎙 대사</span>{say_html}</div>
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

    # 원장 검수 대상 = AI가 【검수】 표시한 씬 + 모든 본론(의학 설명) 블록.
    # 태그가 안 붙어도 검수 공간이 사라지지 않게 본론 블록은 항상 포함.
    MED = ("본론","원인","진단","치료","도침","증례","논문")
    rv = ""
    for i, b in enumerate(script, 1):
        say = b.get("say","") or ""
        block = b.get("block","") or ""
        flagged = "검수" in say
        is_med = flagged or any(k in block for k in MED)
        if not is_med:
            continue
        clean = say.replace("【검수】","").replace("[검수]","").strip()
        badge = '<span class="rv-flag">AI 표시</span>' if flagged else ""
        rv += f"""<div class="rv" data-scene="{i}"><div class="rv-h"><span class="rv-n">씬 {i}</span>
          <span class="rv-tc">{esc(b.get('tc',''))}</span><span class="rv-b">{esc(block)}</span>{badge}</div>
          <div class="rv-say">{_emph(clean)}</div>
          <div class="rv-ox">
            <label><input type="radio" name="rv{i}" value="O"> 맞음(O)</label>
            <label><input type="radio" name="rv{i}" value="X"> 수정 필요(X)</label>
            <input class="rv-fix" type="text" placeholder="수정 내용(선택)">
          </div></div>"""
    review_sec = ""
    if rv:
        review_sec = f"""<section class="sec wrap" id="review"><div class="sec-tag">Doctor Check</div>
        <h2>원장 검수 — 촬영 전 O/X</h2>
        <p class="lead">AI가 <b>AI 표시</b>한 문장과 모든 <b>본론(의학 설명)</b> 블록입니다 — 원장님 임상 판단이 걸린 부분. 맞으면 O, 다르면 X 하고 수정 내용을 적으세요. <b>저장</b>하면 이 브라우저에 남고, <b>내보내기</b>로 결과 파일을 팀에 보낼 수 있어요.</p>
        {rv}
        <div class="rv-tools"><button class="btn pri" id="rvSave">💾 저장</button><button class="btn" id="rvExport">⬇ 내보내기(.txt)</button><span class="rv-status" id="rvStatus"></span></div>
        </section>"""

    # ── 논문 근거 · 자동 1차 검수 (evidence/check.py 결과) ──
    ev_sec = ""
    if evidence:
        vicon = {"OK":"✅ 원문 확인","PARTIAL":"⚠️ 부분확인","EXTERNAL":"🔎 원문 미확인","UNVERIFIED":"❔ 확인필요"}
        evr = ""
        for i, r in enumerate(evidence, 1):
            v = r.get("verdict","UNVERIFIED")
            src = r.get("source_name") or (r.get("source") or "—")
            nf = ", ".join(r.get("nums_found",[])); nm = ", ".join(r.get("nums_missing",[]))
            srcline = (f'<div class="ev-src">출처: <b>{esc(src)}</b>'
                       + (f' · 확인수치 {esc(nf)}' if nf else "")
                       + (f' · <span style="color:var(--warn)">미확인 {esc(nm)}</span>' if nm else "") + '</div>')
            evr += f"""<div class="rv" data-scene="ev{i}"><div class="rv-h">
              <span class="vb {v}">{vicon.get(v,v)}</span><span class="rv-b">근거 {i}</span></div>
              <div class="rv-say">{esc(r.get('claim',''))}</div>{srcline}
              <div class="ev-op">▶ 1차 의견: {esc(r.get('opinion',''))}</div>
              <div class="rv-ox">
                <label><input type="radio" name="ev{i}" value="O"> 확인·동의</label>
                <label><input type="radio" name="ev{i}" value="X"> 재확인 필요</label>
                <input class="rv-fix" type="text" placeholder="원장 메모(선택)"></div></div>"""
        ev_sec = f"""<section class="sec wrap" id="evidence-check"><div class="sec-tag">Evidence · 자동 1차 검수</div>
        <h2>논문 근거 대조 — 1차 검수</h2>
        <p class="lead">대본이 인용한 수치·출처를 <b>업로드된 원문 논문</b>과 자동 대조한 결과입니다. 기계는 <b>인용 실재·수치 일치</b>까지만 봅니다 — <b>의학적 타당성 판단은 원장님 몫</b>이라, 각 근거를 확인하고 동의/재확인을 표시하세요.</p>
        {evr}</section>"""

    # ── 시각자료(논문 추출 이미지 + 장면별 AI프롬프트·구매링크) ──
    img_sec = ""
    if images:
        _zhref = images.get("zip_datauri") or images.get("zip")   # 임베드(data URI) 우선 → 어디서 열든 다운로드됨
        _zname = images.get("zip_name") or "images.zip"
        zipline = (f'<p class="lead" style="margin-top:14px">📦 <a href="{esc(_zhref)}" download="{esc(_zname)}">이미지 zip 다운로드</a> — 추출한 논문 그림(원본) + 장면별 AI프롬프트·구매링크 매니페스트</p>' if _zhref else "")
        fc = images.get("fig_count", 0); mc = images.get("matched_count", 0); pc = len(images.get("plans", []))
        img_sec = f"""<section class="sec wrap" id="assets"><div class="sec-tag">Visual Assets</div>
        <h2>시각자료 — 어디에 붙였나</h2>
        <p class="lead">시각자료는 전부 <b>대본의 해당 장면 옆</b>에 붙였어요:</p>
        <p class="lead">· 📄 논문 그림 <b>{fc}장</b> 추출 → 관련 <b>{mc}장</b>을 근거 장면 옆 썸네일로(클릭하면 크게·넘기기)<br>
        · 🎬 논문에 없지만 그림이 필요한 <b>{pc}장면</b> → 그 장면 옆에 <b>AI 생성 프롬프트(복사) + 구매·검색 링크</b><br>
        · 나머지(콜드오픈·인트로·실연·응급·CTA 등)는 원장 정면/실연이라 <b>스토리보드로 충분</b> — 별도 이미지 불필요</p>
        <p class="lead" style="font-size:13px;color:var(--muted)">※ 논문 추출본은 참고용 — 영상 사용엔 학회지·환자 동의 확인 필요.</p>
        {zipline}</section>"""

    title = pkg.get("episode_title","본큐어 유튜브 패키지")
    host = meta.get("host","송정현")
    files_n = meta.get("files_n","—")
    kb_n = meta.get("kb_n","—")

    _rvjs = r"""<script>(function(){
  var SAVE=document.getElementById('rvSave'); if(!SAVE) return;
  var KEY=__KEY__, status=document.getElementById('rvStatus');
  function collect(){var out={};document.querySelectorAll('.rv').forEach(function(el){
    var s=el.getAttribute('data-scene');
    var r=el.querySelector('input[type=radio]:checked');var ox=r?r.value:'';
    var f=el.querySelector('.rv-fix');var fix=f?f.value:'';
    out[s]={ox:ox,fix:fix};
    el.classList.toggle('done-o',ox==='O');el.classList.toggle('done-x',ox==='X');});return out;}
  function apply(d){Object.keys(d||{}).forEach(function(s){
    var el=document.querySelector('.rv[data-scene="'+s+'"]');if(!el)return;var v=d[s]||{};
    if(v.ox){var r=el.querySelector('input[value="'+v.ox+'"]');if(r)r.checked=true;}
    var f=el.querySelector('.rv-fix');if(f&&v.fix)f.value=v.fix;
    el.classList.toggle('done-o',v.ox==='O');el.classList.toggle('done-x',v.ox==='X');});}
  try{var sv=localStorage.getItem(KEY);if(sv)apply(JSON.parse(sv));}catch(e){}
  document.querySelectorAll('.rv input').forEach(function(i){i.addEventListener('change',collect);});
  SAVE.addEventListener('click',function(){var d=collect();
    try{localStorage.setItem(KEY,JSON.stringify(d));status.textContent='저장됨 · '+new Date().toLocaleTimeString('ko-KR');}
    catch(e){status.textContent='이 브라우저에선 저장이 막혀 있어요 — 내보내기를 쓰세요.';}});
  document.getElementById('rvExport').addEventListener('click',function(){var d=collect();
    var L=['[원장 검수 결과] '+__TITLE__,''];
    document.querySelectorAll('.rv').forEach(function(el){var s=el.getAttribute('data-scene');var v=d[s]||{};
      var say=(el.querySelector('.rv-say')||{}).textContent||'';
      L.push('씬 '+s+' — '+(v.ox||'미정')+(v.fix?(' / 수정: '+v.fix):''));L.push('  '+say.trim());L.push('');});
    var b=new Blob([L.join('\n')],{type:'text/plain;charset=utf-8'});
    var a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=__FN__;a.click();});
})();</script>"""
    _rvjs = (_rvjs.replace("__KEY__", json.dumps("boncure_rv_"+title))
                  .replace("__TITLE__", json.dumps(title))
                  .replace("__FN__", json.dumps("원장검수_"+title.replace(" ","_").replace("/","_")+".txt")))
    lb_html = ('<div id="lb" class="lb"><button class="lb-x" aria-label="닫기">&times;</button>'
               '<button class="lb-nav lb-prev" aria-label="이전">&lsaquo;</button>'
               '<img alt="논문 그림 확대"><button class="lb-nav lb-next" aria-label="다음">&rsaquo;</button>'
               '<div class="lb-cap"></div></div>') if gallery else ""
    lb_js = ""
    if gallery:
        lb_js = r"""<script>(function(){
  var G=__G__,lb=document.getElementById('lb');if(!lb)return;
  var im=lb.querySelector('img'),cap=lb.querySelector('.lb-cap'),cur=0;
  function show(i){cur=(i+G.length)%G.length;im.src=G[cur].src;cap.textContent='📄 '+G[cur].cap+' · 참고용(라이선스 확인) — '+(cur+1)+'/'+G.length;}
  function open(i){show(i);lb.classList.add('on');}
  function close(){lb.classList.remove('on');im.src='';}
  document.querySelectorAll('.rth').forEach(function(t){t.addEventListener('click',function(){open(+t.dataset.i);});});
  lb.querySelector('.lb-x').addEventListener('click',close);
  lb.querySelector('.lb-prev').addEventListener('click',function(e){e.stopPropagation();show(cur-1);});
  lb.querySelector('.lb-next').addEventListener('click',function(e){e.stopPropagation();show(cur+1);});
  lb.addEventListener('click',function(e){if(e.target===lb)close();});
  document.addEventListener('keydown',function(e){if(!lb.classList.contains('on'))return;
    if(e.key==='Escape')close();else if(e.key==='ArrowLeft')show(cur-1);else if(e.key==='ArrowRight')show(cur+1);});
})();</script>""".replace("__G__", json.dumps(gallery))

    plan_js = ""
    if plan_by_tc:
        plan_js = r"""<script>document.querySelectorAll('.pb-copy').forEach(function(btn){btn.addEventListener('click',function(e){e.preventDefault();e.stopPropagation();var d=btn.closest('.pb-d');var p=d&&d.querySelector('.pb-p');if(!p)return;function ok(){var o=btn.textContent;btn.textContent='복사됨';setTimeout(function(){btn.textContent='복사';},1200);}if(navigator.clipboard){navigator.clipboard.writeText(p.textContent).then(ok,function(){});}else{var ta=document.createElement('textarea');ta.value=p.textContent;document.body.appendChild(ta);ta.select();try{document.execCommand('copy');ok();}catch(err){}document.body.removeChild(ta);}});});</script>"""

    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 본큐어 유튜브</title><style>{CSS}{_ED_CSS if edit else ''}</style></head><body>
<nav class="nav"><div class="nav-in"><div class="brand"><span class="dot">본</span>본큐어 유튜브 · 대본 패키지</div>
<div class="nav-links"><a href="#hook">기획</a><a href="#script">대본</a><a href="#deliverables">산출물</a>{'<a href="#evidence-check">근거 검수</a>' if evidence else ''}<a href="#review">원장 검수</a></div>
<button class="toggle" id="tg" aria-label="테마 전환">◐</button></div></nav>
<header class="hero wrap"><span class="eyebrow">Episode Package · 콘텐츠 디렉터 시스템</span>
<h1>{esc(title)}</h1>
<p class="sub">{esc(meta.get('tagline','구조가 답입니다. 뼈가 웃어야 인생이 웃습니다.'))}</p>
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
{ev_sec}
{img_sec}
{review_sec}
<div class="disc"><b>⚠️ 이 대본은 자동 컴플라이언스 검사를 거쳤지만, 검사 통과가 발행을 보장하지 않습니다. 최종 발행은 반드시 원장 의학 검수와 의료광고 심의 확인을 마친 뒤에만 진행하세요.</b><br>대본 속 의학 정보는 교육·정보 제공 목적이며, 효과는 개인마다 다르고 부작용 가능성이 있습니다. 논문 증례는 단일 증례일 수 있으며 모든 환자에게 동일 결과를 보장하지 않습니다.</div>
<footer>본큐어 유튜브 · 화자 {esc(host)}</footer>
<script>(function(){{var r=document.documentElement,b=document.getElementById('tg');function c(){{return r.getAttribute('data-theme')||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light')}}b.addEventListener('click',function(){{r.setAttribute('data-theme',c()==='dark'?'light':'dark')}})}})();</script>
{lb_html}
{lb_js}
{plan_js}
{_rvjs}
{_ED_JS if edit else ''}
</body></html>"""

def _meta(hospital="boncure"):
    """병원별 corpus/kb 개수 + config(호칭·tagline)를 모아 스탯/부제에 넣는다."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    m = {"host":"원장"}
    try:
        import yaml
        cfg = yaml.safe_load(open(os.path.join(root,"config",f"{hospital}.yaml"),encoding="utf-8"))
        h = cfg.get("hospital",{})
        m["host"] = h.get("host", m["host"])
        if h.get("tagline"): m["tagline"] = h["tagline"]
    except Exception:
        pass
    man = os.path.join(root,"data",hospital,"corpus","_MANIFEST.tsv")
    if os.path.exists(man):
        m["files_n"] = max(0, len(open(man,encoding="utf-8").read().splitlines())-1)
    kb = os.path.join(root,"data",hospital,"kb")
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
