"""GPT 코드리뷰 P0 반영 검증 — 생성 job 동시성·상태 무결성·빈결과·adoption."""
import uuid
import pytest
from sqlalchemy import text
from store.repositories import tenant_conn, Conflict
from store import ingest as I


@pytest.fixture
def gen(owner):
    I.ensure_gen_schema(owner)   # 비파괴 채택(신규 DB에서도 성공해야 함)
    return True


def _script():
    return [{"block": "본론", "scene": "", "say": "자율신경이 이명에 관여합니다. THI 54에서 2로 호전.", "tags": []}]


def test_fresh_db_adopt_ok(gen):
    assert gen  # ensure_gen_schema가 예외 없이 완료(조건부 adoption)


def test_request_key_must_be_uuid(rw, tenant, gen):
    with pytest.raises(ValueError):
        I.create_job(rw, tenant["hospital_id"], "t", "not-a-uuid")


def test_create_job_idempotent(rw, tenant, gen):
    h = tenant["hospital_id"]; k = str(uuid.uuid4())
    a = I.create_job(rw, h, "오십견", k)
    b = I.create_job(rw, h, "오십견", k)
    assert a["job_id"] == b["job_id"] and b["reused"] is True


def test_same_key_diff_request_conflict(rw, tenant, gen):
    h = tenant["hospital_id"]; k = str(uuid.uuid4())
    I.create_job(rw, h, "오십견", k)
    with pytest.raises(Conflict):
        I.create_job(rw, h, "다른주제", k)


def test_ingest_lifecycle_and_script_id(rw, tenant, gen):
    h = tenant["hospital_id"]; k = str(uuid.uuid4())
    j = I.create_job(rw, h, "오십견", k)["job_id"]
    I.mark_job(rw, h, j, "generating", allowed_from={"pending"}, started=True)
    I.mark_job(rw, h, j, "generated", allowed_from={"generating"})
    r = I.ingest_content(rw, h, j, "오십견", _script())
    assert r["blocks"] == 1 and not r["reused"]
    # 같은 job·같은 content 재적재 → reused(새 버전 안 만듦)
    assert I.ingest_content(rw, h, j, "오십견", _script())["reused"] is True


def test_completed_not_overwritten_by_late_failed(rw, tenant, gen):
    h = tenant["hospital_id"]; k = str(uuid.uuid4())
    j = I.create_job(rw, h, "오십견", k)["job_id"]
    I.mark_job(rw, h, j, "generating", allowed_from={"pending"})
    I.mark_job(rw, h, j, "generated", allowed_from={"generating"})
    I.ingest_content(rw, h, j, "오십견", _script())          # completed
    ok = I.mark_job(rw, h, j, "failed", allowed_from={"pending", "generating", "generated", "ingesting"})
    assert ok is False   # 전이 안 됨
    with tenant_conn(rw, h) as cn:
        assert cn.execute(text("select status from generation_jobs where id=:j"), {"j": j}).scalar() == "completed"


def test_ingest_rejects_non_ingestable_status(rw, tenant, gen):
    h = tenant["hospital_id"]; k = str(uuid.uuid4())
    j = I.create_job(rw, h, "오십견", k)["job_id"]      # pending
    with pytest.raises(I.InvalidJobState):
        I.ingest_content(rw, h, j, "오십견", _script())


def test_ingest_rejects_empty_blocks(rw, tenant, gen):
    h = tenant["hospital_id"]; k = str(uuid.uuid4())
    j = I.create_job(rw, h, "오십견", k)["job_id"]
    I.mark_job(rw, h, j, "generating", allowed_from={"pending"})
    I.mark_job(rw, h, j, "generated", allowed_from={"generating"})
    with pytest.raises(ValueError):
        I.ingest_content(rw, h, j, "오십견", [{"say": ""}, {"say": "   "}])
