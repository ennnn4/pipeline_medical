"""export gate — 최종 산출물은 current이며 approved인 version만(GPT inv14).

짧은 트랜잭션에서 scripts FOR UPDATE + current==requested + approved 확인 후 immutable payload 반환.
파일/문서 생성은 이 payload로 lock 밖에서 수행(긴 작업 동안 DB lock 미보유).
외부 게시(유튜브/CMS 등 되돌리기 어려운 부작용)는 별도 publication_jobs로(현재 미구현)."""
import uuid
from sqlalchemy import text
from store.repositories import tenant_conn
from services.exceptions import NotFound, VersionConflict, ApprovalPrerequisiteFailed, InvalidStateTransition


def _as_uuid(v):
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


def prepare_export(engine, ctx, script_id, version_id):
    """export 가능 여부를 gate로 검증하고 immutable payload 반환.
    - current이 아니면 VersionConflict, approved 아니면 ApprovalPrerequisiteFailed/InvalidStateTransition.
    반환: {script_id, version_id, content_hash, assessment_hash, approved_at, approver, blocks[]}."""
    sid, vid = _as_uuid(script_id), _as_uuid(version_id)
    with tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id,
                     request_id=ctx.request_id) as conn:
        sc = conn.execute(text("select current_version_id from scripts where id=:s and hospital_id=:h for update"),
                          {"s": sid, "h": ctx.hospital_id}).first()
        if sc is None:
            raise NotFound("스크립트를 찾을 수 없습니다")
        if str(sc.current_version_id) != str(vid):
            raise VersionConflict("현재 버전이 아닙니다 — 최신 승인 버전만 내보낼 수 있습니다")
        st = conn.execute(text(
            "select status, version_content_hash, assessment_set_hash, approver_membership_id, decided_at, "
            "approval_event_id, transaction_timestamp() as prepared_at "
            "from version_approval_states where hospital_id=:h and version_id=:v"),
            {"h": ctx.hospital_id, "v": vid}).first()
        if st is None:
            raise NotFound("승인 상태를 찾을 수 없습니다")
        if st.status == "revoked":
            raise InvalidStateTransition("승인이 철회된 버전은 내보낼 수 없습니다")
        if st.status != "approved":
            raise ApprovalPrerequisiteFailed("승인된 버전만 내보낼 수 있습니다")
        blocks = [dict(r._mapping) for r in conn.execute(text(
            "select stable_block_key, order_index, block_type, scene, text from script_blocks "
            "where hospital_id=:h and version_id=:v order by order_index"),
            {"h": ctx.hospital_id, "v": vid})]
    try:                                    # 성공 이벤트(GPT): 내용·slug 미포함, 병원 해시
        from services.observability import emit, hid
        emit("export_prepared", hospital=hid(ctx.hospital_id), blocks=len(blocks),
             request_id=getattr(ctx, "request_id", None))
    except Exception:
        pass
    return {"script_id": str(sid), "version_id": str(vid),
            "content_hash": st.version_content_hash, "assessment_hash": st.assessment_set_hash,
            "approver_membership_id": str(st.approver_membership_id) if st.approver_membership_id else None,
            "approved_at": st.decided_at.isoformat() if st.decided_at else None,
            "approval_event_id": str(st.approval_event_id) if st.approval_event_id else None,
            "export_prepared_at": st.prepared_at.isoformat() if st.prepared_at else None,
            "blocks": blocks}
