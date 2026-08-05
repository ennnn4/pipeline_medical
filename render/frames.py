"""
비트 유형별 스토리보드 SVG 프레임 (파라메트릭·재사용). 외부 이미지 0.
beat의 block/scene/tags 키워드로 프레임 유형을 고르고 16:9 SVG inner markup을 반환.
병원명·화자는 하드코딩하지 않고 hosp 인자로 주입(멀티병원 안전). 종류를 넉넉히 두어 단조로움 방지.
"""
A, AL, COAT, SKIN, HAIR, W, MUT = "#3182f6", "#7fb0ff", "#eef1f6", "#dcb694", "#2c2f3a", "#fff", "#9db4e6"

def T(x, y, s, wt, c, t):
    t = str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<text x="{x}" y="{y}" font-family="Pretendard,sans-serif" font-size="{s}" font-weight="{wt}" fill="{c}">{t}</text>'

def _studio():
    return '<rect width="400" height="225" fill="#1b2131"/><ellipse cx="200" cy="162" rx="165" ry="98" fill="#252d42" opacity="0.75"/>'
def _clinical():
    return '<rect width="400" height="225" fill="#0f1522"/>'
def _doc(cx, cy=118):
    return (f'<path d="M{cx-58} 225 Q{cx} {cy+30} {cx+58} 225 Z" fill="{COAT}"/>'
            f'<path d="M{cx-13} 225 L{cx-6} {cy+22} L{cx+6} {cy+22} L{cx+13} 225 Z" fill="#cdd3e0"/>'
            f'<circle cx="{cx}" cy="{cy}" r="26" fill="{SKIN}"/>'
            f'<path d="M{cx-27} {cy-3} q27 -34 54 0 q-6 -23 -27 -23 q-21 0 -27 23 Z" fill="{HAIR}"/>'
            f'<circle cx="{cx-9}" cy="{cy+3}" r="2.1" fill="#3a3f4c"/><circle cx="{cx+9}" cy="{cy+3}" r="2.1" fill="#3a3f4c"/>')
def _lower(t, s=""):
    return (f'<rect x="22" y="172" width="250" height="40" rx="7" fill="#0f1420" opacity="0.86"/>'
            f'<rect x="22" y="172" width="5" height="40" fill="{A}"/>' + T(40,191,13,800,W,t) + T(40,205,9.5,500,MUT,s))
def _cap(t):
    return '<rect x="0" y="192" width="400" height="33" fill="#000" opacity="0.5"/>' + T(24,214,14,700,W,t)

# ── 기본 장면들 ──
def _cold_open():
    return ('<rect width="400" height="225" fill="#0e1220"/>'
            '<rect x="262" y="24" width="104" height="86" rx="4" fill="#12172a" stroke="#2a3350"/>'
            '<circle cx="290" cy="50" r="13" fill="#e9e4cf"/>'
            '<rect x="34" y="128" width="250" height="60" rx="10" fill="#1a2236"/><rect x="44" y="126" width="52" height="30" rx="8" fill="#dfe4f0"/>'
            '<circle cx="86" cy="138" r="13" fill="#c9ccd6"/>'
            f'<g fill="none" stroke="{A}" stroke-width="1.6"><path d="M96 132 q10 -8 20 -2"/><path d="M100 126 q16 -12 30 -3" opacity="0.6"/></g>'
            + _cap("증상 사격 · 인사 없음"))
def _talk(hosp="", label="원장 정면"):
    return _studio() + _doc(200) + _lower(hosp or "화자 정면", label)
def _anatomy():
    return (_clinical() + '<circle cx="200" cy="58" r="34" fill="#22304d"/><rect x="176" y="72" width="48" height="120" rx="14" fill="#1c2740"/>'
            f'<path d="M188 190 C184 150 190 108 196 80" fill="none" stroke="{A}" stroke-width="3.6" stroke-linecap="round"/>'
            f'<path d="M212 190 C216 150 210 108 204 80" fill="none" stroke="{AL}" stroke-width="3" opacity="0.85"/>'
            + _lower("해부 일러스트", "경로/구조 하이라이트"))
