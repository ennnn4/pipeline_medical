"""AI 비용 로깅 — Claude·OpenAI 콜의 실제 토큰 사용량을 기록하고 USD로 환산.

토큰 수는 API 응답의 usage에서 '실측'. 요금(RATES)은 조정 가능한 가정치.
기록: data/costs.jsonl(append). 조회: summary(). CLI: python -m llm.cost [since_iso]

주의: RATES는 편집 가능. 정확한 청구액은 각 콘솔(Anthropic/OpenAI)에서 확인.
"""
import os, io, json, time, datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COSTS = os.path.join(_ROOT, "data", "costs.jsonl")

# 요금(1M 토큰당 USD) — 실제 요금제에 맞게 조정. 모델명으로 티어 선택(모르면 opus급으로 보수적 가정).
RATES = {
    "claude":        {"in": 15.0, "out": 75.0},   # 기본/opus급(모델 매칭 실패 시)
    "opus":          {"in": 15.0, "out": 75.0},
    "sonnet":        {"in": 3.0,  "out": 15.0},
    "haiku":         {"in": 1.0,  "out": 5.0},
    "gpt-image-1":   {"in": 5.0,  "img": 40.0},    # 텍스트 입력 / 이미지 출력 토큰
}

def _claude_rate(model):
    m = (model or "").lower()
    if "opus" in m:   return RATES["opus"]
    if "sonnet" in m: return RATES["sonnet"]
    if "haiku" in m:  return RATES["haiku"]
    return RATES["claude"]

def record(source, model, in_tok=0, out_tok=0, img_tok=0, note="", flat_usd=None):
    if flat_usd is not None:
        usd = flat_usd
    elif "image" in model:
        usd = in_tok / 1e6 * RATES["gpt-image-1"]["in"] + img_tok / 1e6 * RATES["gpt-image-1"]["img"]
    else:
        r = _claude_rate(model)
        usd = in_tok / 1e6 * r["in"] + out_tok / 1e6 * r["out"]
    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "source": source, "model": model,
           "in": in_tok, "out": out_tok, "img": img_tok, "usd": round(usd, 4), "note": note[:80]}
    try:
        os.makedirs(os.path.dirname(COSTS), exist_ok=True)
        with io.open(COSTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    print(f"  💰 {source}/{model}: ${usd:.4f}  (in {in_tok:,} / out {out_tok + img_tok:,} tok){' · ' + note if note else ''}")
    return usd

def summary(since=None):
    """{source: {usd, calls}} + total. since=ISO문자열이면 그 이후만."""
    out, total = {}, 0.0
    try:
        for line in io.open(COSTS, encoding="utf-8"):
            r = json.loads(line)
            if since and r["ts"] < since:
                continue
            s = out.setdefault(r["source"], {"usd": 0.0, "calls": 0})
            s["usd"] += r["usd"]; s["calls"] += 1; total += r["usd"]
    except FileNotFoundError:
        pass
    return {"by_source": {k: {"usd": round(v["usd"], 4), "calls": v["calls"]} for k, v in out.items()},
            "total_usd": round(total, 4)}

if __name__ == "__main__":
    import sys
    s = summary(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"총 비용: ${s['total_usd']} (약 {int(s['total_usd']*1380):,}원)")
    for src, v in s["by_source"].items():
        print(f"  · {src}: ${v['usd']} ({v['calls']}콜)")
