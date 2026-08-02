"""승인 기반(Step 2.5) — 작성자/생성유형·superseded 분리·자기승인 설정의 멱등 DDL.

core 테이블(script_versions/version_approval_states/hospitals) additive 변경.
schema.py(create_all 경로)에도 반영돼 있고, 여기서 기존 라이브 DB에 ALTER로 적용(멱등)."""
from sqlalchemy import text

STMTS = [
    # 자기승인 정책(inv12) — 기본 금지
    "ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS allow_self_approval boolean NOT NULL DEFAULT false;",
    # AI 버전의 생성 job 링크(작성자 유형)
    "ALTER TABLE script_versions ADD COLUMN IF NOT EXISTS generation_job_id uuid;",
    # superseded 분리(승인 상태가 아닌 수명주기) — inv14
    "ALTER TABLE version_approval_states ADD COLUMN IF NOT EXISTS superseded_by_version_id uuid;",
    "ALTER TABLE version_approval_states ADD COLUMN IF NOT EXISTS superseded_at timestamptz;",
    # status CHECK 확장(revoked 추가) — 기존 값 부분집합이라 즉시 valid
    "ALTER TABLE version_approval_states DROP CONSTRAINT IF EXISTS ck_version_approval_states_status;",
    "ALTER TABLE version_approval_states ADD CONSTRAINT ck_version_approval_states_status "
    "CHECK (status IN ('none','approved','rejected','revoked'));",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_version_approval_states_revoked_fields') THEN "
    "ALTER TABLE version_approval_states ADD CONSTRAINT ck_version_approval_states_revoked_fields "
    "CHECK (status <> 'revoked' OR (approver_membership_id IS NOT NULL AND decided_at IS NOT NULL)); END IF; END $$;",
    # superseded_by 복합 FK(테넌트)
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_approval_states_superseded_by') THEN "
    "ALTER TABLE version_approval_states ADD CONSTRAINT fk_approval_states_superseded_by "
    "FOREIGN KEY (hospital_id, superseded_by_version_id) REFERENCES script_versions(hospital_id, id) NOT VALID; END IF; END $$;",
    "ALTER TABLE version_approval_states VALIDATE CONSTRAINT fk_approval_states_superseded_by;",
    # generation_job_id 복합 FK(테넌트) — generation_jobs 있을 때만
    "DO $$ BEGIN IF to_regclass('public.generation_jobs') IS NOT NULL AND NOT EXISTS "
    "(SELECT 1 FROM pg_constraint WHERE conname='fk_versions_generation_job') THEN "
    "ALTER TABLE script_versions ADD CONSTRAINT fk_versions_generation_job "
    "FOREIGN KEY (hospital_id, generation_job_id) REFERENCES generation_jobs(hospital_id, id) NOT VALID; END IF; END $$;",
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_versions_generation_job' AND NOT convalidated) THEN "
    "ALTER TABLE script_versions VALIDATE CONSTRAINT fk_versions_generation_job; END IF; END $$;",
    # generation_job_id는 ai 버전만 가질 수 있다(GPT 구조 일관성, 버전 스큐 안전).
    # 코드 버전과 무관하게 성립(과거 ai=NULL도, 신규 ai=job도 통과). editor/migration은 job 링크 없음.
    # 작성자(created_by_membership_id) 채우기·ai→job NOT NULL 강화는 대시보드가 requester membership을
    # 포착하고 새 코드 배포가 확정된 뒤 후속으로.
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_versions_provenance') THEN "
    "ALTER TABLE script_versions ADD CONSTRAINT ck_versions_provenance CHECK ("
    "generation_job_id IS NULL OR source='ai') NOT VALID; END IF; END $$;",
    # 사람 결정 축(waived 모델, GPT) — verification_status(검증결과)와 분리. automated는 NULL.
    "ALTER TABLE claim_assessments ADD COLUMN IF NOT EXISTS human_decision text;",
    "ALTER TABLE claim_assessments ADD COLUMN IF NOT EXISTS decision_reason text;",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_assessment_human_decision') THEN "
    "ALTER TABLE claim_assessments ADD CONSTRAINT ck_assessment_human_decision CHECK ("
    "human_decision IS NULL OR human_decision IN ('accepted','rejected','waived','not_applicable')) NOT VALID; END IF; END $$;",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_assessment_decision_combo') THEN "
    "ALTER TABLE claim_assessments ADD CONSTRAINT ck_assessment_decision_combo CHECK ("
    "human_decision IS NULL "
    "OR (human_decision='accepted' AND verification_status='verified') "
    "OR (human_decision IN ('rejected','waived','not_applicable') AND decision_reason IS NOT NULL AND btrim(decision_reason) <> '')"
    ") NOT VALID; END IF; END $$;",
]

