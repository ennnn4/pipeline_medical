"""scripts service — 편집/버전 업무 규칙. 라우트(대시보드·/studio)가 공유.

트랜잭션 경계는 service가 소유(tenant_conn). 권한은 permissions로 검증.
Conflict/SQLSTATE는 service 경계에서 타입 예외로 번역(라우트가 DB 세부 비의존)."""
import uuid
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from store.repositories import tenant_conn, Conflict
from store import repositories as repo
from services import permissions
from services.exceptions import VersionConflict, NotFound, from_sqlstate


def _sqlstate(exc):
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", None)
    if args and isinstance(args[0], dict):
        return args[0].get("C")
    return getattr(orig, "sqlstate", None)


def _as_uuid(v):
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


# ── 읽기(query service) ──
def get_current_version(engine, ctx, script_id):
    with tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id) as cn:
        row = cn.execute(text(
            "select id, current_version_id, topic from scripts where id=:s and hospital_id=:h"),
            {"s": _as_uuid(script_id), "h": ctx.hospital_id}).first()
    if row is None:
        raise NotFound("스크립트를 찾을 수 없습니다")
    return {"script_id": str(row.id), "current_version_id": str(row.current_version_id),
            "topic": row.topic}


def list_versions(engine, ctx, script_id):
    with tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id) as cn:
        rows = cn.execute(text(
            "select v.id, v.version_no, v.source, s.status "
            "from script_versions v left join version_approval_states s "
            "  on s.hospital_id=v.hospital_id and s.version_id=v.id "
            "where v.hospital_id=:h and v.script_id=:sc order by v.version_no"),
            {"h": ctx.hospital_id, "sc": _as_uuid(script_id)}).all()
    return [{"version_id": str(r.id), "version_no": r.version_no,
             "source": r.source, "approval_status": r.status or "none"} for r in rows]


# ── 편집(edit/version service) ──
def edit_blocks(engine, ctx, script_id, base_version_id, edits, category="tone"):
    """블록 편집 → 새 immutable version(콘텐츠+CAS 단일TX). editor/approver/admin만.
    실제로 원문과 달라진 블록만 편집으로 반영(변경 없으면 새 버전 안 만듦 → no_change=True).
    base_version_id(optimistic lock) 불일치는 VersionConflict.
    반환: {version_id, changed, compliance, no_change}."""
    permissions.require(ctx, permissions.EDIT_ROLES)
    sid, bvid = _as_uuid(script_id), _as_uuid(base_version_id)
    try:
        with tenant_conn(engine, ctx.hospital_id, membership_id=ctx.membership_id,
                         request_id=ctx.request_id) as conn:
            cur = {r.stable_block_key: r.text for r in conn.execute(text(
                "select stable_block_key, text from script_blocks where hospital_id=:h and version_id=:v"),
                {"h": ctx.hospital_id, "v": bvid})}
            unknown = [k for k in (edits or {}) if k not in cur]   # base version에 없는 블록은 조용히 무시 금지
            if unknown:
                raise NotFound(f"base version에 없는 블록: {', '.join(sorted(unknown))}")
            changed = {k: v for k, v in (edits or {}).items() if cur.get(k) != v}  # 실제 변경만
            if not changed:
                return {"version_id": None, "changed": [], "compliance": {}, "no_change": True}
            res = repo.apply_block_edit(conn, _as_uuid(ctx.hospital_id), sid, bvid, changed, category=category)
        res["version_id"] = str(res["version_id"])
        res["compliance"] = {k: [f[0] if isinstance(f, (list, tuple)) else str(f) for f in v]
                             for k, v in res.get("compliance", {}).items()}
        res["no_change"] = False
        return res
    except Conflict as e:
        raise VersionConflict(str(e))          # base_version 불일치(다른 편집 선반영)
    except DBAPIError as e:
        raise from_sqlstate(_sqlstate(e), str(e))
