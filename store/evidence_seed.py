"""4단계 근거 배선 — 이미 계산된 evidence(check.py 결과)를 DB로 적재.

철학(feedback_verify_dont_parrot / asymmetric_risk 준수):
 - 자동 판정은 '인용 실재 + 수치가 원문 논문에 있음'까지만 verified 처리.
   의학적 근거등급·타당성은 사람(원장) 몫 → medical_risk는 보수적으로 'medium',
   effective view가 human_review/override를 automated보다 우선하므로 언제든 사람이 덮어씀.
 - 근거 확인 없이 direct/partial을 매기지 않는다. verdict/nums_missing 실측에만 근거.

RLS 켜기 전(owner) 시드 경로에서 호출. 반환: 생성한 claim 수.
"""
import io, json, hashlib, uuid
from sqlalchemy import text
from nlp.segment import segment, SEGMENTER_VERSION

_CHECKER = "evidence-check-1"

def _sha(s):
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def _map_verdict(verdict, nums_missing):
    has_missing = bool(nums_missing) and nums_missing not in ("[]", "")
    if verdict == "OK" and not has_missing:
        return "direct", "verified", "medium", "directly_supports"
    if verdict in ("OK", "PARTIAL"):
        return "partial", "verified", "medium", "partially_supports"
    return "unsupported", "failed", "high", "context_only"

def seed_evidence_for_block(cn, hospital_id, version_id, block_id, block_text, results):
    """block_text를 문장분할해 script_sentences 적재 후, results(evidence.json['results'])의
    각 인용을 claim + source + source_version + claim_source + 자동 assessment로 적재."""
    h, v = hospital_id, version_id
    # 1) 논문 블록 문장 분할
    sent_ids = []
    for si, (s0, s1, st) in enumerate(segment(block_text)):
        sid = uuid.uuid4(); sent_ids.append(sid)
        cn.execute(text("insert into script_sentences(id,hospital_id,version_id,block_id,sentence_index,"
                        "text,start_offset,end_offset,offset_unit,segmenter_version) "
                        "values(:s,:h,:v,:b,:i,:tx,:a,:z,'codepoint',:sv)"),
                   {"s": sid, "h": h, "v": v, "b": block_id, "i": si, "tx": st,
                    "a": s0, "z": s1, "sv": SEGMENTER_VERSION})
    if not sent_ids:
        return 0
    src_cache = {}   # source_name → source_version_id
    n = 0
    for i, r in enumerate(results):
        name = r.get("source_name") or r.get("source") or f"source-{i}"
        # 2) source + source_version (제목 기준 dedup)
        if name not in src_cache:
            src_id, sv_id = uuid.uuid4(), uuid.uuid4()
            cn.execute(text("insert into sources(id,hospital_id,title,source_type,citation_metadata) "
                            "values(:s,:h,:t,'paper',cast(:cm as jsonb))"),
                       {"s": src_id, "h": h, "t": name[:500],
                        "cm": json.dumps({"cite": r.get("cite"), "corpus_file": r.get("source")}, ensure_ascii=False)})
            cn.execute(text("insert into source_versions(id,hospital_id,source_id,checksum,content_addressed_key,"
                            "extractor_version,mime) values(:sv,:h,:s,:ck,:key,'pdftotext-1','application/pdf')"),
                       {"sv": sv_id, "h": h, "s": src_id, "ck": _sha(name), "key": "source/" + name[:400]})
            src_cache[name] = sv_id
        sv_id = src_cache[name]
        # 3) claim (논문 블록 문장에 결착)
        support, verif, risk, relation = _map_verdict(r.get("verdict"), r.get("nums_missing"))
        cid = uuid.uuid4()
        cn.execute(text("insert into claims(id,hospital_id,version_id,sentence_id,claim_index,claim_text,"
                        "claim_type,detection_method) values(:c,:h,:v,:s,:ix,:tx,'study_result','migration')"),
                   {"c": cid, "h": h, "v": v, "s": sent_ids[min(i, len(sent_ids) - 1)], "ix": i,
                    "tx": (r.get("claim") or "")[:2000]})
        # 4) claim_source (근거 링크)
        cn.execute(text("insert into claim_sources(id,hospital_id,claim_id,source_version_id,source_quote,"
                        "relation_type,confidence) values(:i,:h,:c,:sv,:q,:rel,0.9)"),
                   {"i": uuid.uuid4(), "h": h, "c": cid, "sv": sv_id, "q": name[:500], "rel": relation})
        # 5) 자동 assessment (실측 근거에만 verified)
        rationale = (r.get("opinion") or "") + f" [출처:{name} · 매칭수치:{r.get('nums_found')}]"
        cn.execute(text("insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,"
                        "checker_version,source_set_hash,support_level,verification_status,medical_risk,rationale) "
                        "values(:i,:h,:c,'automated',:ik,:cv,:ssh,:sup,:vf,:risk,:ra)"),
                   {"i": uuid.uuid4(), "h": h, "c": cid, "ik": f"auto:{_CHECKER}:{i}", "cv": _CHECKER,
                    "ssh": _sha(name), "sup": support, "vf": verif, "risk": risk, "ra": rationale[:2000]})
        n += 1
    return n