# superseded 기록은 edit/version 트랜잭션 책임(GPT Step2.5.1). version_approval_states는 app_rw UPDATE
# 잠금이라 좁은 SECURITY DEFINER 함수(app_owner 소유)로만 superseded_by/at 기록. 같은 script·멱등 검증.
_FN_SUPERSEDE = """
CREATE OR REPLACE FUNCTION public.fn_mark_version_superseded(
  p_hospital uuid, p_old uuid, p_new uuid, p_actor uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_old_script uuid; v_new_script uuid; v_existing uuid;
BEGIN
  IF p_old = p_new THEN RAISE EXCEPTION 'old=new version' USING ERRCODE='22023'; END IF;
  SELECT script_id INTO v_old_script FROM public.script_versions WHERE hospital_id=p_hospital AND id=p_old;
  SELECT script_id INTO v_new_script FROM public.script_versions WHERE hospital_id=p_hospital AND id=p_new;
  IF v_old_script IS NULL OR v_new_script IS NULL OR v_old_script <> v_new_script THEN
    RAISE EXCEPTION 'version/script mismatch' USING ERRCODE='42501';
  END IF;
  UPDATE public.version_approval_states
     SET superseded_by_version_id=p_new, superseded_at=now(), updated_at=now()
   WHERE hospital_id=p_hospital AND version_id=p_old AND superseded_by_version_id IS NULL;
  IF NOT FOUND THEN     -- 이미 표시됨: 같은 new면 멱등 OK, 다른 값이면 충돌
    SELECT superseded_by_version_id INTO v_existing FROM public.version_approval_states
      WHERE hospital_id=p_hospital AND version_id=p_old;
    IF v_existing IS DISTINCT FROM p_new THEN
      RAISE EXCEPTION 'version already superseded by different version' USING ERRCODE='P2014';
    END IF;
  END IF;
END $$;
"""

_FN_GRANTS = [
    "REVOKE ALL ON FUNCTION public.fn_mark_version_superseded(uuid,uuid,uuid,uuid) FROM PUBLIC;",
    "GRANT EXECUTE ON FUNCTION public.fn_mark_version_superseded(uuid,uuid,uuid,uuid) TO app_rw;",
    "ALTER FUNCTION public.fn_mark_version_superseded(uuid,uuid,uuid,uuid) OWNER TO app_owner;",
]

# DB 백스톱(inv13/GPT): approved/revoked version의 근거(claim_assessments) 변경 금지.
# rls_sql의 trg_lock_assessment(advisory lock)는 그대로 두고, 별도 freeze 트리거를 더한다.
_FN_FREEZE_ASSESS = """
CREATE OR REPLACE FUNCTION public.fn_freeze_assessment_if_decided() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_status text; v_ver uuid;
BEGIN
  SELECT c.version_id INTO v_ver FROM public.claims c
    WHERE c.id = NEW.claim_id AND c.hospital_id = NEW.hospital_id;
  IF v_ver IS NOT NULL THEN
    SELECT status INTO v_status FROM public.version_approval_states
      WHERE hospital_id = NEW.hospital_id AND version_id = v_ver;
    IF v_status IN ('approved','revoked') THEN
      RAISE EXCEPTION '% version의 근거는 변경 불가(새 버전 필요)', v_status USING ERRCODE='P2013';  -- decided_version_frozen
    END IF;
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_freeze_assessment ON claim_assessments;
CREATE TRIGGER trg_freeze_assessment BEFORE INSERT ON claim_assessments
  FOR EACH ROW EXECUTE FUNCTION public.fn_freeze_assessment_if_decided();
"""


def ensure_approval_foundation(owner_engine):
    with owner_engine.begin() as cn:
        for s in STMTS:
            cn.execute(text(s))
        cn.execute(text(_FN_SUPERSEDE))
        for s in _FN_GRANTS:
            cn.execute(text(s))
        cn.execute(text(_FN_FREEZE_ASSESS))
