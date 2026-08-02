"""업로드 자료 영속 저장 — Render 무료 티어의 임시 디스크(재배포·잠들 때 초기화) 문제 해결.

업로드 파일을 PostgreSQL(materials 테이블, bytea)에 저장 → 재시작에도 안 사라짐.
생성(run.py) 직전 disk로 materialize해서 파이프라인이 그대로 읽게 함.

주의(GPT P2): 대용량 파일은 최종적으로 R2/S3 권장. 현재는 bytea(파일당 상한).
비파괴 additive. app_rw + tenant_conn(RLS).
"""
import io, os, hashlib, uuid, mimetypes
import json as _json
from sqlalchemy import text
from store.repositories import tenant_conn

_TENANT_SET = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"
MAX_BYTES = 40 * 1024 * 1024   # 파일당 상한(bytea 부담·과금 방지). 초과는 저장 안 하고 명시적 예외.

class MaterialTooLarge(ValueError):
    """파일이 영속 저장 한도(MAX_BYTES) 초과 — 임시 disk fallback 금지, 명시적 실패."""

def _normalize_filename(filename):
    """저장·복원 파일명을 basename으로 정규화(DB unique 기준과 disk 이름 일치)."""
    n = os.path.basename(str(filename)).strip()
    if not n or n in {".", ".."}:
        raise ValueError("유효하지 않은 파일명")
    return n

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

# 자료 immutable 버전(P2-1) — 파일 교체 시 기존 bytes를 UPDATE로 덮지 않고 새 version을 남겨,
# 생성 job이 '그 시점의 정확한 원본'을 material_version_id로 가리켜 실제 재현이 가능하게 한다.
#  materials              : 논리 자료항목 + current_version_id 포인터(+표시용 denormalized 현재값)
#  material_versions      : 불변 이력(파일명·mime·size·checksum·bytes). UPDATE/DELETE 동결 트리거.
#  generation_job_materials: 생성 시점 스냅샷 → material_version_id로 정확한 버전 결착.
_MV_DDL = """
CREATE TABLE IF NOT EXISTS material_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  material_id uuid REFERENCES materials(id) ON DELETE SET NULL,  -- 논리 자료 삭제해도 버전 이력은 보존(재현)
  hospital_id uuid NOT NULL REFERENCES hospitals(id),
  filename text NOT NULL,
  mime text,
  size_bytes bigint NOT NULL,
  checksum text,
  data bytea NOT NULL,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE materials ADD COLUMN IF NOT EXISTS current_version_id uuid;
"""

# material_versions 불변 보장(생성만 허용, 변경·삭제 금지). 재현 신뢰의 근거.
_MV_FREEZE = """
CREATE OR REPLACE FUNCTION public.fn_freeze_material_version() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'material_versions는 불변입니다(삭제 불가)' USING ERRCODE = '0A000';
  END IF;
  -- 내용 컬럼 변경 금지(재현 신뢰). material_id는 부모 삭제 시 FK가 NULL로 바꾸므로 허용.
  IF NEW.id IS DISTINCT FROM OLD.id
     OR NEW.hospital_id IS DISTINCT FROM OLD.hospital_id
     OR NEW.filename IS DISTINCT FROM OLD.filename
     OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
     OR NEW.checksum IS DISTINCT FROM OLD.checksum
     OR NEW.data IS DISTINCT FROM OLD.data
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'material_versions 내용은 불변입니다' USING ERRCODE = '0A000';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_freeze_material_version ON material_versions;
CREATE TRIGGER trg_freeze_material_version BEFORE UPDATE OR DELETE ON material_versions
  FOR EACH ROW EXECUTE FUNCTION public.fn_freeze_material_version();
"""

_SNAP_DDL = """
CREATE TABLE IF NOT EXISTS generation_job_materials (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hospital_id uuid NOT NULL REFERENCES hospitals(id),
  job_id uuid NOT NULL,
  material_id uuid,
  material_version_id uuid,
  filename text NOT NULL,
  checksum text,
  size_bytes bigint,
  snapshot_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE generation_job_materials ADD COLUMN IF NOT EXISTS material_version_id uuid;
"""

