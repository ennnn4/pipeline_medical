"""병원 설정(원장 이름·슬로건·주력 질환) PostgreSQL 영속화.

문제: /new로 만든 병원의 이 값들이 config yaml(임시디스크)에만 있어 재배포 때 소실됐음.
해결: PG hospital_settings에도 저장 → config 소실돼도 PG에서 복구(_ensure_cfg가 사용).
hospitals 핵심 테이블·provision 함수는 안 건드리고 별도 테이블(RLS)로.
"""
import json
from sqlalchemy import text
from store.repositories import tenant_conn

_TENANT_SET = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

_DDL = """
CREATE TABLE IF NOT EXISTS hospital_settings (
  hospital_id uuid PRIMARY KEY REFERENCES hospitals(id),
  host text,
  tagline text,
  diseases jsonb NOT NULL DEFAULT '[]'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);
"""

def _policies():
    return [
        "ALTER TABLE hospital_settings ENABLE ROW LEVEL SECURITY;",
        "ALTER TABLE hospital_settings FORCE ROW LEVEL SECURITY;",
        "DROP POLICY IF EXISTS hs_rw ON hospital_settings;",
        f"CREATE POLICY hs_rw ON hospital_settings TO app_rw "
        f"USING (hospital_id = {_TENANT_SET}) WITH CHECK (hospital_id = {_TENANT_SET});",
        "DROP POLICY IF EXISTS hs_def ON hospital_settings;",
        "CREATE POLICY hs_def ON hospital_settings TO app_owner USING (true) WITH CHECK (true);",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON hospital_settings TO app_rw;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON hospital_settings TO app_owner;",
    ]

def ensure_hospital_settings(owner_engine):
    with owner_engine.begin() as cn:
        cn.execute(text(_DDL))
        for s in _policies():
            cn.execute(text(s))

def save_settings(engine, hospital_id, host=None, tagline=None, diseases=None):
    """병원 설정 upsert(영속). diseases는 리스트."""
    with tenant_conn(engine, hospital_id) as cn:
        cn.execute(text(
            "insert into hospital_settings(hospital_id,host,tagline,diseases) "
            "values(:h,:ho,:tg,cast(:dz as jsonb)) "
            "on conflict (hospital_id) do update set host=excluded.host, tagline=excluded.tagline, "
            "diseases=excluded.diseases, updated_at=now()"),
            {"h": hospital_id, "ho": host, "tg": tagline,
             "dz": json.dumps(list(diseases or []), ensure_ascii=False)})

def get_settings(engine, hospital_id):
    """{host,tagline,diseases} 또는 없으면 None."""
    with tenant_conn(engine, hospital_id) as cn:
        r = cn.execute(text(
            "select host, tagline, diseases from hospital_settings where hospital_id=:h"),
            {"h": hospital_id}).first()
    if not r:
        return None
    return {"host": r.host, "tagline": r.tagline, "diseases": list(r.diseases or [])}