def _graph():
    return (_clinical() + '<line x1="60" y1="150" x2="200" y2="150" stroke="#39445c"/>'
            '<rect x="80" y="48" width="34" height="102" rx="3" fill="#39445c"/>' + T(84,42,11,800,W,"전")
            + f'<rect x="150" y="140" width="34" height="10" rx="3" fill="{A}"/>' + T(156,134,11,800,A,"후")
            + '<rect x="214" y="52" width="168" height="108" rx="10" fill="#0f1420"/>' + T(228,74,10,700,"#cfd8e8","논문 자막 4줄")
            + T(228,92,9.5,600,MUT,"저자·저널·연도") + T(228,110,9.5,700,"#ffb84d","+ 한계 문장")
            + '<rect x="228" y="122" width="104" height="20" rx="10" fill="#241318"/>' + T(238,136,9.5,800,"#ff9aa2","♪ 음악 OFF"))
def _emergency():
    return ('<rect width="400" height="225" fill="#1a0e12"/><rect x="28" y="28" width="344" height="168" rx="12" fill="#2a1116" stroke="#ff5b6a"/>'
            + T(80,80,14,800,"#ff8a94","응급 · 병원 신호") + T(50,120,10.5,600,"#f2c9cd","· 빨간 자막 카드 순차")
            + T(50,150,10.5,600,"#f2c9cd","· 3초 유지 · BGM 0"))
def _split():
    return ('<rect width="400" height="225" fill="#141a28"/><line x1="200" y1="28" x2="200" y2="200" stroke="#2a3350" stroke-dasharray="4 5"/>'
            + '<rect x="24" y="168" width="158" height="32" rx="6" fill="#132033"/>' + T(36,188,10.5,700,"#8fe3c2","통념 O")
            + '<rect x="220" y="168" width="158" height="32" rx="6" fill="#241318"/>' + T(232,188,10.5,700,"#f2a6ad","반례 X"))
def _demo(hosp=""):
    return (_studio() + _doc(200) + f'<circle cx="176" cy="150" r="9" fill="{SKIN}"/><circle cx="224" cy="150" r="9" fill="{SKIN}"/>'
            + f'<rect x="250" y="40" width="128" height="30" rx="15" fill="{A}"/>' + T(266,60,13,800,W,"지금 해보세요")
            + _lower(hosp or "실연", "따라 하기"))

# ── 추가 장면들(다양성) ──
def _before_after():
    return (_clinical() + '<line x1="200" y1="24" x2="200" y2="176" stroke="#2a3350" stroke-dasharray="3 5"/>'
            + '<circle cx="108" cy="96" r="42" fill="#2a2036"/><circle cx="108" cy="96" r="42" fill="none" stroke="#5b4a6a" stroke-dasharray="2 3"/>'
            + T(150,40,11,800,MUT,"BEFORE")
            + f'<circle cx="292" cy="96" r="42" fill="#20303a"/><circle cx="292" cy="96" r="42" fill="none" stroke="{A}"/>'
            + T(258,40,11,800,A,"AFTER") + _lower("전 / 후 비교", "동일 각도·조명"))
def _closeup():
    return (_studio() + f'<circle cx="200" cy="112" r="72" fill="{SKIN}"/><circle cx="200" cy="112" r="72" fill="none" stroke="{W}" stroke-opacity="0.15" stroke-width="8"/>'
            + f'<circle cx="238" cy="96" r="15" fill="none" stroke="{A}" stroke-width="2.4"/><line x1="249" y1="107" x2="262" y2="120" stroke="{A}" stroke-width="2.4"/>'
            + _lower("클로즈업", "부위 확대 · 디테일"))
def _list3():
    x = 0
    cards = ""
    for i, cx in enumerate((70, 200, 330)):
        cards += (f'<rect x="{cx-52}" y="60" width="104" height="96" rx="10" fill="#1a2236" stroke="#2a3350"/>'
                  f'<circle cx="{cx}" cy="86" r="15" fill="{A}"/>' + T(cx-4,91,13,800,W,str(i+1))
                  + T(cx-34,128,10.5,700,"#cfd8e8","항목 "+str(i+1)))
    return '<rect width="400" height="225" fill="#0f1522"/>' + cards + _cap("리스트 · Top 3 카드")
def _interview():
    return (_studio() + _doc(132, 120) + _doc(268, 120)
            + '<rect x="150" y="150" width="100" height="14" rx="7" fill="#0f1420" opacity="0.7"/>'
            + _lower("문진 · 대담", "질문/답 교차"))