def _mv_policies():
    return [
        "ALTER TABLE material_versions ENABLE ROW LEVEL SECURITY;",
        "ALTER TABLE material_versions FORCE ROW LEVEL SECURITY;",
        "DROP POLICY IF EXISTS mv_rw ON material_versions;",
        f"CREATE POLICY mv_rw ON material_versions TO app_rw "
        f"USING (hospital_id = {_TENANT_SET}) WITH CHECK (hospital_id = {_TENANT_SET});",
        "DROP POLICY IF EXISTS mv_def ON material_versions;",
        "CREATE POLICY mv_def ON material_versions TO app_owner USING (true) WITH CHECK (true);",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON material_versions TO app_rw;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON material_versions TO app_owner;",
    ]

def _snap_policies():
    return [
        "ALTER TABLE generation_job_materials ENABLE ROW LEVEL SECURITY;",
        "ALTER TABLE generation_job_materials FORCE ROW LEVEL SECURITY;",
        "DROP POLICY IF EXISTS gjm_rw ON generation_job_materials;",
        f"CREATE POLICY gjm_rw ON generation_job_materials TO app_rw "
        f"USING (hospital_id = {_TENANT_SET}) WITH CHECK (hospital_id = {_TENANT_SET});",
        "DROP POLICY IF EXISTS gjm_def ON generation_job_materials;",
        "CREATE POLICY gjm_def ON generation_job_materials TO app_owner USING (true) WITH CHECK (true);",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON generation_job_materials TO app_rw;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON generation_job_materials TO app_owner;",
    ]

# 기존 materials 행(버전 없던 것)을 material_versions로 백필하고 current_version_id 결착. 멱등.
_BACKFILL = """
INSERT INTO material_versions(id, material_id, hospital_id, filename, mime, size_bytes, checksum, data, created_at)
SELECT gen_random_uuid(), m.id, m.hospital_id, m.filename, m.mime, m.size_bytes, m.checksum, m.data, m.uploaded_at
FROM materials m WHERE m.current_version_id IS NULL;
UPDATE materials m SET current_version_id = v.id
FROM material_versions v
WHERE v.material_id = m.id AND m.current_version_id IS NULL;
"""

# P2-1b(GPT): 스냅샷 관계를 DB 최종 방어선으로 — 복합 테넌트 FK + NOT NULL + 중복금지.
# generation_jobs가 이미 있어야 job FK 가능(guard). 모두 멱등. 백필/정리 뒤 seal 트리거 설치.
_GJM_INTEGRITY = [
    # 복합 FK 타깃 UNIQUE(hospital_id, id)
    "DO $$ BEGIN IF to_regclass('public.generation_jobs') IS NOT NULL AND NOT EXISTS "
    "(SELECT 1 FROM pg_constraint WHERE conname='uq_genjobs_hosp_id') THEN "
    "ALTER TABLE generation_jobs ADD CONSTRAINT uq_genjobs_hosp_id UNIQUE (hospital_id, id); END IF; END $$;",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_matver_hosp_id') THEN "
    "ALTER TABLE material_versions ADD CONSTRAINT uq_matver_hosp_id UNIQUE (hospital_id, id); END IF; END $$;",
    # 과거 gjm(P1, material_version_id NULL) 백필: 자료가 그대로면 current=원본
    "UPDATE generation_job_materials g SET material_version_id = m.current_version_id "
    "FROM materials m WHERE g.material_id = m.id AND g.material_version_id IS NULL AND m.current_version_id IS NOT NULL;",
    # 그래도 NULL(자료 삭제됨 등) = 재현불가 legacy 스냅샷 → 정리
    "DELETE FROM generation_job_materials WHERE material_version_id IS NULL;",
    # (job, version) 중복 제거 후 UNIQUE
    "DELETE FROM generation_job_materials a USING generation_job_materials b "
    "WHERE a.ctid < b.ctid AND a.job_id = b.job_id AND a.material_version_id = b.material_version_id;",
    "ALTER TABLE generation_job_materials ALTER COLUMN material_version_id SET NOT NULL;",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_gjm_job_ver') THEN "
    "ALTER TABLE generation_job_materials ADD CONSTRAINT uq_gjm_job_ver UNIQUE (job_id, material_version_id); END IF; END $$;",
    # 복합 테넌트 FK(병원간 오연결 차단) — ON DELETE RESTRICT, NOT VALID→VALIDATE
    "DO $$ BEGIN IF to_regclass('public.generation_jobs') IS NOT NULL AND NOT EXISTS "
    "(SELECT 1 FROM pg_constraint WHERE conname='fk_gjm_job_tenant') THEN "
    "ALTER TABLE generation_job_materials ADD CONSTRAINT fk_gjm_job_tenant "
    "FOREIGN KEY (hospital_id, job_id) REFERENCES generation_jobs(hospital_id, id) ON DELETE RESTRICT NOT VALID; END IF; END $$;",
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_gjm_job_tenant' AND NOT convalidated) THEN "
    "ALTER TABLE generation_job_materials VALIDATE CONSTRAINT fk_gjm_job_tenant; END IF; END $$;",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_gjm_ver_tenant') THEN "
    "ALTER TABLE generation_job_materials ADD CONSTRAINT fk_gjm_ver_tenant "
    "FOREIGN KEY (hospital_id, material_version_id) REFERENCES material_versions(hospital_id, id) ON DELETE RESTRICT NOT VALID; END IF; END $$;",
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_gjm_ver_tenant' AND NOT convalidated) THEN "
    "ALTER TABLE generation_job_materials VALIDATE CONSTRAINT fk_gjm_ver_tenant; END IF; END $$;",
]

