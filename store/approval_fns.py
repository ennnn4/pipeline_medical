"""Step 4 승인 함수 — private core + 정상/자기/반려/철회 wrapper (SECURITY DEFINER, app_owner).

GPT 전략:
- private core(app_rw 직접 실행 불가) + 얇은 wrapper. 정상 승인과 자기승인은 mode만 다르게 core 호출.
- 잠금 순서: scripts FOR UPDATE → version_approval_states FOR UPDATE (편집·승인·검수 직렬화).
- inv2 current version만 승인/반려, inv11/12 작성자≠승인자(자기승인은 별도 override), evidence gate는
  claim별 최신 effective human_decision(accepted+verified/waived/not_applicable, 레거시 automated verified 허용).
- reject: none/pending→rejected(current·사유). revoke: approved→revoked(비-current 과거승인도 허용).
- 모든 상태전이는 같은 TX에서 audit_events 기록(actor·action). ERRCODE: 42501/23514/P0002/P2013/P2015.
"""
from sqlalchemy import text

_ROLE_ACTIVE = (
    "EXISTS (SELECT 1 FROM public.hospital_memberships hm JOIN public.membership_roles mr "
    "ON mr.hospital_id=hm.hospital_id AND mr.membership_id=hm.id "
    "WHERE hm.hospital_id=p_hospital AND hm.id={m} AND hm.archived_at IS NULL AND mr.role IN ({roles}))")

_CORE = """
CREATE OR REPLACE FUNCTION public.fn_approve_core(
  p_hospital uuid, p_version uuid, p_policy text, p_content_hash text, p_assessment_hash text,
  p_mode text, p_reason text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_approver uuid; v_hospital uuid; v_script uuid; v_creator uuid; v_current uuid;
        v_status text; v_is_admin boolean; v_allow_self boolean; v_action text;
BEGIN
  v_approver := NULLIF(current_setting('app.membership_id', true), '')::uuid;
  v_hospital := NULLIF(current_setting('app.hospital_id', true), '')::uuid;
  IF v_approver IS NULL OR v_hospital IS NULL OR v_hospital <> p_hospital THEN
    RAISE EXCEPTION 'session identity required / hospital mismatch' USING ERRCODE='42501';
  END IF;
  -- 콘텐츠·assessment INSERT와 직렬화(freeze 트리거와 동일 advisory lock)
  PERFORM pg_advisory_xact_lock(hashtextextended(p_version::text, 0));
  IF NOT __ROLE_APPROVER__ THEN
    RAISE EXCEPTION 'active approver role required' USING ERRCODE='42501';
  END IF;
  v_is_admin := __ROLE_ADMIN__;
  SELECT script_id, created_by_membership_id INTO v_script, v_creator
    FROM public.script_versions WHERE hospital_id=p_hospital AND id=p_version;
  IF v_script IS NULL THEN RAISE EXCEPTION 'version not found' USING ERRCODE='P0002'; END IF;
  -- scripts 잠금 + current 확인(편집·승인 직렬화, inv2)
  SELECT current_version_id INTO v_current FROM public.scripts
    WHERE hospital_id=p_hospital AND id=v_script FOR UPDATE;
  IF v_current IS DISTINCT FROM p_version THEN
    RAISE EXCEPTION 'approval target is not the current version' USING ERRCODE='P2015';
  END IF;
  SELECT status INTO v_status FROM public.version_approval_states
    WHERE hospital_id=p_hospital AND version_id=p_version FOR UPDATE;
  IF v_status IS NULL THEN RAISE EXCEPTION 'approval state row not found' USING ERRCODE='P0002'; END IF;
  IF v_status NOT IN ('none','pending') THEN
    RAISE EXCEPTION 'version not in approvable state (%)', v_status USING ERRCODE='P2013';
  END IF;
  -- 작성자≠승인자(inv11) / 자기승인 override(inv12)
  IF p_mode = 'self_override' THEN
    IF v_creator IS NULL OR v_creator <> v_approver THEN
      RAISE EXCEPTION 'self-approval requires the actor to be the version author' USING ERRCODE='42501';
    END IF;
    SELECT allow_self_approval INTO v_allow_self FROM public.hospitals WHERE id=p_hospital;
    IF NOT COALESCE(v_allow_self, false) THEN
      RAISE EXCEPTION 'self-approval not allowed for this hospital' USING ERRCODE='42501';
    END IF;
    IF NOT v_is_admin THEN
      RAISE EXCEPTION 'self-approval requires admin (self_override) capability' USING ERRCODE='42501';
    END IF;
    IF p_reason IS NULL OR btrim(p_reason) = '' THEN
      RAISE EXCEPTION 'self-approval requires a reason' USING ERRCODE='23514';
    END IF;
    v_action := 'approval.self_approve';
  ELSE
    IF v_creator IS NOT NULL AND v_creator = v_approver THEN
      RAISE EXCEPTION 'author cannot approve own version' USING ERRCODE='42501';
    END IF;
    v_action := 'approval.approve';
  END IF;
  -- evidence gate: claim별 최신 effective human_decision
  IF EXISTS (SELECT 1 FROM public.claims c
             WHERE c.hospital_id=p_hospital AND c.version_id=p_version
               AND NOT EXISTS (SELECT 1 FROM public.claim_effective_assessment e
                 WHERE e.hospital_id=c.hospital_id AND e.claim_id=c.id
                   AND ((e.human_decision='accepted' AND e.verification_status='verified')
                        OR e.human_decision='waived'
                        OR e.human_decision='not_applicable'
                        OR (e.human_decision IS NULL AND e.verification_status='verified'
                            AND e.support_level NOT IN ('unverified','unsupported'))))) THEN
    RAISE EXCEPTION 'unresolved or unsupported claim blocks approval' USING ERRCODE='23514';
  END IF;
  UPDATE public.version_approval_states SET status='approved', approver_membership_id=v_approver,
    assessment_set_hash=p_assessment_hash, version_content_hash=p_content_hash,
    compliance_policy_version=p_policy, decided_at=now(), updated_at=now()
  WHERE hospital_id=p_hospital AND version_id=p_version;
  INSERT INTO public.audit_events(id,hospital_id,actor_membership_id,action,entity_type,entity_id,after_hash,request_id)
    VALUES(gen_random_uuid(), p_hospital, v_approver, v_action, 'version', p_version, p_assessment_hash,
           NULLIF(current_setting('app.request_id', true), ''));
END $$;
""".replace("__ROLE_APPROVER__", _ROLE_ACTIVE.format(m="v_approver", roles="'approver','admin'")) \
   .replace("__ROLE_ADMIN__", _ROLE_ACTIVE.format(m="v_approver", roles="'admin'"))

