"""유튜브 벤치마킹(C1~C9) 서비스 테스트 — 실제 PostgreSQL(conftest 하네스).

로컬 PG(55432)에서: python -m pytest tests/test_benchmark.py
LLM 단계는 generator 주입(mock)으로 비용 0. RLS/상태전이/승인게이트/표절검사/dedup 검증.
"""
import uuid
import pytest
from sqlalchemy import text

from services.context import ActorContext
from services.exceptions import Forbidden, ServiceError, InvalidStateTransition
from services import benchmark as bm
from services import similarity as sim


@pytest.fixture(scope="session", autouse=True)
def _schema(owner):
    from store.benchmark import ensure_benchmark_schema
    ensure_benchmark_schema(owner)


def _ctx(tenant, roles=("admin",)):
    return ActorContext(user_id=str(tenant["user_id"]), hospital_id=str(tenant["hospital_id"]),
                        membership_id=str(tenant["membership_id"]), roles=frozenset(roles))


# ── C1/C3: 프로젝트·영상 + RLS 격리 ──
def test_project_video_and_rls(owner, rw, tenant):
    ctx = _ctx(tenant)
    p = bm.create_project(owner, ctx, "  RLS  ")
    assert p["title"] == "RLS" and p["status"] == "draft"
    v = bm.add_video(owner, ctx, p["project_id"], "https://youtu.be/abcDEF12345")
    assert v["video_id"] == "abcDEF12345"
    # 같은 URL 재등록 idempotent
    assert bm.add_video(owner, ctx, p["project_id"], "https://youtu.be/abcDEF12345")["video_ref"] == v["video_ref"]
    # 다른 병원 컨텍스트(app_rw)에선 안 보임
    other = ActorContext(user_id="x", hospital_id=str(uuid.uuid4()),
                         membership_id=str(uuid.uuid4()), roles=frozenset({"admin"}))
    from store.repositories import tenant_conn
    with tenant_conn(rw, other.hospital_id) as cn:
        assert cn.execute(text("select count(*) from benchmark_projects")).scalar() == 0


def test_permission_gate(owner, tenant):
    viewer = _ctx(tenant, roles=())
    with pytest.raises(Forbidden):
        bm.create_project(owner, viewer, "무권한")


# ── C3: 메타 fetch(mock fetcher) ──
def test_fetch_metadata_mock(owner, tenant):
    ctx = _ctx(tenant)
    p = bm.create_project(owner, ctx, "meta")
    bm.add_video(owner, ctx, p["project_id"], "https://youtu.be/abcDEF12345")
    def fetcher(vids):
        return {v: {"title": "T", "view_count": 100, "duration": "PT8M", "caption_status": "available",
                    "channel_name": "C", "channel_id": "c1", "subscriber_count": 9} for v in vids}
    r = bm.fetch_metadata(owner, ctx, p["project_id"], fetcher=fetcher)
    assert r["updated"] == 1
    g = bm.get_project(owner, ctx, p["project_id"])
    assert g["videos"][0]["view_count"] == 100 and g["videos"][0]["duration"] == "PT8M"


# ── C2/C4: 자막 + 분석 ──
def test_transcript_and_analyze(owner, tenant):
    ctx = _ctx(tenant)
    p = bm.create_project(owner, ctx, "an")
    v = bm.add_video(owner, ctx, p["project_id"], "https://youtu.be/abcDEF12345")
    # 자막 없이 분석 → 가드
    with pytest.raises(ServiceError):
        bm.analyze_video(owner, ctx, v["video_ref"], generator=lambda *a, **k: {})
    t = bm.fetch_transcript(owner, ctx, v["video_ref"], pasted_text="이명 자막", try_external=False)
    assert t["status"] == "available" and t["provider"] == "manual"
    r = bm.analyze_video(owner, ctx, v["video_ref"], generator=lambda *a, **k: {"topic": "이명"})
    assert r["analysis"]["topic"] == "이명"
    assert len(bm.list_analyses(owner, ctx, p["project_id"])) == 1