# 스냅샷 봉인: job이 pending 벗어나면 gjm 변경(INSERT/UPDATE/DELETE) 금지 → 과거 입력 구성 위변조 차단.
_SEAL = """
CREATE OR REPLACE FUNCTION public.fn_seal_job_materials() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_status text; v_sealed timestamptz; v_jid uuid;
BEGIN
  v_jid := CASE WHEN TG_OP = 'DELETE' THEN OLD.job_id ELSE NEW.job_id END;
  SELECT status, material_snapshot_at INTO v_status, v_sealed FROM generation_jobs WHERE id = v_jid;
  -- 봉인 완료(material_snapshot_at) 또는 pending 이탈 후에는 스냅샷 변경 금지(원자성 경쟁 차단).
  IF v_sealed IS NOT NULL OR (v_status IS NOT NULL AND v_status <> 'pending') THEN
    RAISE EXCEPTION 'job 스냅샷은 봉인 후 변경 불가(sealed_at=%, status=%)', v_sealed, v_status USING ERRCODE = '0A000';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;
DROP TRIGGER IF EXISTS trg_seal_job_materials ON generation_job_materials;
CREATE TRIGGER trg_seal_job_materials BEFORE INSERT OR UPDATE OR DELETE ON generation_job_materials
  FOR EACH ROW EXECUTE FUNCTION public.fn_seal_job_materials();
"""

def ensure_materials_schema(owner_engine):
    with owner_engine.begin() as cn:
        cn.execute(text(_DDL))
        for s in _policies():
            cn.execute(text(s))
        cn.execute(text(_MV_DDL))
        for s in _mv_policies():
            cn.execute(text(s))
        cn.execute(text(_SNAP_DDL))
        for s in _snap_policies():
            cn.execute(text(s))
        for stmt in _BACKFILL.strip().split(";"):   # 기존 자료 백필(멱등) — freeze 트리거 전에
            if stmt.strip():
                cn.execute(text(stmt))
        cn.execute(text(_MV_FREEZE))                 # 백필 후 불변 트리거 설치
        for s in _GJM_INTEGRITY:                     # 복합 FK·NOT NULL(gjm DML은 seal 트리거 전에)
            cn.execute(text(s))
        cn.execute(text(_SEAL))                      # 마지막: 스냅샷 봉인 트리거

def _manifest_hash(rows):
    """봉인 매니페스트 canonical SHA-256 — ordinal·버전·크기·체크섬 고정 규칙(정렬·UTF-8·무공백)."""
    manifest = [{"ordinal": i,
                 "material_version_id": str(r.material_version_id),
                 "byte_size": r.size_bytes,
                 "content_sha256": r.checksum} for i, r in enumerate(rows)]
    canon = _json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()

