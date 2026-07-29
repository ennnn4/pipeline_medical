#!/usr/bin/env python
"""
boncure-pipeline 오케스트레이터.
결정론 단계(ingest/compliance/render)는 LLM 없이 동작. LLM 단계는 ANTHROPIC_API_KEY 필요.

  python run.py ingest      --hospital boncure
  python run.py classify    --hospital boncure
  python run.py kb          --hospital boncure
  python run.py episode     --hospital boncure --topic 이명
  python run.py compliance  --file data/out/이명_package.json --edition 이명
  python run.py render      --file data/out/이명_package.json
  python run.py all         --hospital boncure --topic 이명
"""
import os, sys, json, argparse
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# .env 로더 (의존성 없이)
def _load_env():
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
_load_env()

def _cfg(h):
    import yaml
    return yaml.safe_load(open(os.path.join(ROOT, "config", f"{h}.yaml"), encoding="utf-8"))

def _kbdir(h):
    d = os.path.join(ROOT, "data", h, "kb"); os.makedirs(d, exist_ok=True); return d
def _outdir(h):
    d = os.path.join(ROOT, "data", h, "out"); os.makedirs(d, exist_ok=True); return d

def cmd_ingest(a):
    from ingest.extract import run
    return run(a.hospital)

def cmd_classify(a):
    from llm.runner import generate, load_prompt, corpus_text
    h = a.hospital
    corpus = corpus_text(h)
    if not corpus.strip():
        print("코퍼스가 비었습니다. 먼저 ingest 하세요."); return
    res = generate(load_prompt("classify.md"), "코퍼스:\n" + corpus, parse_json=True)
    json.dump(res, open(os.path.join(_kbdir(h),"classify.json"),"w",encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"분류 완료 → data/{h}/kb/classify.json")

def cmd_kb(a):
    from llm.runner import generate, load_prompt, corpus_text
    h = a.hospital
    cfg = _cfg(h); kb = _kbdir(h)
    # 원장 프로파일
    prof_src = corpus_text(h, categories=["원장설문지","원장인터뷰","기존유튜브대본","원장강의자료"]) or corpus_text(h)
    prof = generate(load_prompt("profile.md"), "자료:\n"+prof_src, parse_json=True)
    json.dump(prof, open(os.path.join(kb,"profile.json"),"w",encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"원장 프로파일 → data/{h}/kb/profile.json")
    # 논문 근거표
    ev = generate(load_prompt("evidence.md"), "논문:\n"+(corpus_text(h, categories=["논문"]) or ""), parse_json=True)
    json.dump(ev, open(os.path.join(kb,"evidence.json"),"w",encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"논문 근거표 → data/{h}/kb/evidence.json")
    # 경쟁 분석
    comp = generate(load_prompt("competitor.md"), "경쟁자막:\n"+(corpus_text(h, categories=["경쟁유튜브"]) or ""), parse_json=True)
    json.dump(comp, open(os.path.join(kb,"competitor.json"),"w",encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"경쟁 분석 → data/{h}/kb/competitor.json")
    # 질환별 KB
    for dz in (cfg.get("diseases") or []):
        d = generate(load_prompt("disease.md"), f"질환: {dz}\n자료:\n"+corpus_text(h), parse_json=True)
        json.dump(d, open(os.path.join(kb,f"disease_{dz}.json"),"w",encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"질환 KB({dz}) → data/{h}/kb/disease_{dz}.json")

def cmd_episode(a):
    from llm.runner import generate, load_prompt
    kb = _kbdir(a.hospital)
    def rd(n):
        p=os.path.join(kb,n); return open(p,encoding="utf-8").read() if os.path.exists(p) else "{}"
    kb_blob = ("[원장프로파일]\n"+rd("profile.json")+"\n[논문근거]\n"+rd("evidence.json")
               +"\n[경쟁분석]\n"+rd("competitor.json")+f"\n[질환KB]\n"+rd(f"disease_{a.topic}.json"))
    pkg = generate(load_prompt("director.md"), f"주제: {a.topic}\nKB:\n"+kb_blob, parse_json=True, max_tokens=55000)
    out = os.path.join(_outdir(a.hospital), f"{a.topic}_package.json")
    json.dump(pkg, open(out,"w",encoding="utf-8"), ensure_ascii=False, indent=1)
    # 분량 자가검산 (발화 글자수 ÷ 450 ≈ 분)
    say_chars = sum(len((b.get("say") or "").replace(" ", "")) for b in pkg.get("script", []))
    mins = round(say_chars / 450, 1)
    print("대본 패키지 →", out)
    print(f"발화 글자수 {say_chars}자 → 예상 러닝타임 ≈ {mins}분  (비트 {len(pkg.get('script',[]))}개)")
    if say_chars < 5400:
        print("  ⚠️ 12분 미만 — 대사가 개요 수준일 수 있음. 프롬프트 분량규칙 확인 후 재생성 권장.")

def cmd_compliance(a):
    """발행되는 텍스트만 검사한다(발화 say + 고정댓글 + 설명란). 메타/촬영주의 문구는 제외해 false positive 방지."""
    from compliance.rules import check, report
    import json as J
    raw = open(a.file, encoding="utf-8", errors="ignore").read()
    if a.file.endswith(".json"):
        try:
            pkg = J.loads(raw)
            parts = []
            for b in pkg.get("script",[]):
                parts.append(b.get("say",""))
                parts.append(b.get("scene",""))   # 자막도 발행물
            parts += [pkg.get("pinned_comment",""), pkg.get("description","")]
            parts += pkg.get("caption_emphasis",[])   # 자막(발행물). papers는 '금지 표현'을 인용·경고하는 근거 메모라 검사에서 제외
            txt = "\n".join(parts)
        except Exception:
            txt = raw
    else:
        txt = raw
    ed = a.edition
    print(f"컴플라이언스 검사: {a.file} (편 {ed or '자동'}) — 발행 텍스트만")
    return report(check(txt, ed))

def cmd_render(a):
    from render.render import render, _meta
    pkg = json.load(open(a.file, encoding="utf-8"))
    out = os.path.splitext(a.file)[0] + ".html"
    open(out,"w",encoding="utf-8").write(render(pkg, _meta(getattr(a,"hospital","boncure"))))
    print("대시보드 →", out)

def cmd_all(a):
    cmd_ingest(a); cmd_classify(a); cmd_kb(a); cmd_episode(a)
    pkg = os.path.join(_outdir(a.hospital), f"{a.topic}_package.json")
    a.file = pkg; a.edition = a.topic
    cmd_compliance(a); cmd_render(a)

def cmd_init(a):
    """새 병원 온보딩: config 템플릿 + 병원별 data 폴더 생성."""
    h = a.hospital
    cfgp = os.path.join(ROOT, "config", f"{h}.yaml")
    if os.path.exists(cfgp):
        print(f"이미 있음: config/{h}.yaml");
    else:
        tpl = os.path.join(ROOT, "config", "_template.yaml")
        src = open(tpl, encoding="utf-8").read() if os.path.exists(tpl) else ""
        src = src.replace("__HOSPITAL_ID__", h)
        open(cfgp, "w", encoding="utf-8").write(src)
        print(f"생성: config/{h}.yaml  ← 병원명·화자·슬로건·질환목록을 채우세요")
    for sub in ("raw","corpus","kb","out"):
        os.makedirs(os.path.join(ROOT,"data",h,sub), exist_ok=True)
    print(f"생성: data/{h}/(raw·corpus·kb·out)")
    print("─"*56)
    print(f"다음: ① config/{h}.yaml 편집  ② data/{h}/raw 에 자료 넣기")
    print(f"      ③ python run.py all --hospital {h} --topic <주제>")

def main():
    ap = argparse.ArgumentParser(description="boncure-pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ini = sub.add_parser("init"); ini.add_argument("--hospital", required=True)
    for c in ["ingest","classify","kb"]:
        s = sub.add_parser(c); s.add_argument("--hospital", default="boncure")
    e = sub.add_parser("episode"); e.add_argument("--hospital", default="boncure"); e.add_argument("--topic", required=True)
    cp = sub.add_parser("compliance"); cp.add_argument("--file", required=True); cp.add_argument("--edition", default=None)
    r = sub.add_parser("render"); r.add_argument("--file", required=True); r.add_argument("--hospital", default="boncure")
    al = sub.add_parser("all"); al.add_argument("--hospital", default="boncure"); al.add_argument("--topic", required=True)
    a = ap.parse_args()
    {"init":cmd_init,"ingest":cmd_ingest,"classify":cmd_classify,"kb":cmd_kb,"episode":cmd_episode,
     "compliance":cmd_compliance,"render":cmd_render,"all":cmd_all}[a.cmd](a)

if __name__ == "__main__":
    main()
