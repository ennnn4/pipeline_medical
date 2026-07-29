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

def generate(system, user, json_schema=None, max_tokens=32000, effort="high"):
    """
    system/user 프롬프트로 1회 생성. json_schema 주면 구조화 출력(dict 반환), 아니면 텍스트 반환.
    긴 출력 대비 streaming 사용.
    """
    client = _client()
    output_config = {"effort": effort}
    if json_schema:
        output_config["format"] = {"type": "json_schema", "schema": json_schema}
    kwargs = dict(
        model=MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config=output_config,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    with client.messages.stream(**kwargs) as stream:
        msg = stream.get_final_message()
    text = "".join(b.text for b in msg.content if b.type == "text")
    if json_schema:
        return json.loads(text)
    return text

def load_prompt(name):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "prompts", name), encoding="utf-8") as f:
        return f.read()

def corpus_text(cids=None, categories=None, max_chars=400000):
    """data/corpus 에서 조건에 맞는 코퍼스를 합쳐 반환 (프롬프트에 넣을 재료)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    corp = os.path.join(root, "data", "corpus")
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
