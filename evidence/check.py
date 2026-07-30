"""
논문 근거 대조 — 자동 1차 검수.
대본(package.json)이 인용한 수치·출처를 '업로드된 논문 원문(corpus)'과 대조해서
 · 자료로 확인됨 / 부분확인 / 자료에 없음(외부논문) 을 판정하고
 · 원장 최종 검수용 '1차 검수 의견'을 초안으로 써준다.

핵심 철학: 자동화는 '인용이 진짜냐·수치가 자료에 있냐'까지만.
'이 근거로 이 말 해도 의학적으로 타당하냐'는 원장 몫 → 그래서 결과는 '의견 초안'이지 통과보증이 아님.

CLI: python run.py evidence --hospital boncure --file data/boncure/out/이명_package.json
"""
import os, re, io, glob, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _norm(s):  # 공백 제거 비교(원문 줄바꿈/띄어쓰기 편차 흡수)
    return re.sub(r"\s+", "", s or "")

def _is_primary_paper(src_path):
    """진짜 '원문 논문'인가? (논문 폴더의 PDF). 병원이 정리한 근거표·대본·설문 docx는 제외 —
    거기서 인용을 찾는 건 순환참조(병원이 적어놓은 걸 병원 자료로 확인)라 원문 검증이 아님."""
    p = (src_path or "").replace("\\", "/").lower()
    if not p.endswith(".pdf"): return False
    if "/논문/" in p or p.startswith("논문/"): return True
    bad = ("근거표", "대본", "설문", "체크리스트", "요청사항", "프롬프트", "링크")
    return not any(b in p for b in bad)

def corpus_blobs(hospital):
    """corpus/*.txt → {cid: (원문, 정규화원문)}, cid→원본경로, 원문논문 cid집합."""
    d = os.path.join(ROOT, "data", hospital, "corpus")
    out = {}
    for p in glob.glob(os.path.join(d, "*.txt")):
        t = io.open(p, encoding="utf-8", errors="ignore").read()
        out[os.path.basename(p)] = (t, _norm(t))
    src, papers = {}, set()
    man = os.path.join(d, "_MANIFEST.tsv")
    if os.path.exists(man):
        for line in io.open(man, encoding="utf-8").read().splitlines()[1:]:
            c = line.split("\t")
            if len(c) >= 4:
                cid = c[0] + ".txt"; src[cid] = c[3]
                if _is_primary_paper(c[3]): papers.add(cid)
    return out, src, papers

# 대조할 '검증 토큰' 추출 규칙 ---------------------------------------------------
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_VOL  = re.compile(r"\d+\(\d+\)")                    # 9(1) 형태 권(호)
_KNAME= re.compile(r"[가-힣]{2,4}(?=\s*(?:등|외|,|\())")  # 김성은 등 / 송정현 공저
_ENAME= re.compile(r"[A-Z][a-zçöüéáA-Z]{2,}")        # 영문 성(Bulut, Zaccaro)
_JWORD= {"Medical","Science","Monitor","Journal","Frontiers","Human","Neuroscience",
         "Surgical","Technology","International","Van","THI","NRS","HRV"}  # 저널어·지표(성 아님)
_NUM  = re.compile(r"\d+\.?\d*")

def claim_tokens(text):
    """한 근거 문장에서 대조 대상 토큰을 뽑는다. 저자명은 '—' 앞(출처부)에서만."""
    head = text.split("—")[0]
    years  = set(m.group(0) for m in _YEAR.finditer(text))
    vols   = set(m.group(0) for m in _VOL.finditer(text))
    names  = set(_KNAME.findall(head)) | (set(_ENAME.findall(head)) - _JWORD)
    # 결과 수치: 단위/지표 근처의 의미있는 숫자만 (한 자리 잡음 최소화)
    nums = set()
    for m in re.finditer(r"(THI|NRS|dB|데시벨|%|점|역치|p\s*=|p<|p>|→|->|to)\s*[^0-9]{0,6}(\d+\.?\d*)", text):
        nums.add(m.group(2))
    for m in re.finditer(r"(\d+\.?\d*)\s*(점|dB|데시벨|%|회|kHz|킬로헤르츠)", text):
        nums.add(m.group(1))
    return {"years": years, "vols": vols, "names": names, "nums": nums}

def _find(token, blobs):
    """토큰이 등장하는 corpus 파일 목록."""
    nt = _norm(token)
    return [cid for cid, (_, nb) in blobs.items() if nt and nt in nb]

