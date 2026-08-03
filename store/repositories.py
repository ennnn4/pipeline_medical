"""런타임 리포지토리 — app_rw + RLS 경로용. 테넌트 함수는 'app.hospital_id가 설정된 Connection'을 받는다.

GPT 검토 반영:
 - 테넌트 함수(create_edited_version, approve_version, is_stale)는 engine이 아니라 tenant_conn을 받음
   → app_rw로 호출 시 RLS 아래에서 동작(owner 우회 아님).
 - create_edited_version: 버전·approval_state·콘텐츠(블록/문장/claim)를 한 트랜잭션에 넣고 CAS를 '마지막'에.
   → 중간 장애 시 current가 빈 버전을 가리키지 않음.
 - 승인은 SECURITY DEFINER fn_approve_version 호출(역할검사 + 미검증/미지원 claim 게이트 + UPDATE + audit 원자).
   버전 상태 직접 UPDATE 권한은 app_rw에서 봉쇄.
 - 마이그레이션 lease/heartbeat는 owner/마이그레이션 role(BYPASSRLS)로 실행.

assessment_set_hash 규격: docs/P4 §4. (source checksum/stable_claim_key 확장은 후속.)
"""
import hashlib, json, uuid
from contextlib import contextmanager
from sqlalchemy import text

class Conflict(Exception):
    """동시성 충돌(HTTP 409)."""

_PREFIX = "boncure.v1:"

@contextmanager
def tenant_conn(engine, hospital_id, membership_id=None, request_id=None, user_id=None):
    """app.hospital_id(+선택 app.membership_id, app.request_id, app.user_id)가 트랜잭션에 설정된 Connection.
    app_rw 경로 전용. membership_id·user_id는 인증된 사용자 신원 — 함수가 세션 GUC에서 읽는다(파라미터 신뢰 금지).
    user_id는 platform operator 멤버십 자동 프로비저닝(fn_ensure_platform_operator_membership)이 읽는다."""
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(text("select set_config('app.hospital_id', :h, true)"), {"h": str(hospital_id)})
            conn.execute(text("select set_config('app.membership_id', :m, true)"),
                         {"m": str(membership_id) if membership_id else ""})
            conn.execute(text("select set_config('app.request_id', :r, true)"),
                         {"r": str(request_id) if request_id else ""})
            conn.execute(text("select set_config('app.user_id', :u, true)"),
                         {"u": str(user_id) if user_id else ""})
            yield conn

def _sha(domain, obj):
    payload = _PREFIX + domain + ":" + json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

# ── 해시(conn 기반) ─────────────────────────────────────
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

# ── 편집: 콘텐츠 + current CAS 단일 트랜잭션(conn) ──────
def create_edited_version(conn, hospital_id, script_id, expected_current_version_id, content_fn):
    if content_fn is None:
        raise ValueError("content_fn 필수 — 빈 버전을 current로 만들 수 없음(#5)")
    row = conn.execute(text(
        "select current_version_id, "
        "(select coalesce(max(version_no),0) from script_versions where hospital_id=:h and script_id=:s) as maxno "
        "from scripts where id=:s and hospital_id=:h for update"),
        {"h": hospital_id, "s": script_id}).first()
    if row is None:
        raise Conflict("script 없음(권한/컨텍스트 확인)")
    if row.current_version_id != expected_current_version_id:
        raise Conflict("current_version 변경됨(다른 편집 선반영)")
    new_v = uuid.uuid4(); new_no = row.maxno + 1
    # 작성자 = 세션 membership(app.membership_id GUC). inv11/12 자기승인 판정 근거.
    conn.execute(text("insert into script_versions(id,hospital_id,script_id,parent_version_id,version_no,source,"
                      "created_by_membership_id) values(:v,:h,:s,:p,:n,'editor',"
                      "NULLIF(current_setting('app.membership_id', true), '')::uuid)"),
                 {"v": new_v, "h": hospital_id, "s": script_id, "p": expected_current_version_id, "n": new_no})
    conn.execute(text("insert into version_approval_states(id,hospital_id,version_id,status) values(:i,:h,:v,'none')"),
                 {"i": uuid.uuid4(), "h": hospital_id, "v": new_v})
    # 이전 버전을 superseded로 명시 기록(같은 TX 책임, GPT Step2.5.1). app_rw UPDATE 잠금이라
    # 좁은 definer 함수로만. content_fn(블록 생성)·CAS 전에 계보를 결착.
    conn.execute(text("select fn_mark_version_superseded(:h,:old,:new,"
                      "NULLIF(current_setting('app.membership_id', true), '')::uuid)"),
                 {"h": hospital_id, "old": expected_current_version_id, "new": new_v})
    content_fn(conn, hospital_id, new_v)              # 블록·문장·claim(동일 TX) — CAS 이전
    cnt = conn.execute(text("select count(*) from script_blocks where hospital_id=:h and version_id=:v"),
                       {"h": hospital_id, "v": new_v}).scalar()
    if cnt == 0:
        raise ValueError("content_fn이 블록을 만들지 않음 — 빈 버전 방지(#no-op 차단)")
    r = conn.execute(text("update scripts set current_version_id=:nv, updated_at=now() "
                          "where id=:s and hospital_id=:h and current_version_id is not distinct from :exp"),
                     {"nv": new_v, "s": script_id, "h": hospital_id, "exp": expected_current_version_id})
    if r.rowcount != 1:
        raise Conflict("CAS 실패")
    return new_v

