"""P0 3단계 앱층 — 편집·승인·diff·버전조회 HTTP API (store/ 기반, 실 PostgreSQL).

SDR 준수:
 - app.hospital_id/app.membership_id는 '서버가 인증 세션에서 결정'해서만 설정(요청 body의 membership 신뢰 금지).
 - request_id를 매 요청 생성해 승인 audit에 배선.
 - 승인은 repositories.approve_version 경로로만(advisory lock 하 hash).
CAS 충돌→409, 역할/권한(42501)→403, 미검증 claim(23514)→422, 없음(P0002)→404.
"""
import os, uuid, secrets, hmac, time
from contextlib import contextmanager
from services.context import ActorContext
from services import scripts as scripts_service
from services import evidence as evidence_service
from services import approvals as approvals_service
from services import exports as exports_service
from services import images as images_service
from services import workspace as workspace_service
from services.exceptions import ServiceError
from flask import Flask, request, jsonify, session, g, abort, redirect, Response
from werkzeug.security import check_password_hash
from sqlalchemy import text
from store.db import make_engine
from store import repositories as repo
try:
    from web.branding import LOGO_URI, ICON_URI
except Exception:
    LOGO_URI = ICON_URI = ""

# 렌더링(CSS·페이지 셸·근거/이미지 패널·버전페이지)은 공유 presentation 계층으로 이관(Step 7A).
# web/api.py는 Flask 값(script_root·session csrf)만 주입하는 얇은 브리지·route adapter로 남는다.
from presentation import render as _render
from presentation.urls import StudioUrls


def _page(title, body):
    return _render.page(title, body)


def _u(path):
    """마운트 프리픽스(script_root) 인식 절대경로. 단독 실행 시 ''→경로 그대로,
    DispatcherMiddleware로 /studio 등에 마운트되면 프리픽스를 자동 부착(하드코딩 링크가 프리픽스를 우회하지 않도록)."""
    return (request.script_root or "") + path


def _studio_urls(slug):
    """presentation에 넘길 URL 어댑터 — Flask script_root 인식 _u를 주입."""
    return StudioUrls(_u, slug)


