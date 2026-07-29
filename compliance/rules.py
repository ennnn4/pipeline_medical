"""
5단계 컴플라이언스 게이트 (결정론 규칙 엔진). LLM 불필요.
대본/패키지 텍스트를 받아 금지어·인과과장·필수블록 누락을 검사한다.
※ 자동 1차 필터일 뿐 의료광고 심의 통과를 보장하지 않는다. 최종은 사람.

사용: python -m compliance.rules <파일.md> [--edition 이명]
"""
import re, sys, argparse, io
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

# (패턴, 설명, 심각도)  심각도 block=발행불가 / warn=검토
BANNED = [
    (r"완치|불치병이?\s*아닙?니다|기적", "완치·불치병 아님·기적 류 단정", "block"),
    (r"100\s*%|백\s*퍼센트|무조건\s*(낫|좋아)", "100%·무조건 효과", "block"),
    (r"\d+\s*번\s*(하|받으)(셔야|면)\s*(효과|낫)", "'N번 하면 효과' 회수 단정(내부 최우선 금지어)", "block"),
    (r"문의\s*주세요|예약\s*하세요|상담\s*문의|지금\s*전화", "직접 내원·예약 유도", "block"),
    (r"\d+\s*만\s*명.*(치료|봤)", "'N만명 치료' 과장 수치", "warn"),
    (r"뇌\s*혈류(가)?\s*(막|차단)|뇌로\s*가는\s*산소가?\s*부족|목이?\s*망가", "인과 과장(연관성으로 표현할 것)", "block"),
    (r"누구나\s*(좋아|낫)", "'누구나 좋아진다' 일반화", "warn"),
]
# 타과 폄하 (있으면 warn)
DISPARAGE = [(r"(이비인후과|정형외과|내과|정신과|MRI|양방)(이|가)?\s*(틀렸|소용없|못\s*고|엉터리)", "타 진료과/검사 폄하 의심")]

# 편별 필수 블록 (없으면 경고). 정규식으로 존재 확인.
REQUIRED_BLOCKS = {
    "_공통": [
        (r"응급|병원\s*가야|이비인후과(부터|\s*진료)|즉시\s*병원|병원\s*검사|갑자기\s*안\s*들|박동성|119", "응급·병원 가야 할 신호 블록"),
        (r"교육\s*[·・]?\s*정보\s*제공\s*목적|개인마다\s*다르", "고지문(교육·정보 목적/개인차)"),
    ],
    "자율신경": [(r"약[은을]?\s*임의(로)?\s*(중단|끊)", "약 임의중단 금지 문구")],
    "이명": [
        (r"약[은을]?\s*임의(로)?\s*(중단|끊)", "약 임의중단 금지 문구"),
        (r"돌발성\s*난청.*(조기|골든타임|이비인후과)", "돌발성난청 조기 이비인후과 안내"),
    ],
    "돌발성난청": [(r"조기|골든타임|이비인후과", "조기 이비인후과 안내")],
}
# 논문 인용 근처 한계 문장 존재 여부(휴리스틱): '단일 증례/한계/아닙니다/연관성' 이 근처에 있어야
LIMIT_HINTS = r"단일\s*증례|대조군\s*없|인과관계는?\s*아니|연관성|한계|일반화\s*할\s*수\s*없"

def find(text, pat):
    return [m.group(0) for m in re.finditer(pat, text)]

def check(text, edition=None):
    findings = []  # (level, category, detail)
    for pat, desc, lvl in BANNED:
        for hit in set(find(text, pat)):
            findings.append((lvl, "금지어", f"{desc} → '{hit.strip()}'"))
    for pat, desc in DISPARAGE:
        for hit in set(find(text, pat)):
            findings.append(("warn", "타과폄하", f"{desc} → '{hit.strip()}'"))

    blocks = list(REQUIRED_BLOCKS["_공통"])
    if edition and edition in REQUIRED_BLOCKS:
        blocks += REQUIRED_BLOCKS[edition]
    for pat, name in blocks:
        if not find(text, pat):
            findings.append(("block", "필수블록누락", name))

    # 논문 인용에 한계 문장이 붙어 있는지 (THI/저널/등 언급 시)
    if re.search(r"THI|학회지|Front\s|Med\s*Sci|\bp\s*=|증례", text):
        if not re.search(LIMIT_HINTS, text):
            findings.append(("warn", "근거결착", "논문 인용은 있으나 한계 문장(단일 증례·연관성·인과 아님)이 안 보임"))
    return findings

def report(findings):
    blocks = [f for f in findings if f[0] == "block"]
    warns = [f for f in findings if f[0] == "warn"]
    NOTE = "ℹ️ 자동 규칙 검사입니다. 통과해도 발행 보장이 아니며, 최종 의학 검수·의료광고 심의는 원장/전문가 확인이 반드시 필요합니다."
    print("─" * 56)
    if not findings:
        print("✅ PASS — 자동 규칙 위반 없음")
        print(NOTE)
        return 0
    for lvl, cat, det in blocks:
        print(f"  ⛔ [{cat}] {det}")
    for lvl, cat, det in warns:
        print(f"  ⚠️ [{cat}] {det}")
    print("─" * 56)
    verdict = "FAIL (발행 불가)" if blocks else "조건부 PASS (경고 검토 후)"
    print(f"판정: {verdict}  |  ⛔{len(blocks)}  ⚠️{len(warns)}")
    print(NOTE)
    return 1 if blocks else 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--edition", default=None, help="이명/자율신경/돌발성난청 등")
    a = ap.parse_args()
    text = io.open(a.file, encoding="utf-8", errors="ignore").read()
    ed = a.edition
    if not ed:  # 파일명에서 추정
        for k in REQUIRED_BLOCKS:
            if k != "_공통" and k in a.file:
                ed = k; break
    print(f"검사 파일: {a.file}  (편: {ed or '자동'})")
    sys.exit(report(check(text, ed)))

if __name__ == "__main__":
    main()
