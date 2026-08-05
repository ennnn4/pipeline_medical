"""원본 유사도(표절) 검사 — 순수 로직(DB·LLM 무관, 테스트 가능).

축자 겹침(verbatim)은 **결정론적**으로 계산(단어 shingle Jaccard + 연속 일치 n-gram 최대길이).
의미/사례/구조 축은 상위(benchmark.check_similarity)에서 선택적으로 LLM 보강.
"""
import re

_WORD = re.compile(r"[0-9A-Za-z가-힣]+")


def norm_words(t):
    return _WORD.findall((t or "").lower())


def _shingles(words, n):
    if len(words) < n:
        return set()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def verbatim_overlap(script, source):
    """script vs source 축자 겹침. 반환: {shingle_jaccard, longest_run_words, verbatim_score(0~1)}.
    longest_run_words = 양쪽에 그대로 등장하는 최대 연속 단어 수(근사: n-gram 크기 상향 탐색)."""
    sw, vw = norm_words(script), norm_words(source)
    ss5, vs5 = _shingles(sw, 5), _shingles(vw, 5)
    union = ss5 | vs5
    jac = (len(ss5 & vs5) / len(union)) if union else 0.0
    longest = 0
    for n in (5, 8, 12, 16, 24, 32):
        if _shingles(sw, n) & _shingles(vw, n):
            longest = n
        else:
            break
    score = min(1.0, jac * 1.5 + (longest / 32.0) * 0.5)
    return {"shingle_jaccard": round(jac, 4), "longest_run_words": longest,
            "verbatim_score": round(score, 4)}


def risk_level(verbatim_score, longest_run_words, semantic_score=0.0):
    """표절 위험도. 긴 축자 일치가 가장 강한 신호."""
    if longest_run_words >= 12 or verbatim_score >= 0.30 or (semantic_score or 0) >= 0.80:
        return "high"
    if longest_run_words >= 8 or verbatim_score >= 0.12 or (semantic_score or 0) >= 0.55:
        return "medium"
    return "low"