# 근거/이미지 패널·버전페이지 렌더는 presentation.render로 이관(권한 게이트는 services/permissions).


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
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=bool(os.environ.get("SECRET_KEY")))  # 대시보드와 동일(세션 공유 안전)
    app.config["ENGINE"] = engine or make_engine()  # app_rw 엔진(운영은 DATABASE_URL)

    @app.before_request
    def _rid():
        g.request_id = uuid.uuid4().hex
        g._t0 = time.perf_counter()        # latency 측정용
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_urlsafe(24)   # 대시보드와 공유 세션이면 이미 있음
        # 상태변경 CSRF 검증: /ui/ 폼만(브라우저). /api/(JSON)·로그인 POST는 면제(API는 추후 토큰인증).
        if request.method == "POST" and not request.path.startswith("/api/") and request.path != "/login":
            sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
            good = session.get("_csrf") or ""
            if not (sent and good and hmac.compare_digest(str(sent), str(good))):
                abort(400)

    @app.after_request
    def _obs(resp):
        # /studio endpoint별 요청 수·상태·redirect·latency 로깅 — 물리 통합/redirect 후 legacy 호출 추적.
        # GPT: method=redirect 가능여부, 404=누락 deep link, 401/403=세션전환, 5xx=새 route 회귀 신호.
        try:
            from services.observability import emit, mask_ids
            st = resp.status_code
            lat = None
            if getattr(g, "_t0", None) is not None:
                lat = round((time.perf_counter() - g._t0) * 1000, 1)
            loc = resp.headers.get("Location") if 300 <= st < 400 else None
            # surface=studio_legacy(제거 예정 계층), compat=True(전환기 동안 전부 호환 계층).
            # 대시보드 canonical(dashboard_canonical, compat=False)과 로그에서 바로 대비(GPT).
            emit("http", app="studio", surface="studio_legacy", compat=True, method=request.method,
                 rule=(request.url_rule.rule if request.url_rule else mask_ids(request.path)),
                 endpoint=request.endpoint, status=st,
                 redirect=(300 <= st < 400) or None,
                 redirect_target=(mask_ids(loc) if loc else None),   # UUID/식별자 마스킹(개인정보 미노출)
                 request_id=getattr(g, "request_id", None), latency_ms=lat)
        except Exception:
            pass
        return resp

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
        return {"42501": 403, "23514": 422, "P0002": 404, "P2013": 409, "P2014": 409}.get(code, 400), code

    @app.errorhandler(403)
    def _stale_session(e):
        # UI 화면에서의 403은 대개 재시드로 세션 user_id가 무효가 된 경우 → 세션 비우고 로그인으로
        if request.method == "GET" and ("/ui/" in request.path or request.path == "/"):
            session.clear()
            return redirect(_u("/login"))
        return e

    # ── 편집 → 새 버전 ──
    @app.post("/api/h/<slug>/scripts/<script_id>/edit")
    def edit(slug, script_id):
        body = request.get_json(force=True) or {}
        expected = body.get("expected_current_version")
        edits = body.get("edits") or {}
        if not expected or not edits:
            return jsonify(error="expected_current_version과 edits 필요"), 400
        try:      # 업무 규칙은 공통 scripts service (라우트는 파싱+매핑만)
            ctx = ActorContext.resolve(app.config["ENGINE"], session.get("user_id"), slug, g.request_id)
            res = scripts_service.edit_blocks(app.config["ENGINE"], ctx, script_id, expected, edits)
            return jsonify(res), (200 if res.get("no_change") else 201)
        except ServiceError as e:
            return jsonify(error=e.code, detail=str(e)), e.http_status

    # ── 승인(공통 approval service) ──
    @app.post("/api/h/<slug>/versions/<version_id>/approve")
    def approve(slug, version_id):
        policy = (request.get_json(force=True) or {}).get("policy", "policy-1")
        try:      # 작성자≠승인자·current·evidence gate는 service/DB가 강제
            ctx = ActorContext.resolve(app.config["ENGINE"], session.get("user_id"), slug, g.request_id)
            out = approvals_service.approve(app.config["ENGINE"], ctx, version_id, policy=policy)
            return jsonify(ok=True, **out), 200
        except ServiceError as e:
            return jsonify(error=e.code, detail=str(e)), e.http_status

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
                session.clear()   # 세션 고정 방지
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
        try:      # 읽기 데이터는 공통 query service(get_version_workspace) 한 번으로
            _ctx = ActorContext.resolve(app.config["ENGINE"], session.get("user_id"), slug, g.request_id)
            ws = workspace_service.get_version_workspace(app.config["ENGINE"], _ctx, version_id)
        except ServiceError as e:
            if e.http_status == 401:
                return redirect(_u("/login") + "?next=" + _u(f"/ui/h/{slug}/versions/{version_id}"))
            abort(e.http_status)
        # 렌더는 공유 presentation(버전페이지). 라우트는 인증·데이터·URL 어댑터·csrf만 주입.
        return _render.version_page(ws, _studio_urls(slug), session.get("_csrf", ""),
                                    msg_code=request.args.get("m"))

    @app.post("/ui/h/<slug>/scripts/<script_id>/edit")
    def ui_edit(slug, script_id):
        expected = request.form.get("expected")
        try:
            uuid.UUID(expected); uuid.UUID(script_id)      # 폼 누락/오형식 → 400(500 방지)
        except (TypeError, ValueError):
            abort(400)
        edits = {k[6:]: v for k, v in request.form.items() if k.startswith("edit__")}
        try:      # 편집 규칙(권한·변경필터·새버전)은 공통 scripts service
            ctx = ActorContext.resolve(app.config["ENGINE"], session.get("user_id"), slug, g.request_id)
            res = scripts_service.edit_blocks(app.config["ENGINE"], ctx, script_id, expected, edits)
            if res.get("no_change"):
                return redirect(_u(f"/ui/h/{slug}/versions/{expected}"))
            return redirect(_u(f"/ui/h/{slug}/versions/{res['version_id']}?m=edited"))
        except ServiceError as e:
            if e.http_status == 409:
                return redirect(_u(f"/ui/h/{slug}/versions/{expected}?m=conflict"))
            abort(e.http_status)

    def _approval_action(slug, version_id, action, ok_msg):
        """승인/반려/철회 공통 — ctx resolve + service 호출 + 결과 메시지 리다이렉트(라우트=파싱+매핑)."""
        try:
            uuid.UUID(version_id)
        except (TypeError, ValueError):
            abort(400)
        try:
            ctx = ActorContext.resolve(app.config["ENGINE"], session.get("user_id"), slug, g.request_id)
            action(ctx)
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m={ok_msg}"))
        except ServiceError as e:
            m = {401: None, 403: "e403", 422: "e422", 409: "conflict"}.get(e.http_status, None)
            if m is None:
                if e.http_status == 401:
                    return redirect(_u("/login"))
                abort(e.http_status)
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m={m}"))

    @app.post("/ui/h/<slug>/versions/<version_id>/approve")
    def ui_approve(slug, version_id):
        return _approval_action(slug, version_id,
                                lambda ctx: approvals_service.approve(app.config["ENGINE"], ctx, version_id), "approved")

    @app.post("/ui/h/<slug>/versions/<version_id>/reject")
    def ui_reject(slug, version_id):
        reason = (request.form.get("reason") or "").strip()
        return _approval_action(slug, version_id,
                                lambda ctx: approvals_service.reject(app.config["ENGINE"], ctx, version_id, reason), "rejected")

    @app.post("/ui/h/<slug>/versions/<version_id>/revoke")
    def ui_revoke(slug, version_id):
        reason = (request.form.get("reason") or "").strip()
        return _approval_action(slug, version_id,
                                lambda ctx: approvals_service.revoke(app.config["ENGINE"], ctx, version_id, reason), "revoked")

    @app.post("/ui/h/<slug>/versions/<version_id>/self-approve")
    def ui_self_approve(slug, version_id):
        reason = (request.form.get("reason") or "").strip()
        return _approval_action(slug, version_id,
                                lambda ctx: approvals_service.self_approve(app.config["ENGINE"], ctx, version_id, reason), "approved")

    # ── export gate: current이며 approved인 version만 산출물 반환(inv14) ──
    @app.get("/api/h/<slug>/scripts/<script_id>/versions/<version_id>/export")
    def export_version(slug, script_id, version_id):
        try:
            ctx = ActorContext.resolve(app.config["ENGINE"], session.get("user_id"), slug, g.request_id)
            payload = exports_service.prepare_export(app.config["ENGINE"], ctx, script_id, version_id)
            return jsonify(payload), 200
        except ServiceError as e:
            return jsonify(error=e.code, detail=str(e)), e.http_status

    # ── 원장 검수/반려: 주장별 사람 판정(human_review) — 자동판정보다 우선 ──
    @app.post("/ui/h/<slug>/claims/<claim_id>/review")
    def ui_review(slug, claim_id):
        version_id = request.form.get("version_id"); script_id = request.form.get("script_id")
        decision = request.form.get("decision")
        try:
            uuid.UUID(claim_id); uuid.UUID(version_id); uuid.UUID(script_id)   # 폼 누락/오형식 → 400
        except (TypeError, ValueError):
            abort(400)
        try:      # 검수 규칙(권한·current 재검사·approved 동결·소속검증)은 공통 evidence service
            ctx = ActorContext.resolve(app.config["ENGINE"], session.get("user_id"), slug, g.request_id)
            evidence_service.assess_claim(app.config["ENGINE"], ctx, script_id, version_id, claim_id, decision)
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m=reviewed"))
        except ServiceError as e:
            if e.http_status == 409:   # current 변경/승인 동결 → 최신 버전으로
                return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m=conflict"))
            abort(e.http_status)

    # ── 장면 이미지 서빙(DB bytea) ──
    @app.get("/img/h/<slug>/<block_key>")
    def scene_img(slug, block_key):
        with tenant(slug) as (conn, hid, mid):
            row = conn.execute(text("select mime, data from scene_images "
                                    "where hospital_id=:h and block_key=:k limit 1"),
                               {"h": hid, "k": block_key}).first()
        if not row:
            abort(404)
        return Response(bytes(row.data), mimetype=row.mime or "image/jpeg",
                        headers={"Cache-Control": "no-store"})

    # ── 피드백 반영 이미지 재생성(사람 피드백 → 프롬프트 조정 → gpt-image-1) ──
    @app.post("/ui/h/<slug>/versions/<version_id>/blocks/<block_key>/regen-image")
    def regen_image(slug, version_id, block_key):
        feedback = (request.form.get("feedback") or "").strip()
        try:      # 이미지 재생성 규칙(권한·프롬프트·영속)은 공통 image service. OpenAI는 TX 밖.
            ctx = ActorContext.resolve(app.config["ENGINE"], session.get("user_id"), slug, g.request_id)
            images_service.regenerate_scene(app.config["ENGINE"], ctx, block_key, feedback, version_id=version_id)
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m=regen#{block_key}"))
        except ServiceError as e:
            if e.http_status == 401:
                return redirect(_u("/login"))
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m=regenfail"))
        except Exception:
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m=regenfail"))

    @app.post("/ui/h/<slug>/versions/<version_id>/blocks/<block_key>/revert-image")
    def revert_image(slug, version_id, block_key):
        try:      # 이전 이미지로 되돌리기(비파괴, 둘 다 보존)
            ctx = ActorContext.resolve(app.config["ENGINE"], session.get("user_id"), slug, g.request_id)
            images_service.revert_scene(app.config["ENGINE"], ctx, block_key)
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m=reverted#{block_key}"))
        except ServiceError as e:
            if e.http_status == 401:
                return redirect(_u("/login"))
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m=regenfail"))
        except Exception:
            return redirect(_u(f"/ui/h/{slug}/versions/{version_id}?m=regenfail"))

    return app
