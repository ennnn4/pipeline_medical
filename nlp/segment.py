"""
문장 분할기 — offset 보존. 마침표 단순분할 금지(소수점·논문번호·약어 보호).
반환: [(start_offset, end_offset, text)] — text == block_text[start:end] 를 보장(span 정합).
unit='codepoint'(파이썬 인덱스, 서버측 기본). 편집기(브라우저)는 나중에 utf16으로.
"""
import re

SEGMENTER_VERSION = "ko-rule-1"
_ENDERS = set(".?!…")

def _is_decimal_dot(text, i):
    return text[i] == "." and 0 < i < len(text) - 1 and text[i-1].isdigit() and text[i+1].isdigit()

def segment(text, unit="codepoint"):
    """문장 span 목록. 종결부호(.?!…) 뒤 공백/끝을 경계로, 소수점·연속부호는 보호."""
    text = text or ""
    n = len(text)
    out, start, i = [], 0, 0
    while i < n:
        ch = text[i]
        if ch in _ENDERS and not _is_decimal_dot(text, i):
            j = i + 1
            while j < n and text[j] in _ENDERS:   # '…', '?!', '..' 흡수
                j += 1
            if j >= n or text[j].isspace():        # 경계 확정
                _emit(text, start, j, out)
                k = j
                while k < n and text[k].isspace():
                    k += 1
                start = i = k
                continue
        i += 1
    if start < n:
        _emit(text, start, n, out)
    return out

def _emit(text, start, end, out):
    raw = text[start:end]
    lead = len(raw) - len(raw.lstrip())
    seg = raw.strip()
    if not seg:
        return
    s = start + lead
    e = s + len(seg)
    assert text[s:e] == seg, "offset 정합 실패"   # span 무결성
    out.append((s, e, seg))