_APPROVE = """
CREATE OR REPLACE FUNCTION public.fn_approve_version(
  p_hospital uuid, p_version uuid, p_policy text, p_content_hash text, p_assessment_hash text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN PERFORM public.fn_approve_core(p_hospital,p_version,p_policy,p_content_hash,p_assessment_hash,'normal',NULL); END $$;
"""

_SELF_APPROVE = """
CREATE OR REPLACE FUNCTION public.fn_self_approve_version(
  p_hospital uuid, p_version uuid, p_policy text, p_content_hash text, p_assessment_hash text, p_reason text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN PERFORM public.fn_approve_core(p_hospital,p_version,p_policy,p_content_hash,p_assessment_hash,'self_override',p_reason); END $$;
"""

_REJECT = """
CREATE OR REPLACE FUNCTION public.fn_reject_version(p_hospital uuid, p_version uuid, p_reason text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_actor uuid; v_hospital uuid; v_script uuid; v_current uuid; v_status text;
BEGIN
  v_actor := NULLIF(current_setting('app.membership_id', true), '')::uuid;
  v_hospital := NULLIF(current_setting('app.hospital_id', true), '')::uuid;
  IF v_actor IS NULL OR v_hospital IS NULL OR v_hospital <> p_hospital THEN
    RAISE EXCEPTION 'session identity required / hospital mismatch' USING ERRCODE='42501';
  END IF;
  IF p_reason IS NULL OR btrim(p_reason) = '' THEN RAISE EXCEPTION 'reason required' USING ERRCODE='23514'; END IF;
  IF NOT __ROLE_APPROVER__ THEN RAISE EXCEPTION 'active approver role required' USING ERRCODE='42501'; END IF;
  SELECT script_id INTO v_script FROM public.script_versions WHERE hospital_id=p_hospital AND id=p_version;
  IF v_script IS NULL THEN RAISE EXCEPTION 'version not found' USING ERRCODE='P0002'; END IF;
  SELECT current_version_id INTO v_current FROM public.scripts
    WHERE hospital_id=p_hospital AND id=v_script FOR UPDATE;
  IF v_current IS DISTINCT FROM p_version THEN
    RAISE EXCEPTION 'reject target is not the current version' USING ERRCODE='P2015';
  END IF;
  SELECT status INTO v_status FROM public.version_approval_states
    WHERE hospital_id=p_hospital AND version_id=p_version FOR UPDATE;
  IF v_status IS NULL THEN RAISE EXCEPTION 'approval state row not found' USING ERRCODE='P0002'; END IF;
  IF v_status NOT IN ('none','pending') THEN
    RAISE EXCEPTION 'cannot reject in state (%)', v_status USING ERRCODE='P2013';
  END IF;
  UPDATE public.version_approval_states SET status='rejected', approver_membership_id=v_actor,
    decided_at=now(), updated_at=now()
  WHERE hospital_id=p_hospital AND version_id=p_version;
  INSERT INTO public.audit_events(id,hospital_id,actor_membership_id,action,entity_type,entity_id,request_id)
    VALUES(gen_random_uuid(), p_hospital, v_actor, 'approval.reject', 'version', p_version,
           NULLIF(current_setting('app.request_id', true), ''));
END $$;
""".replace("__ROLE_APPROVER__", _ROLE_ACTIVE.format(m="v_actor", roles="'approver','admin'"))

