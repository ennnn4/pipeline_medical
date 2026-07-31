"""런타임 리포지토리 — 동시편집 CAS, 마이그레이션 lease, 승인+audit(동일 TX), approval_stale, hash.

assessment_set_hash 규격(결정적):
  - UTF-8, canonical JSON(sort_keys, 공백없음 separators=(',',':'))
  - claim_id 오름차순 정렬, **effective assessment만** 포함(승인에 실제 사용된 집합)
  - 필드: support_level, verification_status, medical_risk, assessment_kind  (override는 kind로 식별)
  - 제외: created_at, 랜덤 UUID, rationale 등 비결정 필드
  - domain prefix "boncure.v1:assessment_set:" 후 SHA-256 hex
version_content_hash 규격: 블록을 order_index 순으로 {order,type,scene,text,label,tags} canonical → SHA-256.
정책: stale = (미승인) 또는 (content/assessment_set/policy 해시 중 하나라도 저장값과 불일치).
"""
import hashlib, json, uuid
from sqlalchemy import text

class Conflict(Exception):
    """동시성 충돌(HTTP 409 대응)."""

_PREFIX = "boncure.v1:"

def _sha(domain, obj):
    payload = _PREFIX + domain + ":" + json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

# ── 해시 ─────────────────────────────────────────────
def version_content_hash(conn, hospital_id, version_id):
    rows = conn.execute(text(
        "select order_index, block_type, scene, text, metadata from script_blocks "
        "where hospital_id=:h and version_id=:v order by order_index"), {"h": hospital_id, "v": version_id}).all()
    canon = [{"o": r[0], "type": r[1], "scene": r[2] or "", "text": r[3],
              "label": (r[4] or {}).get("block_label", ""), "tags": (r[4] or {}).get("tags", [])} for r in rows]
    return _sha("content", canon)

def assessment_set_hash(conn, hospital_id, version_id):
    rows = conn.execute(text(
        "select c.id as claim_id, e.support_level, e.verification_status, e.medical_risk, e.assessment_kind "
        "from claims c left join claim_effective_assessment e "
        "  on e.hospital_id=c.hospital_id and e.claim_id=c.id "
        "where c.hospital_id=:h and c.version_id=:v order by c.id"), {"h": hospital_id, "v": version_id}).all()
    canon = [{"claim": str(r.claim_id), "support": r.support_level, "verif": r.verification_status,
              "risk": r.medical_risk, "kind": r.assessment_kind} for r in rows]
    return _sha("assessment_set", canon)

# ── 승인(+audit 동일 TX) ─────────────────────────────
def approve_version(engine, hospital_id, version_id, approver_membership_id, policy_version, _audit_fail=False):
    with engine.begin() as conn:
        ch = version_content_hash(conn, hospital_id, version_id)
        ah = assessment_set_hash(conn, hospital_id, version_id)
        conn.execute(text(
            "update version_approval_states set status='approved', approver_membership_id=:a, "
            "assessment_set_hash=:ah, version_content_hash=:ch, compliance_policy_version=:pv, "
            "decided_at=now(), updated_at=now() where hospital_id=:h and version_id=:v"),
            {"a": approver_membership_id, "ah": ah, "ch": ch, "pv": policy_version, "h": hospital_id, "v": version_id})
        conn.execute(text(
            "insert into audit_events(id,hospital_id,actor_membership_id,action,entity_type,entity_id,after_hash) "
            "values(:i,:h,:a,'approval.approve','version',:v,:ah)"),
            {"i": uuid.uuid4(), "h": hospital_id, "a": approver_membership_id, "v": version_id, "ah": ah})
        if _audit_fail:
            conn.execute(text("insert into audit_events(id,action) values(:i, null)"), {"i": uuid.uuid4()})  # NOT NULL 위반 → 전체 rollback
    return {"content_hash": ch, "assessment_set_hash": ah}

def is_stale(engine, hospital_id, version_id, policy_version):
    with engine.connect() as conn:
        st = conn.execute(text(
            "select status, version_content_hash, assessment_set_hash, compliance_policy_version "
            "from version_approval_states where hospital_id=:h and version_id=:v"),
            {"h": hospital_id, "v": version_id}).first()
        if not st or st.status != "approved":
            return True                                  # 미승인 = 출력 차단
        ch = version_content_hash(conn, hospital_id, version_id)
        ah = assessment_set_hash(conn, hospital_id, version_id)
        return not (st.version_content_hash == ch and st.assessment_set_hash == ah
                    and st.compliance_policy_version == policy_version)

# ── 동시 편집: current_version compare-and-swap ──────
def create_edited_version(engine, hospital_id, script_id, expected_current_version_id):
    with engine.begin() as conn:
        row = conn.execute(text(
            "select current_version_id, "
            "(select coalesce(max(version_no),0) from script_versions where hospital_id=:h and script_id=:s) as maxno "
            "from scripts where id=:s and hospital_id=:h for update"),
            {"h": hospital_id, "s": script_id}).first()
        if row is None:
            raise Conflict("script 없음")
        if row.current_version_id != expected_current_version_id:
            raise Conflict("current_version 변경됨(다른 편집 선반영)")
        new_v = uuid.uuid4(); new_no = row.maxno + 1
        conn.execute(text(
            "insert into script_versions(id,hospital_id,script_id,parent_version_id,version_no,source) "
            "values(:v,:h,:s,:p,:n,'editor')"),
            {"v": new_v, "h": hospital_id, "s": script_id, "p": expected_current_version_id, "n": new_no})
        conn.execute(text("insert into version_approval_states(id,hospital_id,version_id,status) values(:i,:h,:v,'none')"),
                     {"i": uuid.uuid4(), "h": hospital_id, "v": new_v})
        r = conn.execute(text(
            "update scripts set current_version_id=:nv, updated_at=now() "
            "where id=:s and hospital_id=:h and current_version_id is not distinct from :exp"),
            {"nv": new_v, "s": script_id, "h": hospital_id, "exp": expected_current_version_id})
        if r.rowcount != 1:
            raise Conflict("CAS 실패")
        return new_v

# ── 마이그레이션 lease(SKIP LOCKED 원자 획득) ────────
def acquire_lease(engine, worker_id, ttl_sec=300):
    with engine.begin() as conn:
        row = conn.execute(text(
            "update migration_imports mi set worker_id=:w, lease_token=gen_random_uuid(), "
            "lease_expires_at=now()+ (:ttl || ' seconds')::interval, last_heartbeat_at=now(), "
            "attempt_count=attempt_count+1 "
            "where mi.id = (select id from migration_imports where status='pending' "
            "  and (lease_expires_at is null or lease_expires_at < now()) "
            "  order by started_at for update skip locked limit 1) "
            "returning mi.id, mi.lease_token"),
            {"w": worker_id, "ttl": str(ttl_sec)}).first()
        return dict(row._mapping) if row else None

def heartbeat(engine, import_id, lease_token, ttl_sec=300):
    with engine.begin() as conn:
        r = conn.execute(text(
            "update migration_imports set last_heartbeat_at=now(), "
            "lease_expires_at=now()+(:ttl||' seconds')::interval where id=:i and lease_token=:t"),
            {"i": import_id, "t": lease_token, "ttl": str(ttl_sec)})
        return r.rowcount    # 1=해당 워커, 0=토큰 불일치