def _checklist():
    rows = ""
    for i, y in enumerate((70, 108, 146)):
        rows += (f'<rect x="70" y="{y}" width="20" height="20" rx="5" fill="{A}"/>'
                 + f'<path d="M74 {y+10} l4 5 l9 -11" stroke="{W}" stroke-width="2.4" fill="none"/>'
                 + f'<rect x="102" y="{y+4}" width="{190-i*24}" height="12" rx="6" fill="#2a3350"/>')
    return '<rect width="400" height="225" fill="#111726"/>' + rows + _cap("체크리스트 · 자가진단")
def _device():
    return (_clinical() + '<rect x="150" y="70" width="100" height="60" rx="10" fill="#1c2740" stroke="#2a3350"/>'
            + f'<circle cx="200" cy="100" r="16" fill="{A}" opacity="0.85"/>'
            + f'<rect x="196" y="130" width="8" height="46" fill="#39445c"/><rect x="184" y="176" width="32" height="8" rx="4" fill="#39445c"/>'
            + _lower("장비 · 시술 도구", "클로즈업 인서트"))
def _quote():
    return ('<rect width="400" height="225" fill="#0e1220"/>' + T(40,86,40,800,"#2a3350","“")
            + '<rect x="60" y="98" width="230" height="15" rx="7" fill="#2a3350"/>'
            + f'<rect x="60" y="122" width="150" height="15" rx="7" fill="{A}" opacity="0.7"/>'
            + _cap("한 줄 강조 자막"))
def _calendar():
    cells = ""
    for r in range(2):
        for c in range(4):
            hot = (r == 1 and c == 2)
            cells += f'<rect x="{70+c*66}" y="{78+r*46}" width="52" height="36" rx="6" fill="{A if hot else "#1a2236"}" stroke="#2a3350"/>'
    return '<rect width="400" height="225" fill="#101726"/>' + cells + _cap("기간 · 경과 일정")

def pick(beat, hosp=""):
    b = (beat.get("block", "") + " " + beat.get("scene", "")).lower()
    tags = " ".join(beat.get("tags", [])).lower()
    T_ = b + " " + tags
    if any(k in b for k in ["콜드오픈", "증상 사격"]): return _cold_open()
    if any(k in b for k in ["응급", "병원 신호", "119"]): return _emergency()
    if any(k in T_ for k in ["전후", "비포", "애프터", "before", "after", "결과 비교"]): return _before_after()
    if any(k in T_ for k in ["top", "3가지", "세 가지", "리스트", "목록", "순위"]): return _list3()
    if any(k in T_ for k in ["체크", "자가진단", "확인 목록", "checklist"]): return _checklist()
    if any(k in T_ for k in ["인터뷰", "문진", "대담", "질문", "상담"]): return _interview()
    if any(k in T_ for k in ["장비", "기기", "레이저", "도구", "device", "시술기"]): return _device()
    if any(k in T_ for k in ["클로즈업", "확대", "피부결", "모공", "부위"]): return _closeup()
    if any(k in T_ for k in ["기간", "경과", "일정", "주차", "개월", "회차"]): return _calendar()
    if any(k in b for k in ["논문", "thi", "근거", "그래프", "수치", "통계"]) or "bgm 0" in tags: return _graph()
    if any(k in b for k in ["통념", "역설", "분할", "오해"]): return _split()
    if any(k in b for k in ["신체 확인", "만져", "지금 해보", "시연", "집에서", "따라"]): return _demo(hosp)
    if any(k in b for k in ["해부", "경동맥", "후두하근", "혈류", "일러스트", "척추", "구조"]): return _anatomy()
    if any(k in T_ for k in ["인용", "한 줄", "명언", "강조 자막"]): return _quote()
    return _talk(hosp)

def frame_html(beat, tc="", hosp=""):
    svg = pick(beat, hosp)
    return (f'<div class="frame"><div class="chrome"><span class="rec"></span>REC</div>'
            f'<div class="ftc">{tc}</div>'
            f'<svg viewBox="0 0 400 225" role="img" aria-label="장면 미리보기">{svg}</svg></div>'
            f'<div class="frame-cap">↑ 화면 컷(스토리보드) · 아래는 촬영·편집 지시</div>')
