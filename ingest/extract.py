"""
1단계 수집·정규화 + 입력 자료 체크리스트 리포트.
표준 라이브러리 + pdftotext(외부 바이너리)만으로 동작. (pyyaml만 필요)

사용: python -m ingest.extract --hospital boncure
동작:
  1) config의 input_checklist 로 data/raw/ 를 스캔 → 받은 것/빠진 것 리포트 (required 빠지면 경고)
  2) pdf/docx/txt/hwp/zip → data/corpus/*.txt 로 정규화
  3) data/corpus/_MANIFEST.tsv (cid, category, size, source) 기록
"""
import os, re, sys, zipfile, subprocess, glob, argparse, io
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def load_config(hospital):
    import yaml
    with open(os.path.join(ROOT, "config", f"{hospital}.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)

def categorize(name, checklist):
    low = name.lower()
    for item in checklist:
        for m in (item.get("match") or []):
            if m.lower() in low:
                return item["key"]
    return "기타"

def docx_text(path):
    try:
        z = zipfile.ZipFile(path)
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    except Exception as e:
        return f"[docx 파싱 실패: {e}]"
    xml = xml.replace("</w:p>", "\n")
    xml = re.sub(r"<w:tab/>", "\t", xml)
    t = re.sub(r"<[^>]+>", "", xml)
    for a, b in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")]:
        t = t.replace(a, b)
    return t

def pdf_text(path):
    out = path + ".__tmp.txt"
    r = subprocess.run(["pdftotext", "-enc", "UTF-8", path, out], capture_output=True)
    if os.path.exists(out):
        data = io.open(out, encoding="utf-8", errors="ignore").read()
        os.remove(out)
        return data
    return f"[pdftotext 실패: {r.stderr.decode('utf-8','ignore')[:200]}]"

def hwp_text(path):
    # 선택: pyhwp 설치 시 hwp5txt 사용
    try:
        r = subprocess.run(["hwp5txt", path], capture_output=True)
        if r.returncode == 0:
            return r.stdout.decode("utf-8", "ignore")
    except FileNotFoundError:
        pass
    return "[hwp 추출 불가 — 'pip install pyhwp' 후 재시도]"

def pptx_text(path):
    """pptx(강의자료) 텍스트 추출 — 슬라이드 XML의 <a:t> 조각을 이어붙임."""
    try:
        z = zipfile.ZipFile(path)
        slides = sorted([n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)],
                        key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        parts = []
        for s in slides:
            xml = z.read(s).decode("utf-8", "ignore")
            parts.append(" ".join(re.findall(r"<a:t>(.*?)</a:t>", xml, re.S)))
        t = "\n".join(parts)
        for a, b in [("&amp;","&"),("&lt;","<"),("&gt;",">"),("&quot;",'"'),("&#39;","'")]:
            t = t.replace(a, b)
        return t
    except Exception as e:
        return f"[pptx 파싱 실패: {e}]"

def _safe_extractall(zf, dst):
    """zip-slip 방지: 각 멤버가 dst 밖으로 나가면 무시."""
    dst = os.path.abspath(dst)
    for m in zf.namelist():
        target = os.path.abspath(os.path.join(dst, m))
        if target != dst and not target.startswith(dst + os.sep):
            print(f"  ! 위험 경로 무시(zip): {m}")
            continue
        zf.extract(m, dst)

def extract_one(path):
    ext = path.lower().rsplit(".", 1)[-1]
    if ext == "pdf":  return pdf_text(path)
    if ext == "docx": return docx_text(path)
    if ext == "pptx": return pptx_text(path)
    if ext == "hwp":  return hwp_text(path)
    if ext in ("txt", "md", "csv"):
        return io.open(path, encoding="utf-8", errors="ignore").read()
    return None  # 이미지 등은 스킵(추후 OCR)

def gather_files(raw_dir):
    """zip 은 풀어서 내부 파일까지 포함."""
    files = []
    for p in glob.glob(raw_dir + "/**/*", recursive=True):
        if not os.path.isfile(p): continue
        if p.lower().endswith(".zip"):
            dst = p[:-4] + "_unzipped"
            os.makedirs(dst, exist_ok=True)
            try:
                _safe_extractall(zipfile.ZipFile(p), dst)
            except Exception as e:
                print(f"  ! zip 풀기 실패 {os.path.basename(p)}: {e}")
            for q in glob.glob(dst + "/**/*", recursive=True):
                if os.path.isfile(q) and not q.lower().endswith(".zip"):
                    files.append(q)
        else:
            files.append(p)
    return files

def run(hospital):
    cfg = load_config(hospital)
    checklist = cfg.get("input_checklist", [])
    raw = os.path.join(ROOT, "data", hospital, "raw")       # 병원별 폴더
    corpus = os.path.join(ROOT, "data", hospital, "corpus")
    os.makedirs(raw, exist_ok=True)
    os.makedirs(corpus, exist_ok=True)

    files = gather_files(raw)

    # ── 체크리스트 리포트 (시작 화면) ───────────────────────────
    found = {item["key"]: [] for item in checklist}
    found["기타"] = found.get("기타", [])
    for p in files:
        cat = categorize(os.path.relpath(p, raw), checklist)  # 폴더 경로까지 보고 분류
        found.setdefault(cat, []).append(p)

    print("=" * 60)
    print(f"[{cfg['hospital']['name']}] 입력 자료 체크리스트")
    print("=" * 60)
    missing_required = []
    for item in checklist:
        k = item["key"]; n = len(found.get(k, []))
        req = item.get("required")
        mark = "✅" if n else ("❌" if req else "⬜")
        tag = " (필수)" if req else ""
        print(f"  {mark} {k}{tag}: {n}개")
        if req and n == 0:
            missing_required.append(k)
    other = len(found.get("기타", []))
    if other:
        print(f"  ⬜ 기타(미분류): {other}개")
    if missing_required:
        print("\n⚠️ 필수 자료 누락:", ", ".join(missing_required))
        print("   → 이 자료 없이도 진행은 되지만 KB 품질이 떨어집니다. 받아서 data/raw/ 에 추가 권장.")
    print("=" * 60)

    # ── 정규화 ────────────────────────────────────────────────
    manifest = []
    i = 0
    for p in files:
        text = extract_one(p)
        if text is None:
            continue
        i += 1
        cid = f"c{i:03d}"
        cat = categorize(os.path.relpath(p, raw), checklist)
        io.open(os.path.join(corpus, cid + ".txt"), "w", encoding="utf-8").write(text)
        rel = os.path.relpath(p, raw).replace("\\", "/")
        manifest.append((cid, cat, len(text), rel))

    with io.open(os.path.join(corpus, "_MANIFEST.tsv"), "w", encoding="utf-8") as f:
        f.write("cid\tcategory\tchars\tsource\n")
        for row in manifest:
            f.write("\t".join(str(x) for x in row) + "\n")

    print(f"정규화 완료: {len(manifest)}개 → data/corpus/  (매핑: _MANIFEST.tsv)")
    return {"found": {k: len(v) for k, v in found.items()}, "missing_required": missing_required, "count": len(manifest)}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hospital", default="boncure")
    a = ap.parse_args()
    run(a.hospital)
