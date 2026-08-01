"""P0 3단계 앱층 — 편집·승인·diff·버전조회 HTTP API (store/ 기반, 실 PostgreSQL).

SDR 준수:
 - app.hospital_id/app.membership_id는 '서버가 인증 세션에서 결정'해서만 설정(요청 body의 membership 신뢰 금지).
 - request_id를 매 요청 생성해 승인 audit에 배선.
 - 승인은 repositories.approve_version 경로로만(advisory lock 하 hash).
CAS 충돌→409, 역할/권한(42501)→403, 미검증 claim(23514)→422, 없음(P0002)→404.
"""
import os, uuid
from contextlib import contextmanager
from flask import Flask, request, jsonify, session, g, abort, redirect
from markupsafe import escape
from werkzeug.security import check_password_hash
from sqlalchemy import text
from store.db import make_engine
from store import repositories as repo

_CSS = """*{box-sizing:border-box}body{font-family:'Pretendard',-apple-system,'Malgun Gothic',sans-serif;background:#f2f4f6;color:#191f28;margin:0;line-height:1.6}
.wrap{max-width:820px;margin:0 auto;padding:28px 20px}.card{background:#fff;border:1px solid #e5e8eb;border-radius:16px;padding:22px;margin:14px 0}
h1{font-size:22px;letter-spacing:-.03em}h2{font-size:16px;margin:0 0 10px}.badge{font-size:12px;font-weight:800;padding:3px 10px;border-radius:100px}
.stale{background:#fdeaec;color:#f04452}.ok{background:#e6f7f0;color:#12b886}textarea{width:100%;font:inherit;border:1px solid #e5e8eb;border-radius:10px;padding:10px;min-height:64px}
.btn{font:inherit;font-weight:700;border:0;border-radius:10px;padding:10px 18px;background:#3182f6;color:#fff;cursor:pointer}.btn.g{background:#f2f4f6;color:#191f28;border:1px solid #e5e8eb}
label{font-size:12px;color:#8b95a1;font-weight:700}input{width:100%;font:inherit;border:1px solid #e5e8eb;border-radius:10px;padding:10px;margin:6px 0 12px}
.msg{padding:10px 14px;border-radius:10px;margin:10px 0;font-weight:600}.msg.e{background:#fdeaec;color:#f04452}.msg.s{background:#e6f7f0;color:#12b886}
.blk{border-top:1px solid #eef;padding:12px 0}.key{font-size:12px;color:#8b95a1;font-weight:700}small{color:#8b95a1}"""

def _page(title, body):
    return f"<!doctype html><meta charset=utf-8><title>{escape(title)}</title><style>{_CSS}</style><div class=wrap>{body}</div>"


def _u(path):
    """마운트 프리픽스(script_root) 인식 절대경로. 단독 실행 시 ''→경로 그대로,
    DispatcherMiddleware로 /studio 등에 마운트되면 프리픽스를 자동 부착(하드코딩 링크가 프리픽스를 우회하지 않도록)."""
    return (request.script_root or "") + path


_SUPPORT_KO = {"direct": "직접근거", "partial": "부분근거", "inferred": "추론", "unsupported": "근거없음", "unverified": "미검증"}
_KIND_KO = {"automated": "자동검증", "human_review": "원장검수", "override": "원장확정", "migration": "이관"}

def _evidence_panel(claims):
    """4단계: 버전의 의학주장별 유효 근거판정을 카드로. 검증됨=초록, 실패=빨강, 판정없음=회색(미검증).
    자동판정은 '인용·수치 실측'까지만이며 의학 타당성은 원장 확인 몫임을 명시(과신 방지)."""
    if not claims:
        return ('<div class=card><h2>근거 검증 (4단계)</h2>'
                '<p><small>이 버전에 등록된 의학주장이 없습니다.</small></p></div>')
    verified = sum(1 for c in claims if c["verification_status"] == "verified")
    rows = []
    for c in claims:
        vs = c["verification_status"]
        if vs == "verified":
            style, label = "background:#e6f7f0;color:#12b886", "검증됨"
        elif vs == "failed":
            style, label = "background:#fdeaec;color:#f04452", "검증실패"
        else:
            style, label = "background:#f2f4f6;color:#8b95a1", "미검증"
        sup = _SUPPORT_KO.get(c["support_level"], "미검증")
        kind = _KIND_KO.get(c["assessment_kind"], "")
        src = f'<div style="font-size:12px;color:#8b95a1;margin-top:4px">📄 {escape(c["source_title"])}</div>' if c["source_title"] else ""
        rat = f'<div style="font-size:12px;color:#8b95a1;margin-top:2px">{escape((c["rationale"] or "")[:160])}</div>' if c["rationale"] else ""
        rows.append(
            f'<div class=blk><div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
            f'<span class="badge" style="{style}">{label}</span>'
            f'<span class="badge" style="background:#eef4ff;color:#3182f6">{sup}</span>'
            f'{f"<small>{escape(kind)}</small>" if kind else ""}</div>'
            f'<div style="margin-top:6px;font-size:14px">{escape((c["claim_text"] or "")[:220])}</div>{src}{rat}</div>')
    note = ('<p><small>자동검증은 <b>인용 실재·수치 대조</b>까지만 확인합니다. '
            '의학적 근거등급·환자적용은 원장 최종 판단 몫이며, 원장 검수가 자동판정보다 우선합니다.</small></p>')
    return (f'<div class=card><h2>근거 검증 (4단계) — 검증됨 {verified}/{len(claims)}</h2>{note}{"".join(rows)}</div>')


