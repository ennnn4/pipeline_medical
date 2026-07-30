"""
시각자료 빌더 (Phase 2) — 논문 그림 추출 + 장면별 AI프롬프트·구매링크 + 이미지 zip.

 · 업로드된 '논문 PDF'(raw/ 및 zip 내부)에서 그림을 추출 → 사진/그래프 분류 → 썸네일 data URI
 · 대본 비트 중 '시각자료가 필요한' 장면 → AI 생성 프롬프트 + 스톡 구매/검색 링크
 · 원본 그림 + 매니페스트를 out/<topic>_images.zip 으로 묶음
 · 결과를 <pkg>.assets.json 으로 저장 → render 가 '시각자료' 섹션으로 표시

주의: 추출본은 '참고용'. 저작권(학회지)·환자 동의는 원장 확인 후에만 영상 사용.
생성·결제는 사람 몫 — 여기선 프롬프트·링크까지만.
"""
import os, re, io, glob, json, zipfile, base64, urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _looks_paper(path):
    p = path.replace("\\", "/").lower()
    if not p.endswith(".pdf"): return False
    base = p.rsplit("/", 1)[-1]
    if any(b in base for b in ("강좌","강의","해부","설문","프롬프트","근거표","대본","체크","요청","브랜딩")):
        return False
    return ("논문" in p) or bool(re.search(r"(19|20)\d{2}", base)) or bool(re.match(r"\d{2}_", base))

def _paper_sources(hospital):
    """raw/ 의 논문 PDF들을 (라벨, 바이트)로 모은다 — zip 내부 포함."""
    raw = os.path.join(ROOT, "data", hospital, "raw")
    out = []
    for p in glob.glob(raw + "/**/*", recursive=True):
        if os.path.isfile(p) and _looks_paper(p):
            out.append((os.path.basename(p), io.open(p, "rb").read()))
    for z in glob.glob(raw + "/**/*.zip", recursive=True):
        try:
            zf = zipfile.ZipFile(z)
            for n in zf.namelist():
                if _looks_paper(n):
                    out.append((os.path.basename(n), zf.read(n)))
        except Exception:
            pass
    return out

def _classify(pix):
    """사진(연속톤=색 많음) vs 그래프/도표(단순) 추정."""
    s = pix.samples; n = pix.n; step = max(1, len(s)//3000); cols = set()
    for i in range(0, len(s)-n, n*step):
        cols.add(bytes(s[i:i+3]))
    return "📷 사진(추정)" if len(cols) > 200 else "📊 그래프·도표"

def extract_figures(hospital, cap=12):
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("  · PyMuPDF 없음 — 논문 그림 추출 건너뜀 (pip install pymupdf)"); return []
    figs, seen_full = [], []
    for label, data in _paper_sources(hospital):
        try: doc = fitz.open(stream=data, filetype="pdf")
        except Exception: continue
        seen = set()
        for pg in doc:
            for im in pg.get_images(full=True):
                xref = im[0]
                if xref in seen: continue
                seen.add(xref)
                try: pix = fitz.Pixmap(doc, xref)
                except Exception: continue
                if pix.width < 200 or pix.height < 200: continue
                if pix.n - pix.alpha >= 4 or pix.alpha:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                kind = _classify(pix)
                full = pix.tobytes("png")
                thumb = pix
                while thumb.width > 480: thumb.shrink(1)   # 대시보드 임베드용 썸네일(경량화)
                try: tb = thumb.tobytes("jpeg", jpg_quality=68)
                except Exception:
                    try: tb = thumb.tobytes("jpeg")
                    except Exception: tb = thumb.tobytes("png")
                mime = "jpeg" if tb[:2] == b"\xff\xd8" else "png"
                figs.append({"caption": label[:34], "kind": kind,
                             "src": f"data:image/{mime};base64," + base64.b64encode(tb).decode(),
                             "license_link": "https://www.google.com/search?q=" + urllib.parse.quote(os.path.splitext(label)[0]),
                             "_full": full, "_name": f"fig{len(figs)+1:02d}.png"})
                if len(figs) >= cap: return figs
    return figs

# 시각자료가 필요한 장면(단순 원장 토킹 말고 그래픽/일러스트/사진 등)만 계획 생성
_NEED = ("그래픽","일러스트","애니","그래프","사진","이미지","도식","다이어그램","차트","프리뷰","cg","자막카드","인포그래픽","모식도")

def visual_plans(pkg):
    plans = []
    for b in pkg.get("script", []):
        scene = (b.get("scene","") or "")
        if not any(k in scene.lower() for k in _NEED):
            continue
        kw = re.sub(r"[^\w가-힣 ]", " ", (b.get("block","") + " " + scene))[:60]
        prompt = ("의학 유튜브용 이미지. 장면: " + scene.strip()
                  + " — 스타일: 깔끔한 한국어 의료 인포그래픽/일러스트, 사실적이되 환자 식별정보·워터마크 없음, 16:9, 고해상도.")
        buy = "https://www.shutterstock.com/search/" + urllib.parse.quote(kw.strip().replace(" ", "-"))
        plans.append({"tc": b.get("tc",""), "block": b.get("block",""), "prompt": prompt, "buy_link": buy})
    return plans

def build_zip(pkg_path, figs, plans):
    base = os.path.splitext(pkg_path)[0]
    topic = os.path.basename(base).replace("_package", "")
    zpath = base.rsplit("_package", 1)[0] + "_images.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        man = ["[시각자료 매니페스트] " + topic, "", "■ 논문 추출 그림 (참고용 · 라이선스/동의 확인 후 사용):"]
        for f in figs:
            z.writestr("figures/" + f["_name"], f["_full"])
            man.append(f"  - {f['_name']} · {f['caption']} · {f['kind']}")
        man += ["", "■ AI 생성/구매 필요 장면:"]
        for p in plans:
            man.append(f"  - {p['tc']} {p['block']}")
            man.append(f"      AI프롬프트: {p['prompt']}")
            man.append(f"      구매/검색: {p['buy_link']}")
        z.writestr("manifest.txt", "\n".join(man))
    return os.path.basename(zpath)

def run(hospital, pkg_path):
    pkg = json.load(io.open(pkg_path, encoding="utf-8"))
    print("시각자료 추출 중…")
    figs = extract_figures(hospital)
    plans = visual_plans(pkg)
    zipname = build_zip(pkg_path, figs, plans) if (figs or plans) else None
    # render 용(무거운 _full/_name 제거)
    fig_light = [{k: v for k, v in f.items() if not k.startswith("_")} for f in figs]
    data = {"figures": fig_light, "plans": plans, "zip": zipname}
    outp = os.path.splitext(pkg_path)[0] + ".assets.json"
    json.dump(data, io.open(outp, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"  · 논문 그림 {len(figs)}장 · 장면 계획 {len(plans)}건 · zip {zipname}")
    return data
