"""
비트 유형별 스토리보드 SVG 프레임 (파라메트릭). 외부 이미지 0.
beat의 block/scene/tags 키워드로 프레임 유형을 고르고 16:9 SVG inner markup을 반환.
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

def _cold_open():
    return ('<rect width="400" height="225" fill="#0e1220"/>'
            '<rect x="262" y="24" width="104" height="86" rx="4" fill="#12172a" stroke="#2a3350"/>'
            '<circle cx="290" cy="50" r="13" fill="#e9e4cf"/>'
            '<rect x="34" y="128" width="250" height="60" rx="10" fill="#1a2236"/><rect x="44" y="126" width="52" height="30" rx="8" fill="#dfe4f0"/>'
            '<circle cx="86" cy="138" r="13" fill="#c9ccd6"/>'
            f'<g fill="none" stroke="{A}" stroke-width="1.6"><path d="M96 132 q10 -8 20 -2"/><path d="M100 126 q16 -12 30 -3" opacity="0.6"/></g>'
            + _cap("증상 사격 · 인사 없음"))
def _talk(label="원장 정면", sub="본큐어한의원 송정현"):
    return _studio() + _doc(200) + _lower(sub, label)
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
def _demo():
    return (_studio() + _doc(200) + f'<circle cx="176" cy="150" r="9" fill="{SKIN}"/><circle cx="224" cy="150" r="9" fill="{SKIN}"/>'
            + f'<rect x="250" y="40" width="128" height="30" rx="15" fill="{A}"/>' + T(266,60,13,800,W,"지금 해보세요"))

def pick(beat):
    b = (beat.get("block","") + " " + beat.get("scene","")).lower()
    tags = " ".join(beat.get("tags", [])).lower()
    if any(k in b for k in ["콜드오픈","증상 사격"]): return _cold_open()
    if any(k in b for k in ["응급","병원 신호","119"]): return _emergency()
    if any(k in b for k in ["논문","thi","근거","그래프"]) or "bgm 0" in tags: return _graph()
    if any(k in b for k in ["통념","역설","분할"]): return _split()
    if any(k in b for k in ["신체 확인","만져","지금 해보","시연","집에서"]): return _demo()
    if any(k in b for k in ["해부","경동맥","후두하근","혈류","일러스트","척추"]): return _anatomy()
    return _talk()

def frame_html(beat, tc=""):
    svg = pick(beat)
    return (f'<div class="frame"><div class="chrome"><span class="rec"></span>REC</div>'
            f'<div class="ftc">{tc}</div>'
            f'<svg viewBox="0 0 400 225" role="img" aria-label="장면 미리보기">{svg}</svg></div>'
            f'<div class="frame-cap">↑ 화면 컷(스토리보드) · 아래는 촬영·편집 지시</div>')
