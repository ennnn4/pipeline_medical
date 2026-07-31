"""
기존 out/<topic>_package.json → DB v1 마이그레이션 (비파괴·멱등·커밋전 canonical 검증).

승인 범위: 구조 변환 + claim=unverified 생성 + migration_imports 원자적 기록.
evidence 재검사(per-claim grounding)는 P0 4단계. 여기선 claim을 unverified로만 등록.

원칙(P2.1 §8):
 원본 checksum → JSON검증 → in-memory 변환 → canonical 왕복(메모리) → BEGIN
 → insert(script/version/approval_state/blocks/sentences/claims) + migration_imports=imported(동일 TX)
 → DB 재조회 canonical 비교 → 불일치면 commit 전 rollback → 일치면 commit.
"""
import hashlib, io, json, os, re, uuid
from sqlalchemy import insert, update, select, text as sqltext
import store.schema as S
from nlp.segment import segment, SEGMENTER_VERSION

MIGRATION_VERSION = "m1"

# ── 휴리스틱 ─────────────────────────────────────────────
def block_type_of(name):
    n = name or ""
    pairs = [("intro", ["콜드오픈","오프닝","인트로","인사","면죄"]),
             ("evidence", ["논문","근거","증례","학회지"]),
             ("transition", ["통념","역설","되감기","전환","예고","보상"]),
             ("analogy", ["비유"]),
             ("example", ["사례","고백"]),
             ("summary", ["요약"]),
             ("cta", ["cta","아웃트로","댓글","구독"]),
             ("explanation", ["본론","원인","기전","진단","치료","확인","방법","응급","신호","하지"])]
    low = n.lower()
    for t, keys in pairs:
        if any(k.lower() in low for k in keys):
            return t
    return "other"

_TC = re.compile(r"(\d+):(\d+)")
def tc_to_ms(tc):
    m = _TC.findall(tc or "")
    if not m: return None, None
    def ms(mm, ss): return (int(mm)*60 + int(ss)) * 1000
    a = ms(*m[0]); b = ms(*m[1]) if len(m) > 1 else None
    return a, b

# claim 후보 신호(보수적). 의료 수치·연구·기전·효과.
_NUMU = re.compile(r"\d+(?:\.\d+)?\s*(?:점|dB|데시벨|%|배|회|명|례|kHz|헤르츠)")
_CMP  = re.compile(r"\d+(?:\.\d+)?\s*(?:→|->|~|대)\s*\d+")
_STUDY = ("논문","증례","학회지","연구","Journal","Frontiers","Monitor")
_MECH  = ("기전","원인","연관","호전","개선","효과","자율신경","혈류","신경","근육","해부")
def is_claim(s):
    return bool(_NUMU.search(s) or _CMP.search(s) or any(k in s for k in _STUDY) or any(k in s for k in _MECH))
def claim_type_of(s):
    if _NUMU.search(s) or _CMP.search(s): return "statistic"
    if any(k in s for k in _STUDY): return "study_result"
    if any(k in s for k in ("호전","개선","효과")): return "treatment_effect"
    if any(k in s for k in ("기전","혈류","신경","근육","자율신경")): return "mechanism"
    if any(k in s for k in ("원인","연관")): return "cause"
    return "other"

# ── canonical (원본 보존 필드만: 순서·블록명·tc·scene·say·tags) ──
def canon_from_pkg(pkg):
    out = []
    for i, b in enumerate(pkg.get("script", [])):
        out.append({"order": i, "block": b.get("block",""), "tc": b.get("tc",""),
                    "scene": b.get("scene",""), "say": b.get("say",""), "tags": list(b.get("tags",[]))})
    return out

def canon_from_db(conn, hospital_id, version_id):
    rows = conn.execute(
        select(S.script_blocks.c.order_index, S.script_blocks.c.scene, S.script_blocks.c.text,
               S.script_blocks.c.metadata)
        .where(S.script_blocks.c.hospital_id == hospital_id, S.script_blocks.c.version_id == version_id)
        .order_by(S.script_blocks.c.order_index)).all()
    out = []
    for oi, scene, txt, meta in rows:
        out.append({"order": oi, "block": meta.get("block_label",""), "tc": meta.get("tc",""),
                    "scene": scene or "", "say": txt, "tags": list(meta.get("tags", []))})
    return out

# ── 병원 확보 ────────────────────────────────────────────
def ensure_hospital(conn, slug, name):
    hid = conn.execute(select(S.hospitals.c.id).where(S.hospitals.c.slug == slug)).scalar()
    if hid: return hid
    hid = uuid.uuid4()
    conn.execute(insert(S.hospitals).values(id=hid, slug=slug, name=name))
    return hid

