"""대본 장면 → AI 이미지 생성. Claude가 한국어 연출지시를 영어 이미지 프롬프트로 변환(1콜),
OpenAI(gpt-image-1, 실패시 dall-e-3)로 장면별 이미지 생성.

스타일 통일: 깔끔한 현대 의학 인포그래픽 일러스트, 청록/시안 브랜드 팔레트, 흰 배경, 플랫,
글자·숫자·자막 없음, 실존 인물/특정 얼굴 아님, 안전·비자극(환자 사진 아님, 교육용 개념도).

주의: AI 생성물은 '연출 참고용'. 실제 환자사진·논문 그림·의학적 사실의 대체가 아님(원장 확인).
키: OPENAI_API_KEY(.env). 산출: data/<병원>/out/images/blk_NN.png + prompts.json + manifest.json.
사용: python -m assets.gen_images 이명 [--only 6,8]
"""
import os, io, json, base64, sys
from llm.runner import generate as claude

STYLE = ("clean modern medical infographic illustration, soft teal and cyan color palette, "
         "white background, flat vector style with subtle depth, professional healthcare aesthetic, "
         "absolutely no text no letters no numbers no captions no watermark, "
         "not a photograph of a real identifiable person, tasteful non-graphic educational tone, "
         "wide 16:9 composition")

def build_prompts(scenes):
    """scenes: 문자열 또는 {block,scene,say} dict 리스트. 화면 연출 + 대사(핵심 논지)를 함께 읽고
    그 장면이 '실제로 말하는 내용'을 담은 영어 이미지 프롬프트로 변환(화면에 들어갈 사진이므로 논지 반영)."""
    sys_p = ("당신은 의료 교육 유튜브 영상용 이미지 프롬프트 디렉터입니다. 각 장면의 '화면 연출'과 '대사(핵심 논지)'를 "
             "함께 읽고, 그 장면이 실제로 전달하는 내용을 시각적으로 담는 영어 이미지 생성 프롬프트로 변환합니다.")
    user = ("각 장면을, 그 개념·논지를 시각화하는 영어 이미지 프롬프트로 변환하세요.\n"
            "논지 구조를 구도로 반영: 통념 vs 반례=좌우 분할 대비, 전·후=before/after 2분할, Top3·리스트=3분할 카드, "
            "경고·주의=강조된 아이콘/붉은 톤, 해부·기전=깔끔한 교육 일러스트, 비교=나란히 배치, 시술·장비=도구 클로즈업.\n"
            "규칙: 실존 인물/특정 얼굴 금지(익명 실루엣·손·개념도), 글자/숫자/자막 금지, 자극·유혈·혐오 금지, "
            "각 1~2문장, 대사의 요지가 이미지에서 드러나게.\n\n")
    for i, s in enumerate(scenes):
        if isinstance(s, dict):
            line = f'화면: {s.get("scene","")} / 유형: {s.get("block","")}'
            if s.get("say"):
                line += f' / 대사 요지: {s.get("say","")}'
            user += f"[{i+1}] {line}\n"
        else:
            user += f"[{i+1}] {s}\n"
    user += '\n출력 JSON: {"prompts":[...]} — 장면 수와 정확히 동일 길이.'
    r = claude(sys_p, user, parse_json=True, max_tokens=4000)
    return r["prompts"]

def _client():
    from openai import OpenAI
    return OpenAI()  # OPENAI_API_KEY

def _rec_img(r, model):
    try:
        from llm.cost import record
        u = getattr(r, "usage", None)
        if u and getattr(u, "output_tokens", None):
            record("image", model, in_tok=getattr(u, "input_tokens", 0), img_tok=u.output_tokens)
        else:
            record("image", model, flat_usd=0.08, note="usage 없음(추정)")  # dall-e 등
    except Exception:
        pass

def gen_image_bytes(prompt):
    """프롬프트 → PNG 바이트(재생성용). gpt-image-1 실패 시 dall-e-3 폴백."""
    client = _client()
    full = prompt.strip().rstrip(".") + ". " + STYLE
    try:
        r = client.images.generate(model="gpt-image-1", prompt=full, size="1536x1024", quality="medium", n=1)
        _rec_img(r, "gpt-image-1")
        return base64.b64decode(r.data[0].b64_json)
    except Exception:
        import urllib.request
        r = client.images.generate(model="dall-e-3", prompt=full[:3900], size="1792x1024", quality="standard", n=1)
        _rec_img(r, "dall-e-3")
        d0 = r.data[0]
        return base64.b64decode(d0.b64_json) if getattr(d0, "b64_json", None) else urllib.request.urlopen(d0.url).read()

def gen_image(client, prompt, path):
    full = prompt.strip().rstrip(".") + ". " + STYLE
    try:
        r = client.images.generate(model="gpt-image-1", prompt=full, size="1536x1024", quality="medium", n=1)
        _rec_img(r, "gpt-image-1")
        data = base64.b64decode(r.data[0].b64_json)
    except Exception as e:
        # 폴백: dall-e-3 (org 미검증 등으로 gpt-image-1 불가 시)
        import urllib.request
        r = client.images.generate(model="dall-e-3", prompt=full[:3900], size="1792x1024", quality="standard", n=1)
        _rec_img(r, "dall-e-3")
        d0 = r.data[0]
        data = base64.b64decode(d0.b64_json) if getattr(d0, "b64_json", None) else urllib.request.urlopen(d0.url).read()
    with open(path, "wb") as f:
        f.write(data)
    return os.path.getsize(path)

def run(topic="이명", hospital="boncure", only=None):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pkg = json.load(io.open(os.path.join(root, "data", hospital, "out", f"{topic}_package.json"), encoding="utf-8"))
    script = pkg["script"]
    scenes = [{"block": b.get("block", ""), "scene": b.get("scene", ""),
               "say": (b.get("say", "") or "").replace("\n", " ")[:160]} for b in script]
    outdir = os.path.join(root, "data", hospital, "out", "images")
    os.makedirs(outdir, exist_ok=True)
    pj = os.path.join(outdir, f"{topic}_prompts.json")
    if os.path.exists(pj):
        prompts = json.load(io.open(pj, encoding="utf-8"))["prompts"]
        print("prompts: 캐시 사용")
    else:
        print("prompts: Claude로 변환 중...")
        prompts = build_prompts(scenes)
        json.dump({"prompts": prompts}, io.open(pj, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    client = _client()
    manifest = []
    idxs = only if only else range(len(script))
    for i in idxs:
        key = f"blk_{i+1}"
        path = os.path.join(outdir, f"{topic}_{key}.png")
        print(f"[{i+1}/{len(script)}] {key} 생성...", prompts[i][:60])
        sz = gen_image(client, prompts[i], path)
        manifest.append({"block": key, "prompt": prompts[i], "file": os.path.basename(path), "bytes": sz})
        print(f"   저장 {sz//1024}KB")
    mf = os.path.join(outdir, f"{topic}_images_manifest.json")
    json.dump({"images": manifest}, io.open(mf, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("manifest:", mf)

if __name__ == "__main__":
    args = sys.argv[1:]
    topic = args[0] if args and not args[0].startswith("--") else "이명"
    only = None
    if "--only" in args:
        only = [int(x) - 1 for x in args[args.index("--only") + 1].split(",")]
    run(topic, only=only)