# ── 3단계: 블록 편집 오케스트레이션 ──
def apply_block_edit(conn, hospital_id, script_id, expected_current_version_id, edits, category="tone"):
    """블록 편집 → 새 immutable 버전(콘텐츠+CAS 단일TX) → 변경블록 재분할 → claim 재추출(unverified)
    → 편집이력 기록 → 변경블록 compliance 재검사. 새 버전은 미승인(승인은 4단계 근거검증 후).
    edits: {stable_block_key: new_text}. evidence 판정(direct/partial/inferred)은 절대 하지 않음(4단계)."""
    import uuid as _uuid
    from nlp.segment import segment, SEGMENTER_VERSION
    from store.migrate import is_claim, claim_type_of
    from compliance.rules import check as compliance_check

    changed = {}
    def content_fn(cn, h, new_v):
        rows = cn.execute(text(
            "select stable_block_key, order_index, block_type, scene, text, metadata "
            "from script_blocks where hospital_id=:h and version_id=:pv order by order_index"),
            {"h": h, "pv": expected_current_version_id}).all()
        for r in rows:
            key = r.stable_block_key
            new_text = edits.get(key, r.text)
            bid = _uuid.uuid4()
            cn.execute(text("insert into script_blocks(id,hospital_id,version_id,stable_block_key,order_index,"
                            "block_type,scene,text,metadata) values(:b,:h,:v,:k,:o,:bt,:sc,:tx,:md)"),
                       {"b": bid, "h": h, "v": new_v, "k": key, "o": r.order_index, "bt": r.block_type,
                        "sc": r.scene, "tx": new_text, "md": __import__("json").dumps(r.metadata or {})})
            for si, (s0, s1, st) in enumerate(segment(new_text)):
                sid = _uuid.uuid4()
                cn.execute(text("insert into script_sentences(id,hospital_id,version_id,block_id,sentence_index,"
                                "text,start_offset,end_offset,offset_unit,segmenter_version) "
                                "values(:s,:h,:v,:b,:i,:tx,:a,:z,'codepoint',:sv)"),
                           {"s": sid, "h": h, "v": new_v, "b": bid, "i": si, "tx": st, "a": s0, "z": s1, "sv": SEGMENTER_VERSION})
                if is_claim(st):                            # 모든 블록의 의학주장을 재추출(unverified) — 어떤 claim도 게이트 탈출 못함
                    cn.execute(text("insert into claims(id,hospital_id,version_id,sentence_id,claim_index,"
                                    "claim_text,claim_type,detection_method) values(:c,:h,:v,:s,0,:tx,:ct,'llm')"),
                               {"c": _uuid.uuid4(), "h": h, "v": new_v, "s": sid, "tx": st, "ct": claim_type_of(st)})
            if key in edits:
                changed[key] = (r.text, new_text)

    new_v = create_edited_version(conn, hospital_id, script_id, expected_current_version_id, content_fn)
    findings = {}
    for key, (before, after) in changed.items():
        conn.execute(text("insert into edits(id,hospital_id,script_id,from_version_id,to_version_id,stable_block_key,"
                          "before_text,after_text,category) values(:i,:h,:s,:fv,:tv,:k,:bt,:at,:cat)"),
                     {"i": _uuid.uuid4(), "h": hospital_id, "s": script_id, "fv": expected_current_version_id,
                      "tv": new_v, "k": key, "bt": before, "at": after, "cat": category})
        findings[key] = compliance_check(after)            # 변경 say 금지어 재검사
    return {"version_id": new_v, "changed": list(changed), "compliance": findings}

