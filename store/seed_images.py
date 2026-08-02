"""scene_images 테이블(장면별 AI 이미지) 추가 + 웹사이즈 적재 — 비파괴 additive(full reset 불필요).

새 테이블만 CREATE(IF NOT EXISTS) + 이미지 upsert + RLS/grant. 기존 데이터 안 건드림.
이미지는 원본 PNG를 웹용 JPEG(~1000px)로 줄여 bytea 저장 → Render 재시작에도 영속(재생성도 영구 반영).

사용: OWNER_URL=<owner url> python -m store.seed_images [이명]
멱등: 재실행 시 이미지 갱신 + 정책 재생성.
"""
import os, io, sys, json
from sqlalchemy import text
from store.db import make_engine

_TENANT_SET = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

DDL = """
CREATE TABLE IF NOT EXISTS scene_images (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hospital_id uuid NOT NULL REFERENCES hospitals(id),
  topic text NOT NULL,
  block_key text NOT NULL,
  mime text NOT NULL DEFAULT 'image/jpeg',
  data bytea NOT NULL,
  prompt text,
  model text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (hospital_id, topic, block_key)
);
"""

# 이미지 provenance(GPT): 어떤 version의 어떤 장면 입력으로 생성했는지 → 대본 변경 시 stale 파생.
_PROVENANCE = [
    "ALTER TABLE scene_images ADD COLUMN IF NOT EXISTS source_version_id uuid;",
    "ALTER TABLE scene_images ADD COLUMN IF NOT EXISTS source_scene_hash text;",   # 생성에 쓴 장면 입력 canonical 해시
    "ALTER TABLE scene_images ADD COLUMN IF NOT EXISTS source_prompt_hash text;",
    "ALTER TABLE scene_images ADD COLUMN IF NOT EXISTS generated_by_membership_id uuid;",
]

def ensure_scene_images(owner_engine):
    """scene_images 테이블 + 정책 + provenance 컬럼(멱등). deploy_bootstrap·image service 전제."""
    with owner_engine.begin() as cn:
        cn.execute(text(DDL))
        for s in _PROVENANCE:
            cn.execute(text(s))
        for s in _policies():
            cn.execute(text(s))

def _policies():
    return [
        "ALTER TABLE scene_images ENABLE ROW LEVEL SECURITY;",
        "ALTER TABLE scene_images FORCE ROW LEVEL SECURITY;",
        "DROP POLICY IF EXISTS si_rw ON scene_images;",
        f"CREATE POLICY si_rw ON scene_images TO app_rw "
        f"USING (hospital_id = {_TENANT_SET}) WITH CHECK (hospital_id = {_TENANT_SET});",
        "DROP POLICY IF EXISTS si_def ON scene_images;",
        "CREATE POLICY si_def ON scene_images TO app_owner USING (true) WITH CHECK (true);",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON scene_images TO app_rw;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON scene_images TO app_owner;",
    ]

def web_jpeg_bytes(raw, maxw=1000, q=85):
    from PIL import Image
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    if im.size[0] > maxw:
        im = im.resize((maxw, int(im.size[1] * maxw / im.size[0])))
    b = io.BytesIO(); im.save(b, "JPEG", quality=q, optimize=True)
    return b.getvalue()

def web_jpeg(path, maxw=1000, q=85):
    return web_jpeg_bytes(io.open(path, "rb").read(), maxw, q)

def run(topic="이명", hospital="boncure"):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    imgdir = os.path.join(root, "data", hospital, "out", "images")
    pj = os.path.join(imgdir, f"{topic}_prompts.json")
    prompts = json.load(io.open(pj, encoding="utf-8"))["prompts"] if os.path.exists(pj) else []
    url = os.environ.get("OWNER_URL") or os.environ.get("DATABASE_URL")
    eng = make_engine(url)
    n = 0
    with eng.begin() as cn:
        cn.execute(text(DDL))
        hid = cn.execute(text("select id from hospitals where slug=:s"), {"s": hospital}).scalar()
        if not hid:
            sys.exit(f"병원 slug={hospital} 없음 — 먼저 시드 필요")
        cn.execute(text("ALTER TABLE scene_images DISABLE ROW LEVEL SECURITY"))  # owner upsert(멱등 재실행 대비)
        for i in range(len(prompts) or 100):
            key = f"blk_{i+1}"
            path = os.path.join(imgdir, f"{topic}_{key}.png")
            if not os.path.exists(path):
                continue
            data = web_jpeg(path)
            cn.execute(text(
                "insert into scene_images(hospital_id,topic,block_key,mime,data,prompt,model) "
                "values(:h,:t,:k,'image/jpeg',:d,:p,'gpt-image-1') "
                "on conflict (hospital_id,topic,block_key) do update set "
                "data=excluded.data, prompt=excluded.prompt, model=excluded.model, updated_at=now()"),
                {"h": hid, "t": topic, "k": key, "d": data, "p": prompts[i] if i < len(prompts) else None})
            n += 1
        for s in _policies():
            cn.execute(text(s))
    print(f"[scene_images] {hospital}/{topic}: {n}장 적재 + RLS 적용 완료")

if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "이명")
