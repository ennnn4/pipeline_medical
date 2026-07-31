"""P0 3단계 앱층 — 편집·승인·diff·버전조회 HTTP API (store/ 기반, 실 PostgreSQL).

SDR 준수:
 - app.hospital_id/app.membership_id는 '서버가 인증 세션에서 결정'해서만 설정(요청 body의 membership 신뢰 금지).
 - request_id를 매 요청 생성해 승인 audit에 배선.
 - 승인은 repositories.approve_version 경로로만(advisory lock 하 hash).
CAS 충돌→409, 역할/권한(42501)→403, 미검증 claim(23514)→422, 없음(P0002)→404.
"""
import os, uuid
from contextlib import contextmanager
from flask import Flask, request, jsonify, session, g, abort
from sqlalchemy import text
from store.db import make_engine
from store import repositories as repo


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

    return app