# ── 승인: fn_approve_version(승인자=세션 membership, 역할+게이트+UPDATE+audit) ──
def approve_version(conn, hospital_id, version_id, policy_version):
    """승인자는 tenant_conn의 membership_id(세션 app.membership_id)로 결합 — 파라미터로 안 받음.
    version advisory lock을 먼저 잡아 hash 계산~승인 사이 콘텐츠/assessment INSERT 직렬화(TOCTOU 차단)."""
    conn.execute(text("select pg_advisory_xact_lock(hashtextextended(:v, 0))"), {"v": str(version_id)})
    ch = version_content_hash(conn, hospital_id, version_id)
    ah = assessment_set_hash(conn, hospital_id, version_id)
    conn.execute(text("select fn_approve_version(:h,:v,:p,:ch,:ah)"),
                 {"h": hospital_id, "v": version_id, "p": policy_version, "ch": ch, "ah": ah})
    return {"content_hash": ch, "assessment_set_hash": ah}

def self_approve_version(conn, hospital_id, version_id, policy_version, reason):
    """자기승인(작성자==승인자) — 별도 함수. allow_self_approval+admin+사유는 DB가 강제."""
    conn.execute(text("select pg_advisory_xact_lock(hashtextextended(:v, 0))"), {"v": str(version_id)})
    ch = version_content_hash(conn, hospital_id, version_id)
    ah = assessment_set_hash(conn, hospital_id, version_id)
    conn.execute(text("select fn_self_approve_version(:h,:v,:p,:ch,:ah,:r)"),
                 {"h": hospital_id, "v": version_id, "p": policy_version, "ch": ch, "ah": ah, "r": reason})
    return {"content_hash": ch, "assessment_set_hash": ah}

def reject_version(conn, hospital_id, version_id, reason):
    """반려(none/pending→rejected, current·사유). DB가 상태·권한 강제."""
    conn.execute(text("select fn_reject_version(:h,:v,:r)"),
                 {"h": hospital_id, "v": version_id, "r": reason})

def revoke_version(conn, hospital_id, version_id, reason):
    """승인 철회(approved→revoked, 비-current 과거승인도 허용·admin·사유). DB가 강제."""
    conn.execute(text("select fn_revoke_version(:h,:v,:r)"),
                 {"h": hospital_id, "v": version_id, "r": reason})

def is_stale(conn, hospital_id, version_id, policy_version):
    st = conn.execute(text(
        "select status, version_content_hash, assessment_set_hash, compliance_policy_version "
        "from version_approval_states where hospital_id=:h and version_id=:v"),
        {"h": hospital_id, "v": version_id}).first()
    if not st or st.status != "approved":
        return True
    return not (st.version_content_hash == version_content_hash(conn, hospital_id, version_id)
                and st.assessment_set_hash == assessment_set_hash(conn, hospital_id, version_id)
                and st.compliance_policy_version == policy_version)

# ── 마이그레이션 lease — owner/마이그레이션 role(BYPASSRLS)로 실행 ──
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
            "lease_expires_at=now()+(:ttl||' seconds')::interval "
            "where id=:i and lease_token=:t and status='pending' "  # fencing: pending + 토큰 일치만
            "and (lease_expires_at is null or lease_expires_at > now())"),
            {"i": import_id, "t": lease_token, "ttl": str(ttl_sec)})
        return r.rowcount
