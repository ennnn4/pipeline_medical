"""업로드 자료 영속 저장 — Render 무료 티어의 임시 디스크(재배포·잠들 때 초기화) 문제 해결.

업로드 파일을 PostgreSQL(materials 테이블, bytea)에 저장 → 재시작에도 안 사라짐.
생성(run.py) 직전 disk로 materialize해서 파이프라인이 그대로 읽게 함.

주의(GPT P2): 대용량 파일은 최종적으로 R2/S3 권장. 현재는 bytea(파일당 상한).
비파괴 additive. app_rw + tenant_conn(RLS).
"""
import io, os, hashlib, uuid, mimetypes
from sqlalchemy import text
from store.repositories import tenant_conn

_TENANT_SET = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"
MAX_BYTES = 40 * 1024 * 1024   # 파일당 상한(bytea 부담·과금 방지). 초과분은 disk만.

_DDL = """
CREATE TABLE IF NOT EXISTS materials (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hospital_id uuid NOT NULL REFERENCES hospitals(id),
  filename text NOT NULL,
  mime text,
  size_bytes bigint NOT NULL,
  checksum text,
  data bytea NOT NULL,
  uploaded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (hospital_id, filename)
);
"""

def _policies():
    return [
        "ALTER TABLE materials ENABLE ROW LEVEL SECURITY;",
        "ALTER TABLE materials FORCE ROW LEVEL SECURITY;",
        "DROP POLICY IF EXISTS mat_rw ON materials;",
        f"CREATE POLICY mat_rw ON materials TO app_rw "
        f"USING (hospital_id = {_TENANT_SET}) WITH CHECK (hospital_id = {_TENANT_SET});",
        "DROP POLICY IF EXISTS mat_def ON materials;",
        "CREATE POLICY mat_def ON materials TO app_owner USING (true) WITH CHECK (true);",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON materials TO app_rw;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON materials TO app_owner;",
    ]

def ensure_materials_schema(owner_engine):
    with owner_engine.begin() as cn:
        cn.execute(text(_DDL))
        for s in _policies():
            cn.execute(text(s))

def save_material(engine, hospital_id, filename, raw, mime=None):
    """같은 파일명은 교체(upsert). 상한 초과면 저장 안 함(False)."""
    if len(raw) > MAX_BYTES:
        return False
    mime = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    ck = hashlib.sha256(raw).hexdigest()
    with tenant_conn(engine, hospital_id) as cn:
        cn.execute(text("insert into materials(id,hospital_id,filename,mime,size_bytes,checksum,data) "
                        "values(:i,:h,:f,:m,:s,:c,:d) "
                        "on conflict (hospital_id,filename) do update set "
                        "mime=excluded.mime, size_bytes=excluded.size_bytes, checksum=excluded.checksum, "
                        "data=excluded.data, uploaded_at=now()"),
                   {"i": uuid.uuid4(), "h": hospital_id, "f": filename, "m": mime,
                    "s": len(raw), "c": ck, "d": raw})
    return True

def list_materials(engine, hospital_id):
    with tenant_conn(engine, hospital_id) as cn:
        return [dict(r._mapping) for r in cn.execute(text(
            "select filename, size_bytes, uploaded_at from materials "
            "where hospital_id=:h order by uploaded_at"), {"h": hospital_id})]

def get_material(engine, hospital_id, filename):
    with tenant_conn(engine, hospital_id) as cn:
        r = cn.execute(text("select mime, data from materials where hospital_id=:h and filename=:f"),
                       {"h": hospital_id, "f": filename}).first()
    return (r.mime, bytes(r.data)) if r else (None, None)

def delete_material(engine, hospital_id, filename):
    with tenant_conn(engine, hospital_id) as cn:
        cn.execute(text("delete from materials where hospital_id=:h and filename=:f"),
                   {"h": hospital_id, "f": filename})

def materialize_to_disk(engine, hospital_id, dest_dir):
    """PG의 자료를 dest_dir로 복원(run.py가 읽게). 복원 전, DB에 없는 stale 파일은 제거해
    disk == PG 정합을 보장(삭제된 자료가 생성에 섞이지 않음). 반환: 복원한 파일 수.
    주의: 대시보드는 병원당 job 1개(running 가드)로 직렬화되므로 동시 materialize 없음."""
    os.makedirs(dest_dir, exist_ok=True)
    with tenant_conn(engine, hospital_id) as cn:
        rows = cn.execute(text("select filename, data from materials where hospital_id=:h"),
                          {"h": hospital_id}).all()
    keep = {os.path.basename(r.filename) for r in rows}
    for existing in os.listdir(dest_dir):     # DB에 없는 stale 파일 제거
        if existing not in keep and os.path.isfile(os.path.join(dest_dir, existing)):
            try:
                os.remove(os.path.join(dest_dir, existing))
            except OSError:
                pass
    n = 0
    for r in rows:
        with io.open(os.path.join(dest_dir, os.path.basename(r.filename)), "wb") as f:
            f.write(bytes(r.data))
        n += 1
    return n