def _sqlstate(exc):
    for o in (getattr(exc, "orig", None), exc):
        try:
            return o.args[0].get("C")
        except Exception:
            pass
    return None


def create_app(engine=None):
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-" + uuid.uuid4().hex)
    app.config["ENGINE"] = engine or make_engine()  # app_rw 엔진(운영은 DATABASE_URL)

    @app.before_request
    def _rid():
        g.request_id = uuid.uuid4().hex

    @contextmanager
    def tenant(slug):
        """slug→hospital_id, 세션 user_id→membership_id(서버 결정) 후 tenant_conn."""
        uid = session.get("user_id")
        if not uid:
            abort(401)
        eng = app.config["ENGINE"]
        # hospital_id (hospitals는 RLS 밖, app_rw SELECT 허용)
        with eng.connect() as c0:
            hid = c0.execute(text("select id from hospitals where slug=:s"), {"s": slug}).scalar()
        if not hid:
            abort(404)
        # membership 결정: hospital 컨텍스트에서 (user_id, hospital, active)로 조회 — 요청값 신뢰 안 함
        with repo.tenant_conn(eng, hid) as c1:
            mid = c1.execute(text("select id from hospital_memberships "
                                  "where hospital_id=:h and user_id=:u and archived_at is null"),
                             {"h": hid, "u": uid}).scalar()
        if not mid:
            abort(403)
        with repo.tenant_conn(eng, hid, membership_id=mid, request_id=g.request_id) as conn:
            yield conn, hid, mid

    def _map_pg(exc):
        code = _sqlstate(exc)
        return {"42501": 403, "23514": 422, "P0002": 404, "2BP01": 409}.get(code, 400), code

    # ── 편집 → 새 버전 ──
    @app.post("/api/h/<slug>/scripts/<script_id>/edit")
    def edit(slug, script_id):
        body = request.get_json(force=True) or {}
        expected = body.get("expected_current_version")
        edits = body.get("edits") or {}
        if not expected or not edits:
            return jsonify(error="expected_current_version과 edits 필요"), 400
        try:
            with tenant(slug) as (conn, hid, mid):
                res = repo.apply_block_edit(conn, hid, uuid.UUID(script_id), uuid.UUID(expected), edits)
            res["version_id"] = str(res["version_id"])
            res["compliance"] = {k: [f[0] if isinstance(f, (list, tuple)) else str(f) for f in v]
                                 for k, v in res["compliance"].items()}
            return jsonify(res), 201
        except repo.Conflict as e:
            return jsonify(error="conflict", detail=str(e)), 409       # 다른 편집 선반영

    # ── 승인 ──
    @app.post("/api/h/<slug>/versions/<version_id>/approve")
    def approve(slug, version_id):
        policy = (request.get_json(force=True) or {}).get("policy", "policy-1")
        try:
            with tenant(slug) as (conn, hid, mid):
                out = repo.approve_version(conn, hid, uuid.UUID(version_id), policy)
            return jsonify(ok=True, **out), 200
        except Exception as e:
            status, code = _map_pg(e)
            if status == 400 and code is None:
                raise
            return jsonify(error="approve_failed", sqlstate=code), status

    # ── 버전 조회(+stale) ──
    @app.get("/api/h/<slug>/versions/<version_id>")
    def get_version(slug, version_id):
        policy = request.args.get("policy", "policy-1")
        with tenant(slug) as (conn, hid, mid):
            blocks = conn.execute(text(
                "select stable_block_key, order_index, block_type, scene, text "
                "from script_blocks where hospital_id=:h and version_id=:v order by order_index"),
                {"h": hid, "v": uuid.UUID(version_id)}).mappings().all()
            stale = repo.is_stale(conn, hid, uuid.UUID(version_id), policy)
        return jsonify(version_id=version_id, stale=stale, blocks=[dict(b) for b in blocks])

    # ── 블록 단위 diff ──
    @app.get("/api/h/<slug>/versions/<version_id>/diff")
    def diff(slug, version_id):
        frm = request.args.get("from")
        if not frm:
            return jsonify(error="from(비교 버전) 필요"), 400
        with tenant(slug) as (conn, hid, mid):
            def blocks(v):
                return {r.stable_block_key: r.text for r in conn.execute(text(
                    "select stable_block_key, text from script_blocks where hospital_id=:h and version_id=:v"),
                    {"h": hid, "v": v})}
            a = blocks(uuid.UUID(frm)); b = blocks(uuid.UUID(version_id))
        changed = [{"key": k, "before": a[k], "after": b[k]} for k in a.keys() & b.keys() if a[k] != b[k]]
        added = [{"key": k, "after": b[k]} for k in b.keys() - a.keys()]
        removed = [{"key": k, "before": a[k]} for k in a.keys() - b.keys()]
        return jsonify(changed=changed, added=added, removed=removed)

    # ══ 최소 UI (로그인·버전편집·승인) ══
    @app.route("/login", methods=["GET", "POST"])
    def login():
        err = ""
        if request.method == "POST":
            email = request.form.get("email", "").strip(); pw = request.form.get("password", "")
            eng = app.config["ENGINE"]
            with eng.connect() as cn:
                row = cn.execute(text("select id, pw_hash from lookup_user_for_login(:e)"), {"e": email}).first()
            if row and row.pw_hash and check_password_hash(row.pw_hash, pw):
                session["user_id"] = str(row.id)
                return redirect(request.args.get("next") or _u("/"))
            err = '<div class="msg e">이메일 또는 비밀번호가 올바르지 않습니다.</div>'
        return _page("로그인", f"<div class=card><h1>로그인</h1>{err}<form method=post>"
                     f"<label>이메일</label><input name=email type=email required>"
                     f"<label>비밀번호</label><input name=password type=password required>"
                     f"<button class=btn type=submit>로그인</button></form></div>")

    @app.get("/logout")
    def logout():
        session.clear(); return redirect(_u("/login"))

    @app.get("/")
    def home():
        if not session.get("user_id"): return redirect(_u("/login"))
        return _page("홈", "<div class=card><h1>대본 편집</h1><p>버전 URL로 접근하세요: "
                     "<code>/ui/h/&lt;slug&gt;/versions/&lt;version_id&gt;</code></p>"
                     f'<a class="btn g" href="{_u("/logout")}">로그아웃</a></div>')

    @app.get("/ui/h/<slug>/versions/<version_id>")
    def ui_version(slug, version_id):
        if not session.get("user_id"):
            return redirect(_u("/login") + "?next=" + _u(f"/ui/h/{slug}/versions/{version_id}"))
        msg = {"approved": '<div class="msg s">승인되었습니다.</div>',
               "e403": '<div class="msg e">승인 권한(approver)이 없습니다.</div>',
               "e422": '<div class="msg e">미검증/미지원 claim이 있어 승인할 수 없습니다(4단계 근거검증 필요).</div>',
               "edited": '<div class="msg s">새 버전이 생성되었습니다(미승인).</div>'}.get(request.args.get("m"), "")
        with tenant(slug) as (conn, hid, mid):
            sc = conn.execute(text("select script_id, version_no, parent_version_id from script_versions where hospital_id=:h and id=:v"),
                              {"h": hid, "v": uuid.UUID(version_id)}).first()
            if not sc: abort(404)
            blocks = conn.execute(text("select stable_block_key, order_index, block_type, text from script_blocks "
                                       "where hospital_id=:h and version_id=:v order by order_index"),
                                  {"h": hid, "v": uuid.UUID(version_id)}).mappings().all()
            stale = repo.is_stale(conn, hid, uuid.UUID(version_id), "policy-1")
            is_current = conn.execute(text("select current_version_id=:v from scripts where id=:s"),
                                      {"v": uuid.UUID(version_id), "s": sc.script_id}).scalar()
            # 4단계: 이 버전의 의학주장 + 유효 근거판정(사람>자동, migration 제외) + 출처
            claims = conn.execute(text(
                "select c.id, c.claim_text, e.support_level, e.verification_status, e.medical_risk, "
                "e.assessment_kind, e.rationale, "
                "(select s.title from claim_sources cs join source_versions sv "
                "  on sv.hospital_id=cs.hospital_id and sv.id=cs.source_version_id "
                "  join sources s on s.hospital_id=sv.hospital_id and s.id=sv.source_id "
                "  where cs.hospital_id=c.hospital_id and cs.claim_id=c.id limit 1) as source_title "
                "from claims c left join claim_effective_assessment e "
                "  on e.hospital_id=c.hospital_id and e.claim_id=c.id "
                "where c.hospital_id=:h and c.version_id=:v order by c.claim_index"),
                {"h": hid, "v": uuid.UUID(version_id)}).mappings().all()
        badge = '<span class="badge stale">미승인/stale</span>' if stale else '<span class="badge ok">승인됨</span>'
        rows = "".join(f'<div class=blk><div class=key>{escape(b["stable_block_key"])} · {escape(b["block_type"])}</div>'
                       f'<textarea name="edit__{escape(b["stable_block_key"])}">{escape(b["text"])}</textarea></div>' for b in blocks)
        editform = (f'<form method=post action="{_u(f"/ui/h/{slug}/scripts/{sc.script_id}/edit")}">'
                    f'<input type=hidden name=expected value="{version_id}">{rows}'
                    f'<button class=btn type=submit>💾 편집 저장(새 버전 생성)</button></form>') if is_current else \
                   f'<p><small>이 버전은 현재 버전이 아니라 편집할 수 없습니다(불변).</small></p>{rows}'
        approve = (f'<form method=post action="{_u(f"/ui/h/{slug}/versions/{version_id}/approve")}" style="margin-top:12px">'
                   f'<button class=btn type=submit>✅ 승인</button></form>') if (is_current and stale) else ""
        diff = f'<a class="btn g" href="{_u(f"/api/h/{slug}/versions/{version_id}/diff")}?from={sc.parent_version_id}">diff(JSON)</a>' if sc.parent_version_id else ""
        evidence = _evidence_panel(claims)
        return _page(f"버전 {sc.version_no}",
                     f'<div class=card><h1>버전 v{sc.version_no} {badge}</h1>{msg}'
                     f'<h2>블록 (편집 → 새 immutable 버전)</h2>{editform}{approve} {diff} '
                     f'<a class="btn g" href="{_u("/logout")}">로그아웃</a></div>{evidence}')

    @app.post("/ui/h/<slug>/scripts/<script_id>/edit")
    def ui_edit(slug, script_id):
        if not session.get("user_id"): abort(401)
        expected = request.form.get("expected")
        try:
            exp_uuid, sc_uuid = uuid.UUID(expected), uuid.UUID(script_id)   # 폼 누락/오형식 → 400(500 방지)
        except (TypeError, ValueError):
            abort(400)
        edits = {k[6:]: v for k, v in request.form.items() if k.startswith("edit__")}
        try:
            with tenant(slug) as (conn, hid, mid):
                # 원문과 다른 블록만 편집으로 간주(apply_block_edit이 변경분만 처리)
                cur = {r.stable_block_key: r.text for r in conn.execute(text(
                    "select stable_block_key, text from script_blocks where hospital_id=:h and version_id=:v"),
                    {"h": hid, "v": exp_uuid})}
                changed = {k: v for k, v in edits.items() if cur.get(k) != v}
                if not changed:
                    return redirect(_u(f"/ui/h/{slug}/versions/{expected}"))
                res = repo.apply_block_edit(conn, hid, sc_uuid, exp_uuid, changed)
            return redirect(_u(f"/ui/h/{slug}/versions/{res['version_id']}?m=edited"))
        except repo.Conflict:
            return redirect(_u(f"/ui/h/{slug}/versions/{expected}?m=conflict"))

    @app.post("/ui/h/<slug>/versions/<version_id>/approve")
    def ui_approve(slug, version_id):
        if not session.get("user_id"): abort(401)
        try:
            with tenant(slug) as (conn, hid, mid):
                repo.approve_version(conn, hid, uuid.UUID(version_id), "policy-1")
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m=approved"))
        except Exception as e:
            code = _sqlstate(e)
            m = {"42501": "e403", "23514": "e422"}.get(code)
            if not m: raise
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m={m}"))

    return app
