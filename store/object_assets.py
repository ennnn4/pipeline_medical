"""오브젝트 자산 메타데이터 인덱스 + 스토리지 라우터.

object_assets: 파일 '본문'은 R2에, PG엔 메타데이터(object_key·mime·size·sha256·상태)만.
스토리지 라우터(store_blob/load_blob): R2 활성이면 R2, 아니면 기존 PG bytea 경로로 폴백 →
호출부는 R2 여부를 몰라도 되고, 키가 없을 땐 지금과 100% 동일 동작(무영향).

마이그레이션(키 준비 후): 신규는 R2 저장, 기존 bytea는 배치로 R2 이전 → checksum 검증 → dual-read → bytea 제거.
"""
import os
import hashlib
from sqlalchemy import text
from store.repositories import tenant_conn
from store import object_storage as r2

_TENANT_SET = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

_DDL = """
CREATE TABLE IF NOT EXISTS object_assets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hospital_id uuid NOT NULL REFERENCES hospitals(id),
  kind text NOT NULL,                 -- material | material_version | artifact | scene_image | export
  ref_id text,                        -- 원본 논리키(material_id·topic:kind·block_key 등)
  object_key text NOT NULL,
  original_filename text,
  mime_type text,
  byte_size bigint,
  sha256 text,
  storage_provider text NOT NULL DEFAULT 'r2',
  status text NOT NULL DEFAULT 'active',   -- active | migrating | deleted
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (hospital_id, object_key)
);
"""

def _policies():
    return [
        "ALTER TABLE object_assets ENABLE ROW LEVEL SECURITY;",
        "ALTER TABLE object_assets FORCE ROW LEVEL SECURITY;",
        "DROP POLICY IF EXISTS oa_rw ON object_assets;",
        f"CREATE POLICY oa_rw ON object_assets TO app_rw "
        f"USING (hospital_id = {_TENANT_SET}) WITH CHECK (hospital_id = {_TENANT_SET});",
        "DROP POLICY IF EXISTS oa_def ON object_assets;",
        "CREATE POLICY oa_def ON object_assets TO app_owner USING (true) WITH CHECK (true);",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON object_assets TO app_rw;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON object_assets TO app_owner;",
    ]

def ensure_object_assets(owner_engine):
    with owner_engine.begin() as cn:
        cn.execute(text(_DDL))
        for s in _policies():
            cn.execute(text(s))

def make_key(hospital_id, kind, ref_id, filename="blob"):
    safe = os.path.basename(str(filename)).replace(" ", "_")
    return f"hospitals/{hospital_id}/{kind}/{ref_id}/{safe}"

def put_object(engine, hospital_id, kind, ref_id, data, filename=None, mime=None, created_by=None):
    """R2에 저장 + object_assets 메타 기록. R2 미설정이면 None 반환(호출부가 PG 폴백 판단)."""
    if not r2.enabled():
        return None
    data = bytes(data)
    key = make_key(hospital_id, kind, ref_id, filename or "blob")
    meta = r2.put(key, data, content_type=(mime or "application/octet-stream"))
    with tenant_conn(engine, hospital_id) as cn:
        cn.execute(text(
            "insert into object_assets(hospital_id,kind,ref_id,object_key,original_filename,mime_type,byte_size,sha256,storage_provider,created_by) "
            "values(:h,:k,:r,:ok,:fn,:mt,:sz,:sh,'r2',:by) "
            "on conflict (hospital_id,object_key) do update set byte_size=excluded.byte_size, sha256=excluded.sha256, status='active'"),
            {"h": hospital_id, "k": kind, "r": str(ref_id), "ok": key, "fn": filename,
             "mt": mime, "sz": meta["size"], "sh": meta["sha256"], "by": created_by})
    return {"object_key": key, **meta}

def load_object(engine, hospital_id, kind, ref_id):
    """object_assets에서 이 (kind,ref_id)의 최신 object_key 찾아 R2에서 로드. 없으면 None."""
    if not r2.enabled():
        return None
    with tenant_conn(engine, hospital_id) as cn:
        row = cn.execute(text(
            "select object_key, mime_type from object_assets "
            "where hospital_id=:h and kind=:k and ref_id=:r and status='active' "
            "order by created_at desc limit 1"),
            {"h": hospital_id, "k": kind, "r": str(ref_id)}).first()
    if not row:
        return None
    return {"data": r2.get(row.object_key), "mime": row.mime_type, "object_key": row.object_key}
