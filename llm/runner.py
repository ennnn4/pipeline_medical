"""
LLM 러너 — Anthropic SDK 래퍼. 분류·KB·대본 단계가 공용으로 쓴다.

모델 티어링(비용·속도): 대본(director)=최고품질 MODEL, KB/구조적 단계=저렴·빠른 MODEL_KB.
안전장치: 명시적 timeout(조용한 연결끊김 → 무한대기 대신 재시도)·max_retries·토큰 단위 스트리밍
          진행표시(부모의 heartbeat가 stdout에 의존하므로, 긴 호출 중에도 주기적으로 한 줄 찍음).
키: ANTHROPIC_API_KEY 또는 `ant auth login` 프로파일.
"""
import os, json, re, sys, time

MODEL    = os.environ.get("BONCURE_MODEL",    "claude-opus-5")     # 대본(최고품질)
MODEL_KB = os.environ.get("BONCURE_MODEL_KB", "claude-sonnet-5")   # KB·구조적 단계(저렴·빠름)

# Claude 5 계열만 thinking/effort 지원. 그 외(예: haiku 4.5)는 단순 호출로.
_THINKING_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5")

def _client():
    """명시적 타임아웃·재시도. read=180s: 스트림이 조용히 끊기면 무한대기 대신 3분 뒤 에러→재시도.
    정상 생성 중엔 API가 ping/delta를 계속 보내서 이 타임아웃에 안 걸린다."""
    import anthropic, httpx
    return anthropic.Anthropic(
        timeout=httpx.Timeout(connect=15.0, read=180.0, write=30.0, pool=15.0),
        max_retries=int(os.environ.get("BONCURE_LLM_RETRIES", "2")),
    )

def _extract_json(text):
    """```json 펜스/앞뒤 잡소리를 걷어내고 첫 {...} 블록을 파싱. 후행 콤마 등 흔한 오류 보정."""
    t = (text or "").strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
        if m:
            t = m.group(1).strip()
    s, e = t.find("{"), t.rfind("}")
    if s != -1 and e > s:
        t = t[s:e + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        t2 = re.sub(r",(\s*[}\]])", r"\1", t)          # 후행 콤마 제거
        try:
            return json.loads(t2)
        except json.JSONDecodeError as err:
            raise ValueError(f"LLM 응답을 JSON으로 파싱하지 못했습니다: {err}. 앞부분: {t[:200]}")

def _kwargs(model, system, user, max_tokens, effort, cache):
    content = [{"type": "text", "text": user}]
    if cache:
        # 큰 입력을 캐시 프리픽스로 → 같은 자료로 다른 주제 재생성 시 입력 토큰 대폭 절감.
        content[0]["cache_control"] = {"type": "ephemeral"}
    kw = dict(model=model, max_tokens=max_tokens, system=system,
              messages=[{"role": "user", "content": content}])
    if model in _THINKING_MODELS:
        kw["thinking"] = {"type": "adaptive"}
        kw["output_config"] = {"effort": effort}
    return kw

def generate(system, user, parse_json=False, max_tokens=32000, effort="high",
             model=None, label=None, cache=False):
    """
    system/user 프롬프트로 1회 생성. parse_json=True 면 JSON dict, 아니면 텍스트.
    - model: None이면 MODEL(대본). KB 단계는 MODEL_KB를 넘긴다.
    - label: 진행 로그 태그(예: "KB profile", "대본").
    - cache: 큰 입력을 프롬프트 캐싱(반복 생성 비용↓).
    토큰 단위 스트리밍으로 진행상황을 주기적으로 stdout에 찍는다 → 부모의 heartbeat가 살아있음을 안다.
    """
    client = _client()
    mdl = model or MODEL
    if parse_json:
        user = user + "\n\n반드시 유효한 JSON 하나만 출력. 코드펜스·설명 없이 { 로 시작해 } 로 끝낼 것."
    kw = _kwargs(mdl, system, user, max_tokens, effort, cache)
    tag = f"[{label}] " if label else ""
    parts, chars = [], 0
    t0 = time.monotonic(); last = t0
    print(f"  ▷ {tag}{mdl} 생성 시작 (입력 {len(user):,}자)", flush=True)
    with client.messages.stream(**kw) as stream:
        for chunk in stream.text_stream:
            parts.append(chunk); chars += len(chunk)
            now = time.monotonic()
            if now - last >= 10:                      # 10초마다 진행 한 줄(= heartbeat 갱신 신호)
                print(f"  … {tag}생성 중 {chars:,}자 · {int(now - t0)}s", flush=True)
                last = now
        msg = stream.get_final_message()
    dt = round(time.monotonic() - t0, 1)
    text = "".join(parts) or "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    u = getattr(msg, "usage", None)
    itok = getattr(u, "input_tokens", 0) or 0
    otok = getattr(u, "output_tokens", 0) or 0
    cread = getattr(u, "cache_read_input_tokens", 0) or 0     # 프롬프트 캐시 재사용 토큰(있으면 훨씬 쌈)
    usd = 0.0
    try:
        from llm.cost import record, _claude_rate
        r = _claude_rate(mdl)
        usd = itok / 1e6 * r["in"] + otok / 1e6 * r["out"]
        record("claude", mdl, in_tok=itok, out_tok=otok, note=(label or system or "")[:40])
    except Exception:
        pass
    # 단계별 원가 계측(파싱용) — app이 이 라인들을 합산해 이번 생성 총비용을 기록/표시
    print(f"  [COST] stage={label or 'gen'} model={mdl} in={itok} out={otok} cache={cread} usd={usd:.4f} lat={dt}s", flush=True)
    print(f"  ✓ {tag}완료 {chars:,}자 · {int(dt)}s", flush=True)
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
