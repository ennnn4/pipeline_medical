"""
LLM 러너 — Anthropic SDK 래퍼. 분류·KB·대본 단계가 공용으로 쓴다.
모델 claude-opus-5, adaptive thinking, effort high, 긴 출력은 streaming.
키: ANTHROPIC_API_KEY 또는 `ant auth login` 프로파일.
"""
import os, json

MODEL = os.environ.get("BONCURE_MODEL", "claude-opus-5")

def _client():
    import anthropic
    return anthropic.Anthropic()

def _extract_json(text):
    """```json 펜스/앞뒤 잡소리를 걷어내고 첫 {...} 블록을 파싱."""
    t = text.strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
        if m: t = m.group(1).strip()
    s, e = t.find("{"), t.rfind("}")
    if s != -1 and e != -1:
        t = t[s:e+1]
    return json.loads(t)

def generate(system, user, parse_json=False, max_tokens=32000, effort="high"):
    """
    system/user 프롬프트로 1회 생성. parse_json=True 면 응답 텍스트를 JSON으로 파싱(dict), 아니면 텍스트.
    긴 출력 대비 streaming. (구조화 출력 대신 프롬프트-지시 JSON + 견고 파싱 방식 — SDK 호환성 우선)
    """
    client = _client()
    if parse_json:
        user = user + "\n\n반드시 유효한 JSON 하나만 출력. 코드펜스·설명 없이 { 로 시작해 } 로 끝낼 것."
    kwargs = dict(
        model=MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    with client.messages.stream(**kwargs) as stream:
        msg = stream.get_final_message()
    text = "".join(b.text for b in msg.content if b.type == "text")
    return _extract_json(text) if parse_json else text

def load_prompt(name):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "prompts", name), encoding="utf-8") as f:
        return f.read()

def corpus_text(hospital, cids=None, categories=None, max_chars=400000):
    """data/<병원>/corpus 에서 조건에 맞는 코퍼스를 합쳐 반환 (프롬프트에 넣을 재료)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    corp = os.path.join(root, "data", hospital, "corpus")
    man = os.path.join(corp, "_MANIFEST.tsv")
    rows = []
    if os.path.exists(man):
        for line in open(man, encoding="utf-8").read().splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) >= 4:
                rows.append({"cid": parts[0], "category": parts[1], "source": parts[3]})
    out, total = [], 0
    for r in rows:
        if cids and r["cid"] not in cids: continue
        if categories and r["category"] not in categories: continue
        p = os.path.join(corp, r["cid"] + ".txt")
        if not os.path.exists(p): continue
        t = open(p, encoding="utf-8", errors="ignore").read()
        chunk = f"\n\n===== [{r['cid']} · {r['category']}] {r['source']} =====\n{t}"
        if total + len(chunk) > max_chars: break
        out.append(chunk); total += len(chunk)
    return "".join(out)