def check_claim(text, blobs, papers):
    tok = claim_tokens(text)
    # 출처 후보 = '저자명이 등장하는' 파일만 (연도·권호는 참고문헌 목록에 흔해 단독으론 잡음).
    cite_hits = {}
    for t in tok["names"]:
        for cid in _find(t, blobs):
            cite_hits.setdefault(cid, set()).add("name:" + t)
    # 이름이 맞은 파일에 한해 연도·권호로 신뢰도 가산
    for kind in ("vols", "years"):
        for t in tok[kind]:
            for cid in _find(t, blobs):
                if cid in cite_hits: cite_hits[cid].add(f"{kind}:{t}")
    # 원문 논문(primary) 우선. 없으면 병원자료(secondary)에서만 발견된 것.
    prim = {c: v for c, v in cite_hits.items() if c in papers}
    source = max(prim, key=lambda c: len(prim[c])) if prim else None
    secondary_only = (not source) and bool(cite_hits)  # 근거표·대본 등에만 있음
    # 결과 수치가 '원문 논문' 안에 있는지 (secondary는 순환참조라 수치확인으로 안 침)
    nums_found, nums_missing = [], []
    for n in sorted(tok["nums"]):
        if source and _find(n, {source: blobs[source]}):
            nums_found.append(n)
        else:
            nums_missing.append(n)
    if source and not nums_missing:
        verdict, op = "OK", "원문 논문에서 출처·수치 확인됨. (증례 근거등급 타당성은 원장 판단)"
    elif source and nums_missing:
        verdict, op = "PARTIAL", f"원문은 있으나 일부 수치 원문 미확인({', '.join(nums_missing)}) → 원문 재확인 권장."
    elif secondary_only:
        where = os.path.basename(next(iter(cite_hits)))
        verdict, op = "EXTERNAL", ("원문 논문이 업로드 자료에 없음 — 인용이 병원 정리자료(근거표·대본 등)에만 존재. "
                                   "순환참조라 원문 실재·수치를 원장/원문으로 직접 확인 필요.")
    else:
        verdict, op = "UNVERIFIED", "대조할 출처 토큰이 약함 → 수동 확인 필요."
    src_cid = source or (next(iter(cite_hits)) if cite_hits else None)
    return {"claim": text[:160], "source": src_cid, "is_primary": bool(source),
            "cite": sorted(cite_hits.get(src_cid, [])) if src_cid else [],
            "nums_found": nums_found, "nums_missing": nums_missing, "verdict": verdict, "opinion": op}

def run(hospital, pkg_path):
    pkg = json.load(io.open(pkg_path, encoding="utf-8"))
    blobs, src, papers = corpus_blobs(hospital)
    claims = list(pkg.get("papers", []))
    results = [check_claim(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False), blobs, papers) for c in claims]
    # 리포트
    icon = {"OK": "✅", "PARTIAL": "⚠️", "EXTERNAL": "🔎", "UNVERIFIED": "❔"}
    print(f"논문 근거 대조 — 자동 1차 검수 : {os.path.basename(pkg_path)}")
    print("─" * 64)
    for r in results:
        sname = src.get(r["source"], r["source"]) if r["source"] else "—"
        print(f"{icon[r['verdict']]} [{r['verdict']}] {r['claim']}")
        print(f"    출처: {os.path.basename(sname) if sname!='—' else '자료 내 없음'}"
              + (f" | 확인수치 {r['nums_found']}" if r['nums_found'] else "")
              + (f" | 미확인 {r['nums_missing']}" if r['nums_missing'] else ""))
        print(f"    ▶ 1차 의견: {r['opinion']}")
    n_ext = sum(1 for r in results if r["verdict"] == "EXTERNAL")
    n_ok  = sum(1 for r in results if r["verdict"] == "OK")
    print("─" * 64)
    print(f"요약: 자료확인 {n_ok} · 외부논문 {n_ext} · 전체 {len(results)}")
    print("ℹ️ 자동 1차 검수입니다. 인용 실재·수치 대조까지만 하며, 의학적 타당성·저작권·환자동의는 원장 최종 검수가 반드시 필요합니다.")
    # render/검수용 JSON 저장
    outp = os.path.splitext(pkg_path)[0] + ".evidence.json"
    json.dump({"results": results, "summary": {"ok": n_ok, "external": n_ext, "total": len(results)}},
              io.open(outp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return results