# ── 마이그레이션 ─────────────────────────────────────────
def migrate_package(engine, slug, name, path, topic):
    raw = io.open(path, "rb").read()
    raw_sha = hashlib.sha256(raw).hexdigest()
    pkg = json.loads(raw.decode("utf-8"))
    src_canon = canon_from_pkg(pkg)
    canonical_sha = hashlib.sha256(json.dumps(src_canon, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    with engine.begin() as conn:
        hospital_id = ensure_hospital(conn, slug, name)
        # 멱등: 이미 validated면 skip
        prev = conn.execute(select(S.migration_imports.c.status, S.migration_imports.c.script_id)
            .where(S.migration_imports.c.hospital_id == hospital_id,
                   S.migration_imports.c.source_uri == path,
                   S.migration_imports.c.raw_sha256 == raw_sha,
                   S.migration_imports.c.migration_version == MIGRATION_VERSION)).first()
        if prev and prev[0] in ("imported", "validated"):   # 구조 import 완료분은 재실행 skip(멱등)
            return {"skipped": True, "reason": f"already {prev[0]}", "script_id": prev[1]}

        imp_id = uuid.uuid4()
        conn.execute(insert(S.migration_imports).values(
            id=imp_id, hospital_id=hospital_id, source_uri=path, raw_sha256=raw_sha,
            canonical_sha256=canonical_sha, migration_version=MIGRATION_VERSION, status="pending"))

        script_id = uuid.uuid4(); version_id = uuid.uuid4()
        conn.execute(insert(S.scripts).values(id=script_id, hospital_id=hospital_id, topic=topic, status="draft"))
        conn.execute(insert(S.script_versions).values(
            id=version_id, hospital_id=hospital_id, script_id=script_id, version_no=1,
            source="migration", creation_reason="package.json v1", source_package_hash=raw_sha))
        conn.execute(insert(S.version_approval_states).values(
            id=uuid.uuid4(), hospital_id=hospital_id, version_id=version_id, status="none"))  # 승인 상태행 함께 생성

        n_sent = n_claim = 0
        for oi, b in enumerate(pkg.get("script", [])):
            block_id = uuid.uuid4()
            tcs, tce = tc_to_ms(b.get("tc",""))
            conn.execute(insert(S.script_blocks).values(
                id=block_id, hospital_id=hospital_id, version_id=version_id,
                stable_block_key=f"blk_{oi+1:04d}", order_index=oi,
                block_type=block_type_of(b.get("block","")), scene=b.get("scene",""),
                text=b.get("say",""), tc_start_ms=tcs, tc_end_ms=tce,
                metadata={"block_label": b.get("block",""), "tags": list(b.get("tags",[])),
                          "tc": b.get("tc",""), "migration_inferred": True}))
            for si, (s0, s1, stext) in enumerate(segment(b.get("say",""))):
                sent_id = uuid.uuid4()
                conn.execute(insert(S.script_sentences).values(
                    id=sent_id, hospital_id=hospital_id, version_id=version_id, block_id=block_id,
                    sentence_index=si, text=stext, start_offset=s0, end_offset=s1,
                    offset_unit="codepoint", segmenter_version=SEGMENTER_VERSION))
                n_sent += 1
                if is_claim(stext):
                    claim_id = uuid.uuid4()
                    conn.execute(insert(S.claims).values(
                        id=claim_id, hospital_id=hospital_id, version_id=version_id, sentence_id=sent_id,
                        claim_index=0, claim_text=stext, claim_type=claim_type_of(stext),
                        detection_method="migration"))
                    conn.execute(insert(S.claim_assessments).values(
                        id=uuid.uuid4(), hospital_id=hospital_id, claim_id=claim_id,
                        assessment_kind="migration", idempotency_key=f"migration:{MIGRATION_VERSION}",
                        support_level="unverified", verification_status="pending",
                        medical_risk="high" if claim_type_of(stext) in ("treatment_effect","statistic") else "medium"))
                    n_claim += 1

        conn.execute(update(S.scripts).where(S.scripts.c.id == script_id).values(current_version_id=version_id))
        conn.execute(update(S.migration_imports).where(S.migration_imports.c.id == imp_id)
                     .values(status="imported", script_id=script_id, version_id=version_id,
                             completed_at=sqltext("now()")))

        # 커밋 전 DB 재조회 canonical 왕복 검증
        db_canon = canon_from_db(conn, hospital_id, version_id)
        if db_canon != src_canon:
            raise RuntimeError("canonical 불일치 — commit 전 rollback")  # begin 블록 예외 → 롤백

    return {"skipped": False, "hospital_id": str(hospital_id), "script_id": str(script_id),
            "version_id": str(version_id), "blocks": len(pkg.get("script",[])),
            "sentences": n_sent, "claims": n_claim, "canonical_sha256": canonical_sha}
