"""LLM 의미기반 근거검증 — 각 인용 claim을 '인용한 논문 원문(corpus)'과 대조해 Claude가 판정.

check.py의 저자명+수치 매칭을 넘어, '이 원문이 이 주장을 실제로 뒷받침하나'를 의미로 판단하고
원문에서 근거 문장을 그대로 인용한다. 실제 원문을 읽어 판정 — 원문에 없는 내용은 지어내지 않는다.

결과는 out/<topic>_package.evidence.llm.json 로 저장(evidence_seed가 로드).
1회 로컬 실행(비용/시간)으로 산출물을 커밋 → 배포 시드는 결정적·빠름.

사용: python -m evidence.llm_verify [이명]
"""
import io, json, os, sys
from llm.runner import generate

_SYS = ("당신은 의학 논문 근거 검증자입니다. 주어진 '주장'이 '논문 원문'에 의해 실제로 뒷받침되는지 "
        "엄격하게 판단합니다. 원문에 없는 내용은 절대 지어내지 않으며, 근거가 약하면 partial/none으로 낮춥니다.")

def verify_one(claim, paper_text):
    user = f"""[주장]
{claim}

[논문 원문(발췌)]
{paper_text[:120000]}

위 주장이 이 논문 원문으로 뒷받침되는지 판정하세요. 근거가 되는 원문 문장을 그대로 인용하세요(없으면 빈 문자열).
- direct: 주장의 핵심(수치·결론)이 원문에 명시적으로 존재
- partial: 부분적으로만(맥락 일부만) 뒷받침
- none: 원문이 주장을 뒷받침하지 않음(→ verification_status=failed)
verification_status는 direct/partial일 때만 verified, none이면 failed.
JSON 형식: {{"support_level":"direct|partial|none","verification_status":"verified|failed","supporting_quote":"원문 문장 그대로","rationale":"한국어 한두 문장"}}"""
    return generate(_SYS, user, parse_json=True, max_tokens=2000)

def run(topic="이명", hospital="boncure"):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ev = json.load(io.open(os.path.join(root, "data", hospital, "out", f"{topic}_package.evidence.json"), encoding="utf-8"))
    corp = os.path.join(root, "data", hospital, "corpus")
    out = []
    for r in ev.get("results", []):
        src = r.get("source")
        p = os.path.join(corp, src) if src else None
        paper = io.open(p, encoding="utf-8", errors="ignore").read() if p and os.path.exists(p) else ""
        if not paper:
            llm = {"support_level": "none", "verification_status": "failed", "supporting_quote": "", "rationale": "원문 파일 없음"}
        else:
            try:
                llm = verify_one(r["claim"], paper)
            except Exception as e:
                llm = {"support_level": "none", "verification_status": "failed", "supporting_quote": "", "rationale": f"검증 오류: {e}"}
        out.append({"claim": r["claim"], "source": r.get("source"), "source_name": r.get("source_name"), "llm": llm})
        print("·", (r["claim"][:44]), "->", llm.get("support_level"), "/", llm.get("verification_status"))
    dst = os.path.join(root, "data", hospital, "out", f"{topic}_package.evidence.llm.json")
    json.dump({"results": out}, io.open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("saved:", dst)

if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "이명")
