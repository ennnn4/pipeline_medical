"""4단계 주장·근거 배선 — 전 블록 의학주장 추출 + 논문블록 인용의 LLM 근거판정 적재.

철학(feedback_verify_dont_parrot / asymmetric_risk 준수):
 - 자동 판정은 '원문을 실제로 읽어' 판단한 것만 verified. 근거 없는 주장은 판정 없이 '미검증'으로 둔다.
 - LLM 검증(evidence.llm.json, evidence/llm_verify.py 산출)이 있으면 그걸 우선 사용(의미기반 + 원문 인용).
   없으면 휴리스틱(evidence.json, 저자명+수치)로 fallback.
 - medical_risk는 보수적. 의학 타당성 최종판단은 원장(human_review/override가 automated보다 우선).

RLS 켜기 전(owner) 시드 경로에서 호출.
"""
import io, json, os, hashlib, uuid
from sqlalchemy import text
from nlp.segment import segment, SEGMENTER_VERSION
from store.migrate import is_claim, claim_type_of

def _sha(s):
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def _map_support(support_level, verification_status):
    """LLM/휴리스틱 판정 → (support_level, verification_status, medical_risk, relation_type)."""
    if support_level == "direct" and verification_status == "verified":
        return "direct", "verified", "medium", "directly_supports"
    if support_level == "partial" and verification_status == "verified":
        return "partial", "verified", "medium", "partially_supports"
    return "unsupported", "failed", "high", "context_only"

def _load_results(pkg_path):
    """LLM 결과(우선) 또는 휴리스틱 결과를 통일 포맷 리스트로. 각 원소:
    {claim, source_name, support_level, verification_status, quote, rationale, checker}."""
    llm_path = pkg_path.replace("_package.json", "_package.evidence.llm.json")
    heur_path = pkg_path.replace("_package.json", "_package.evidence.json")
    if os.path.exists(llm_path):
        raw = json.load(io.open(llm_path, encoding="utf-8")).get("results", [])
        out = []
        for r in raw:
            l = r.get("llm") or {}
            out.append({"claim": r.get("claim", ""), "source_name": r.get("source_name") or r.get("source"),
                        "support_level": l.get("support_level"), "verification_status": l.get("verification_status"),
                        "quote": l.get("supporting_quote", ""), "rationale": l.get("rationale", ""),
                        "checker": "llm-verify-1"})
        return out
    if os.path.exists(heur_path):
        raw = json.load(io.open(heur_path, encoding="utf-8")).get("results", [])
        out = []
        for r in raw:
            ok = r.get("verdict") == "OK" and not r.get("nums_missing")
            out.append({"claim": r.get("claim", ""), "source_name": r.get("source_name") or r.get("source"),
                        "support_level": "direct" if ok else "unsupported",
                        "verification_status": "verified" if ok else "failed",
                        "quote": "", "rationale": r.get("opinion", "") + f" [매칭수치:{r.get('nums_found')}]",
                        "checker": "evidence-check-1"})
        return out
    return []

def _sentences(cn, h, v, block_id, block_text):
    ids = []
    for si, (s0, s1, st) in enumerate(segment(block_text)):
        sid = uuid.uuid4(); ids.append((sid, st))
        cn.execute(text("insert into script_sentences(id,hospital_id,version_id,block_id,sentence_index,"
                        "text,start_offset,end_offset,offset_unit,segmenter_version) "
                        "values(:s,:h,:v,:b,:i,:tx,:a,:z,'codepoint',:sv)"),
                   {"s": sid, "h": h, "v": v, "b": block_id, "i": si, "tx": st, "a": s0, "z": s1, "sv": SEGMENTER_VERSION})
    return ids

def seed_claims(cn, hospital_id, version_id, blocks, pkg_path):
    """blocks: [(stable_key, block_id, block_type, text)]. 전 블록 문장분할 후:
      - evidence 블록: 인용 claim + source + claim_source(근거문장 quote) + 자동 assessment(LLM 판정).
      - 그 외 블록: is_claim 문장을 미검증 claim으로 추출(assessment 없음).
    반환: {"claims": n_total, "verified": n_verified}."""
    h, v = hospital_id, version_id
    results = _load_results(pkg_path)
    ev_blocks = [b for b in blocks if b[2] == "evidence"] or blocks[-1:]
    ev_key = ev_blocks[0][0] if ev_blocks else None
    total = verified = 0
    src_cache = {}
    for key, bid, btype, btext in blocks:
        sents = _sentences(cn, h, v, bid, btext)
        if not sents:
            continue
        if key == ev_key and results:
            # 논문 블록: 인용 claim + LLM 근거판정
            for i, r in enumerate(results):
                name = r["source_name"] or f"source-{i}"
                if name not in src_cache:
                    src_id, sv_id = uuid.uuid4(), uuid.uuid4()
                    cn.execute(text("insert into sources(id,hospital_id,title,source_type) values(:s,:h,:t,'paper')"),
                               {"s": src_id, "h": h, "t": name[:500]})
                    cn.execute(text("insert into source_versions(id,hospital_id,source_id,checksum,content_addressed_key,"
                                    "extractor_version,mime) values(:sv,:h,:s,:ck,:key,'pdftotext-1','application/pdf')"),
                               {"sv": sv_id, "h": h, "s": src_id, "ck": _sha(name), "key": "source/" + name[:400]})
                    src_cache[name] = sv_id
                sv_id = src_cache[name]
                sup, vf, risk, rel = _map_support(r["support_level"], r["verification_status"])
                cid = uuid.uuid4()
                cn.execute(text("insert into claims(id,hospital_id,version_id,sentence_id,claim_index,claim_text,"
                                "claim_type,detection_method) values(:c,:h,:v,:s,:ix,:tx,'study_result','migration')"),
                           {"c": cid, "h": h, "v": v, "s": sents[min(i, len(sents) - 1)][0], "ix": i,
                            "tx": (r["claim"] or "")[:2000]})
                cn.execute(text("insert into claim_sources(id,hospital_id,claim_id,source_version_id,source_quote,"
                                "relation_type,confidence) values(:i,:h,:c,:sv,:q,:rel,0.9)"),
                           {"i": uuid.uuid4(), "h": h, "c": cid, "sv": sv_id, "q": (r["quote"] or name)[:2000], "rel": rel})
                cn.execute(text("insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,"
                                "checker_version,source_set_hash,support_level,verification_status,medical_risk,rationale) "
                                "values(:i,:h,:c,'automated',:ik,:cv,:ssh,:sup,:vf,:risk,:ra)"),
                           {"i": uuid.uuid4(), "h": h, "c": cid, "ik": f"auto:{r['checker']}:{i}", "cv": r["checker"],
                            "ssh": _sha(name), "sup": sup, "vf": vf, "risk": risk, "ra": (r["rationale"] or "")[:2000]})
                total += 1
                verified += (vf == "verified")
        else:
            # 일반 블록: 의학주장 추출(미검증)
            ci = 0
            for sid, st in sents:
                if is_claim(st):
                    cn.execute(text("insert into claims(id,hospital_id,version_id,sentence_id,claim_index,claim_text,"
                                    "claim_type,detection_method) values(:c,:h,:v,:s,:ix,:tx,:ct,'llm')"),
                               {"c": uuid.uuid4(), "h": h, "v": v, "s": sid, "ix": ci, "tx": st[:2000],
                                "ct": claim_type_of(st)})
                    ci += 1; total += 1
    return {"claims": total, "verified": verified}