# ── C5/C6: 종합 + 주장후보(전부 pending, dedup) ──
def test_synthesis_and_claims(owner, tenant):
    ctx = _ctx(tenant)
    p = bm.create_project(owner, ctx, "syn")
    v = bm.add_video(owner, ctx, p["project_id"], "https://youtu.be/abcDEF12345")
    bm.fetch_transcript(owner, ctx, v["video_ref"], pasted_text="이명", try_external=False)
    bm.analyze_video(owner, ctx, v["video_ref"], generator=lambda *a, **k: {"topic": "이명"})
    syn = bm.synthesize_project(owner, ctx, p["project_id"], generator=lambda *a, **k: {
        "claims_to_verify": [{"claim_text": "침이 효과", "claim_type": "효과", "intervention": "침"},
                             {"claim_text": "  "}]})
    assert syn["video_count"] == 1
    r = bm.extract_claim_candidates(owner, ctx, p["project_id"])
    assert r["inserted"] == 1  # 빈 주장 skip
    assert bm.extract_claim_candidates(owner, ctx, p["project_id"])["inserted"] == 0  # dedup
    cands = bm.list_claim_candidates(owner, ctx, p["project_id"])
    assert all(c["status"] == "pending_verification" and c["linked_claim_card_id"] is None for c in cands)


# ── C7/C8: 기획안 승인 게이트 + 브릿지 ──
def test_plan_approval_and_bridge(owner, tenant):
    ctx = _ctx(tenant)
    editor = _ctx(tenant, roles=("editor",))
    p = bm.create_project(owner, ctx, "plan")
    v = bm.add_video(owner, ctx, p["project_id"], "https://youtu.be/abcDEF12345")
    bm.fetch_transcript(owner, ctx, v["video_ref"], pasted_text="이명", try_external=False)
    bm.analyze_video(owner, ctx, v["video_ref"], generator=lambda *a, **k: {"topic": "이명"})
    bm.synthesize_project(owner, ctx, p["project_id"], generator=lambda *a, **k: {"claims_to_verify": []})
    plan = bm.generate_plan(owner, ctx, p["project_id"], generator=lambda *a, **k: {"topic": "이명", "outline": []})
    pid = plan["plan_id"]
    # 미승인 기획안으로 생성 브리핑 → 차단
    with pytest.raises(InvalidStateTransition):
        bm.build_generation_brief(owner, ctx, pid)
    # editor는 승인 불가
    with pytest.raises(Forbidden):
        bm.approve_plan(owner, editor, pid)
    assert bm.approve_plan(owner, ctx, pid)["status"] == "approved"
    # 재승인 불가(상태전이 가드)
    with pytest.raises(InvalidStateTransition):
        bm.approve_plan(owner, ctx, pid)
    brief = bm.build_generation_brief(owner, ctx, pid)
    assert "검증 대상" in brief["brief_text"] or brief["topic"] == "이명"
    svid = uuid.uuid4()
    assert bm.link_script_version(owner, ctx, pid, str(svid))["project_status"] == "scripted"


# ── C9: 유사도(순수 로직 + service) ──
def test_similarity_verbatim():
    src = "이명은 귀에서 소리가 나는 증상으로 스트레스와 수면 부족이 주요 악화 요인이며 초기 치료가 중요합니다"
    copy = src
    para = "귀울림은 여러 원인으로 생기며 충분한 휴식과 조기 진료가 도움이 됩니다"
    vo_c = sim.verbatim_overlap(copy, src)
    vo_p = sim.verbatim_overlap(para, src)
    assert sim.risk_level(vo_c["verbatim_score"], vo_c["longest_run_words"]) == "high"
    assert sim.risk_level(vo_p["verbatim_score"], vo_p["longest_run_words"]) == "low"


def test_similarity_service(owner, tenant):
    ctx = _ctx(tenant)
    src = "이명은 귀에서 소리가 나는 증상으로 스트레스와 수면 부족이 주요 악화 요인이며 초기 치료가 중요합니다"
    p = bm.create_project(owner, ctx, "sim")
    v = bm.add_video(owner, ctx, p["project_id"], "https://youtu.be/abcDEF12345")
    with pytest.raises(ServiceError):        # 원본 없음
        bm.check_similarity(owner, ctx, p["project_id"], src)
    bm.fetch_transcript(owner, ctx, v["video_ref"], pasted_text=src, try_external=False)
    assert bm.check_similarity(owner, ctx, p["project_id"], src)["risk"] == "high"