def snapshot_job_materials(engine, hospital_id, job_id):
    """생성 시점의 자료를 job에 스냅샷 봉인 — 현재 버전(current_version_id)을 정확히 기록.
    한 트랜잭션에서: job 행 FOR UPDATE 잠금 → INSERT..SELECT(부분혼합 방지) → count·SHA256 manifest·
    material_snapshot_at 기록(봉인). 잠금+봉인시각으로 봉인 원자성 보장(경쟁 GJM 변경 차단).
    이미 봉인된 job이면 None 반환(재호출 무해). 반환: 스냅샷한 자료 수."""
    with tenant_conn(engine, hospital_id) as cn:
        locked = cn.execute(text(
            "select material_snapshot_at, status from generation_jobs "
            "where id=:j and hospital_id=:h for update"),
            {"j": job_id, "h": hospital_id}).first()
        if locked is None:
            raise ValueError("스냅샷 대상 job 없음")
        if locked.material_snapshot_at is not None:
            return None                    # 이미 봉인됨
        if locked.status != "pending":
            raise ValueError(f"스냅샷은 pending에서만 봉인 가능(현재 {locked.status})")
        r = cn.execute(text(
            "insert into generation_job_materials"
            "(id,hospital_id,job_id,material_id,material_version_id,filename,checksum,size_bytes) "
            "select gen_random_uuid(), :h, :j, id, current_version_id, filename, checksum, size_bytes "
            "from materials where hospital_id=:h and current_version_id is not null"),
            {"h": hospital_id, "j": job_id})
        rows = cn.execute(text(
            "select material_version_id, size_bytes, checksum from generation_job_materials "
            "where job_id=:j order by filename, material_version_id::text"),
            {"j": job_id}).all()
        cn.execute(text(
            "update generation_jobs set material_snapshot_count=:c, material_snapshot_hash=:hh, "
            "material_snapshot_at=now() where id=:j and hospital_id=:h"),
            {"c": len(rows), "hh": _manifest_hash(rows), "j": job_id, "h": hospital_id})
        return r.rowcount

def save_material(engine, hospital_id, filename, raw, mime=None, created_by=None):
    """같은 파일명은 새 immutable 버전 추가 + current 포인터 갱신(기존 bytes는 보존).
    상한 초과는 MaterialTooLarge(임시 disk fallback 금지). 반환: 새 version_id."""
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise TypeError("raw는 bytes 계열이어야 함")
    raw = bytes(raw)
    if len(raw) > MAX_BYTES:
        raise MaterialTooLarge(f"업로드 파일은 최대 {MAX_BYTES // (1024 * 1024)}MB까지 저장할 수 있습니다.")
    filename = _normalize_filename(filename)
    mime = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    ck = hashlib.sha256(raw).hexdigest()
    vid = uuid.uuid4()
    with tenant_conn(engine, hospital_id) as cn:
        # 1) 논리 자료항목 확보(신규/기존 모두 id 반환)
        mid = cn.execute(text(
            "insert into materials(id,hospital_id,filename,mime,size_bytes,checksum,data) "
            "values(:i,:h,:f,:m,:s,:c,:d) "
            "on conflict (hospital_id,filename) do update set filename=excluded.filename "
            "returning id"),
            {"i": uuid.uuid4(), "h": hospital_id, "f": filename, "m": mime,
             "s": len(raw), "c": ck, "d": raw}).scalar()
        # 2) 불변 버전 추가(원본 보존)
        cn.execute(text(
            "insert into material_versions(id,material_id,hospital_id,filename,mime,size_bytes,checksum,data,created_by) "
            "values(:v,:mid,:h,:f,:m,:s,:c,:d,:by)"),
            {"v": vid, "mid": mid, "h": hospital_id, "f": filename, "m": mime,
             "s": len(raw), "c": ck, "d": raw, "by": created_by})
        # 3) current 포인터 + denormalized 현재값 갱신(교체 시)
        cn.execute(text(
            "update materials set current_version_id=:v, mime=:m, size_bytes=:s, checksum=:c, "
            "data=:d, uploaded_at=now() where id=:mid"),
            {"v": vid, "m": mime, "s": len(raw), "c": ck, "d": raw, "mid": mid})
    return vid