_REVOKE = """
CREATE OR REPLACE FUNCTION public.fn_revoke_version(p_hospital uuid, p_version uuid, p_reason text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_actor uuid; v_hospital uuid; v_status text;
BEGIN
  v_actor := NULLIF(current_setting('app.membership_id', true), '')::uuid;
  v_hospital := NULLIF(current_setting('app.hospital_id', true), '')::uuid;
  IF v_actor IS NULL OR v_hospital IS NULL OR v_hospital <> p_hospital THEN
    RAISE EXCEPTION 'session identity required / hospital mismatch' USING ERRCODE='42501';
  END IF;
  IF p_reason IS NULL OR btrim(p_reason) = '' THEN RAISE EXCEPTION 'reason required' USING ERRCODE='23514'; END IF;
  IF NOT __ROLE_ADMIN__ THEN RAISE EXCEPTION 'revoke requires admin capability' USING ERRCODE='42501'; END IF;
  -- 과거(비-current) 승인 version도 revoke 가능 → current 확인 없음. 행만 잠금.
  SELECT status INTO v_status FROM public.version_approval_states
    WHERE hospital_id=p_hospital AND version_id=p_version FOR UPDATE;
  IF v_status IS NULL THEN RAISE EXCEPTION 'approval state row not found' USING ERRCODE='P0002'; END IF;
  IF v_status <> 'approved' THEN
    RAISE EXCEPTION 'only approved version can be revoked (%)', v_status USING ERRCODE='P2013';
  END IF;
  -- 원 승인자(approver_membership_id)·decided_at은 유지(revoked_fields CHECK) → 상태만 revoked
  UPDATE public.version_approval_states SET status='revoked', updated_at=now()
  WHERE hospital_id=p_hospital AND version_id=p_version;
  INSERT INTO public.audit_events(id,hospital_id,actor_membership_id,action,entity_type,entity_id,request_id)
    VALUES(gen_random_uuid(), p_hospital, v_actor, 'approval.revoke', 'version', p_version,
           NULLIF(current_setting('app.request_id', true), ''));
END $$;
""".replace("__ROLE_ADMIN__", _ROLE_ACTIVE.format(m="v_actor", roles="'admin'"))

# private core는 app_rw 직접 실행 불가. wrapper만 app_rw EXECUTE. 전부 app_owner 소유.
_GRANTS = [
    "REVOKE ALL ON FUNCTION public.fn_approve_core(uuid,uuid,text,text,text,text,text) FROM PUBLIC;",
    "ALTER FUNCTION public.fn_approve_core(uuid,uuid,text,text,text,text,text) OWNER TO app_owner;",
    "REVOKE ALL ON FUNCTION public.fn_self_approve_version(uuid,uuid,text,text,text,text) FROM PUBLIC;",
    "GRANT EXECUTE ON FUNCTION public.fn_self_approve_version(uuid,uuid,text,text,text,text) TO app_rw;",
    "ALTER FUNCTION public.fn_self_approve_version(uuid,uuid,text,text,text,text) OWNER TO app_owner;",
    "REVOKE ALL ON FUNCTION public.fn_reject_version(uuid,uuid,text) FROM PUBLIC;",
    "GRANT EXECUTE ON FUNCTION public.fn_reject_version(uuid,uuid,text) TO app_rw;",
    "ALTER FUNCTION public.fn_reject_version(uuid,uuid,text) OWNER TO app_owner;",
    "REVOKE ALL ON FUNCTION public.fn_revoke_version(uuid,uuid,text) FROM PUBLIC;",
    "GRANT EXECUTE ON FUNCTION public.fn_revoke_version(uuid,uuid,text) TO app_rw;",
    "ALTER FUNCTION public.fn_revoke_version(uuid,uuid,text) OWNER TO app_owner;",
    # fn_approve_version은 rls_sql에서 이미 app_rw EXECUTE + app_owner. 여기선 core 위임형으로 교체만.
    "ALTER FUNCTION public.fn_approve_version(uuid,uuid,text,text,text) OWNER TO app_owner;",
    # core가 hospitals.allow_self_approval을 읽으므로 definer(app_owner)에 SELECT 필요(멱등).
    "GRANT SELECT ON hospitals TO app_owner;",
]


def ensure_approval_fns(owner_engine):
    """승인 함수 재정의(core+wrappers) + 권한. rls_sql R.apply 이후에 호출(fn_approve_version 위임형으로 교체)."""
    with owner_engine.begin() as cn:
        cn.execute(text(_CORE))
        cn.execute(text(_APPROVE))
        cn.execute(text(_SELF_APPROVE))
        cn.execute(text(_REJECT))
        cn.execute(text(_REVOKE))
        for s in _GRANTS:
            cn.execute(text(s))
