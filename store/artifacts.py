"""생성 결과물 영속 저장 — Render 무료 티어의 임시 디스크(재배포·잠들 때 초기화) 문제 해결.

문제: 대본 '블록'은 PG(script_blocks)에 있지만, 예쁜 스토리보드 html·풀 패키지(제목·챕터·논문·
근거·이미지 등)는 data/<h>/out/ 디스크에만 있어 재배포하면 사라진다 → ③ 결과물 목록·미리보기가 비어 보임.

해결: 생성 성공 시 out/<topic>_package.json/.evidence.json/.assets.json/.html 4종을 PG(script_artifacts,
bytea)에 저장. 목록은 PG∪disk로 만들고, 미리보기/편집은 디스크에 없으면 PG에서 복원해서 연다.

비파괴 additive. app_rw + tenant_conn(RLS). materials.py 패턴을 그대로 따름.
"""
import io, os
from sqlalchemy import text
from store.repositories import tenant_conn

_TENANT_SET = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"
MAX_ARTIFACT_BYTES = 25 * 1024 * 1024   # 아티팩트 1종 상한(임베드 이미지로 html이 커질 수 있어 안전상한, 초과분은 저장 생략)

# out/ 파일 접미사 ↔ kind
KIND_SUFFIX = {
    "package":  "_package.json",
    "evidence": "_package.evidence.json",
    "assets":   "_package.assets.json",
    "html":     "_package.html",
}

_DDL = """
CREATE TABLE IF NOT EXISTS script_artifacts (
  hospital_id uuid NOT NULL REFERENCES hospitals(id),
  topic text NOT NULL,
  kind text NOT NULL,
  data bytea NOT NULL,
  size_bytes bigint NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (hospital_id, topic, kind)
);
"""

def _policies():
    return [
        "ALTER TABLE script_artifacts ENABLE ROW LEVEL SECURITY;",
        "ALTER TABLE script_artifacts FORCE ROW LEVEL SECURITY;",
        "DROP POLICY IF EXISTS sa_rw ON script_artifacts;",
        f"CREATE POLICY sa_rw ON script_artifacts TO app_rw "
        f"USING (hospital_id = {_TENANT_SET}) WITH CHECK (hospital_id = {_TENANT_SET});",
        "DROP POLICY IF EXISTS sa_def ON script_artifacts;",
        "CREATE POLICY sa_def ON script_artifacts TO app_owner USING (true) WITH CHECK (true);",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON script_artifacts TO app_rw;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON script_artifacts TO app_owner;",
    ]

def ensure_artifacts_schema(owner_engine):
    with owner_engine.begin() as cn:
        cn.execute(text(_DDL))
        for s in _policies():
            cn.execute(text(s))

def save_from_out_dir(engine, hospital_id, topic, out_dir):
    """out_dir의 <topic>_package.* 4종을 읽어 PG에 upsert. 반환: 저장한 kind 목록.
    상한 초과 파일은 건너뜀(경고만). 재현 신뢰가 아니라 '표시 복원'용이라 실패해도 생성은 성공 처리."""
    saved = []
    base = os.path.join(out_dir, f"{os.path.basename(topic)}_package")
    payload = {}
    for kind, suf in KIND_SUFFIX.items():
        p = os.path.join(out_dir, f"{os.path.basename(topic)}{suf}")
        if not os.path.isfile(p):
            continue
        try:
            data = io.open(p, "rb").read()
        except Exception:
            continue
        if len(data) > MAX_ARTIFACT_BYTES:
            continue
        payload[kind] = data
    if not payload:
        return saved
    with tenant_conn(engine, hospital_id) as cn:
        for kind, data in payload.items():
            cn.execute(text(
                "insert into script_artifacts(hospital_id,topic,kind,data,size_bytes,updated_at) "
                "values(:h,:t,:k,:d,:s,now()) "
                "on conflict (hospital_id,topic,kind) do update set data=excluded.data, "
                "size_bytes=excluded.size_bytes, updated_at=now()"),
                {"h": hospital_id, "t": topic, "k": kind, "d": data, "s": len(data)})
            saved.append(kind)
    return saved

def list_topics(engine, hospital_id):
    """결과물이 PG에 있는 topic 목록(package 또는 html 보유). 최신순."""
    with tenant_conn(engine, hospital_id) as cn:
        return [r[0] for r in cn.execute(text(
            "select topic from script_artifacts where hospital_id=:h "
            "and kind in ('package','html') group by topic order by max(updated_at) desc"),
            {"h": hospital_id})]

def restore_to_out_dir(engine, hospital_id, topic, out_dir):
    """PG의 <topic> 아티팩트를 out_dir로 복원(디스크에 없을 때 미리보기/편집용). 반환: 복원한 파일 수.
    이미 있는 파일은 덮지 않음(디스크 최신본 우선)."""
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    with tenant_conn(engine, hospital_id) as cn:
        rows = cn.execute(text("select kind, data from script_artifacts where hospital_id=:h and topic=:t"),
                          {"h": hospital_id, "t": topic}).all()
    for r in rows:
        suf = KIND_SUFFIX.get(r.kind)
        if not suf:
            continue
        dest = os.path.join(out_dir, f"{os.path.basename(topic)}{suf}")
        if os.path.exists(dest):
            continue
        try:
            with io.open(dest, "wb") as f:
                f.write(bytes(r.data))
            n += 1
        except Exception:
            pass
    return n

def has_topic(engine, hospital_id, topic):
    with tenant_conn(engine, hospital_id) as cn:
        return cn.execute(text("select 1 from script_artifacts where hospital_id=:h and topic=:t limit 1"),
                          {"h": hospital_id, "t": topic}).first() is not None