def list_materials(engine, hospital_id):
    with tenant_conn(engine, hospital_id) as cn:
        return [dict(r._mapping) for r in cn.execute(text(
            "select filename, size_bytes, uploaded_at from materials "
            "where hospital_id=:h order by uploaded_at"), {"h": hospital_id})]

def get_material(engine, hospital_id, filename):
    filename = _normalize_filename(filename)
    with tenant_conn(engine, hospital_id) as cn:
        r = cn.execute(text("select mime, data from materials where hospital_id=:h and filename=:f"),
                       {"h": hospital_id, "f": filename}).first()
    return (r.mime, bytes(r.data)) if r else (None, None)

def delete_material(engine, hospital_id, filename):
    filename = _normalize_filename(filename)
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
    for existing in os.listdir(dest_dir):     # DB에 없는 stale 파일 제거(정합 보장 — 실패 시 중단)
        path = os.path.join(dest_dir, existing)
        if existing not in keep and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError as e:
                raise RuntimeError(f"stale 자료 제거 실패(삭제자료 혼입 위험): {existing}") from e
    n = 0
    for r in rows:
        with io.open(os.path.join(dest_dir, os.path.basename(r.filename)), "wb") as f:
            f.write(bytes(r.data))
        n += 1
    return n

def list_material_versions(engine, hospital_id, filename=None):
    """자료 버전 이력(최신순). filename 지정 시 그 자료만. 재현·감사용."""
    q = ("select v.id, v.filename, v.size_bytes, v.checksum, v.created_at, "
         "(v.id = m.current_version_id) as is_current "
         "from material_versions v left join materials m on m.id = v.material_id "
         "where v.hospital_id = :h" + (" and v.filename = :f" if filename else "") +
         " order by v.created_at desc")
    p = {"h": hospital_id}
    if filename:
        p["f"] = _normalize_filename(filename)
    with tenant_conn(engine, hospital_id) as cn:
        return [dict(r._mapping) for r in cn.execute(text(q), p)]

class SnapshotIntegrityError(RuntimeError):
    """복원한 자료의 크기/체크섬이 스냅샷과 불일치 — 손상된 재현이므로 생성 중단."""

def materialize_job_snapshot(engine, hospital_id, job_id, dest_dir):
    """생성 job이 '그때 사용한 정확한 원본'을 material_version_id로 복원(재현). 반환: 복원 파일 수.
    - current_version이 아니라 오직 스냅샷된 material_version_id 경로만 사용.
    - 복원 bytes의 size·sha256을 스냅샷 값과 재검증(불일치 시 SnapshotIntegrityError).
    - 임시파일 기록 후 atomic rename(부분 파일 방지). dest_dir은 호출자가 job별로 격리."""
    os.makedirs(dest_dir, exist_ok=True)
    with tenant_conn(engine, hospital_id) as cn:
        rows = cn.execute(text(
            "select g.filename, g.checksum, g.size_bytes, v.data, v.checksum as vck "
            "from generation_job_materials g "
            "join material_versions v on v.hospital_id = g.hospital_id and v.id = g.material_version_id "
            "where g.hospital_id = :h and g.job_id = :j"),
            {"h": hospital_id, "j": job_id}).all()
    n = 0
    for r in rows:
        data = bytes(r.data)
        want = r.checksum or r.vck
        if r.size_bytes is not None and len(data) != r.size_bytes:
            raise SnapshotIntegrityError(f"size 불일치: {r.filename} ({len(data)}≠{r.size_bytes})")
        if want and hashlib.sha256(data).hexdigest() != want:
            raise SnapshotIntegrityError(f"checksum 불일치: {r.filename}")
        final = os.path.join(dest_dir, os.path.basename(r.filename))
        tmp = final + ".tmp"
        with io.open(tmp, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, final)     # atomic
        n += 1
    return n
