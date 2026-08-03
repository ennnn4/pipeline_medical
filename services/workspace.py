"""읽기 query service — 버전 워크스페이스 데이터를 두 UI(대시보드·/studio)가 공유.

라우트가 직접 SQL을 짜지 않고 이 함수의 반환(DTO dict)만 렌더. hospital_id는 ctx에서만,
접근 불가 자원은 NotFound. current/effective/approval 계산 규칙의 단일 원본.
available_actions(버튼 노출)는 편의 정보이며 실제 mutation 가능 여부는 쓰기 service가 재검사한다."""
import uuid
from sqlalchemy import text
from store.repositories import tenant_conn
from store import repositories as repo
from services.exceptions import NotFound
from services import images as images_service


def _as_uuid(v):
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


def get_version_workspace(engine, ctx, version_id, policy="policy-1"):
    """버전 편집·근거·승인·이미지 렌더에 필요한 데이터 한 번에.
    반환: {version_id, script_id, version_no, parent_version_id, is_current, approval_status, stale,
           blocks[], claims[], img_keys(set), images_status{}, available_actions{}}."""
    vid = _as_uuid(version_id)
    with tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id,
                     request_id=ctx.request_id) as conn:
        sc = conn.execute(text("select script_id, version_no, parent_version_id "
                               "from script_versions where hospital_id=:h and id=:v"),
                          {"h": ctx.hospital_id, "v": vid}).first()
        if sc is None:
            raise NotFound("버전을 찾을 수 없습니다")
        blocks = [dict(r) for r in conn.execute(text(
            "select stable_block_key, order_index, block_type, text from script_blocks "
            "where hospital_id=:h and version_id=:v order by order_index"),
            {"h": ctx.hospital_id, "v": vid}).mappings()]
        stale = repo.is_stale(conn, _as_uuid(ctx.hospital_id), vid, policy)
        is_current = bool(conn.execute(text("select current_version_id=:v from scripts where id=:s"),
                                       {"v": vid, "s": sc.script_id}).scalar())
        appr_status = conn.execute(text("select status from version_approval_states "
                                        "where hospital_id=:h and version_id=:v"),
                                   {"h": ctx.hospital_id, "v": vid}).scalar() or "none"
        claims = [dict(r) for r in conn.execute(text(
            "select c.id, c.claim_text, e.support_level, e.verification_status, e.medical_risk, "
            "e.assessment_kind, e.human_decision, e.rationale, "
            "(select s.title from claim_sources cs join source_versions sv "
            "  on sv.hospital_id=cs.hospital_id and sv.id=cs.source_version_id "
            "  join sources s on s.hospital_id=sv.hospital_id and s.id=sv.source_id "
            "  where cs.hospital_id=c.hospital_id and cs.claim_id=c.id limit 1) as source_title, "
            "(select cs.source_quote from claim_sources cs "
            "  where cs.hospital_id=c.hospital_id and cs.claim_id=c.id limit 1) as source_quote "
            "from claims c left join claim_effective_assessment e "
            "  on e.hospital_id=c.hospital_id and e.claim_id=c.id "
            "where c.hospital_id=:h and c.version_id=:v order by c.claim_index"),
            {"h": ctx.hospital_id, "v": vid}).mappings()]
        has_img = conn.execute(text("select to_regclass('public.scene_images')")).scalar()
        img_keys = ({r[0] for r in conn.execute(text("select block_key from scene_images where hospital_id=:h"),
                                                {"h": ctx.hospital_id})} if has_img else set())
    images_status = images_service.list_scene_status(engine, ctx, vid) if has_img else {}
    # 편의용 노출 액션(보안 gate 아님 — 쓰기 service가 실제 권한/상태 재검사)
    actions = {
        "can_edit": is_current,
        "can_approve": is_current and appr_status in ("none", "pending"),
        "can_reject": is_current and appr_status in ("none", "pending"),
        "can_revoke": appr_status == "approved",
        "can_export": appr_status == "approved",
    }
    return {"version_id": str(vid), "script_id": str(sc.script_id), "version_no": sc.version_no,
            "parent_version_id": str(sc.parent_version_id) if sc.parent_version_id else None,
            "is_current": is_current, "approval_status": appr_status, "stale": stale,
            "blocks": blocks, "claims": claims, "img_keys": img_keys,
            "images_status": images_status, "available_actions": actions}
