"""3단계 앱층 — 편집·승인·diff·조회 endpoint (Flask 테스트클라이언트 + 실 PG)."""
import uuid, pytest
from sqlalchemy import text
from store.testkit import new_version
from web.api import create_app

@pytest.fixture
def client(rw):
    app = create_app(engine=rw)
    app.config["TESTING"] = True
    return app.test_client()

def _login(client, user_id):
    with client.session_transaction() as s:
        s["user_id"] = str(user_id)

def _setup(owner, role=None, verified_claim=False):
    """병원+유저+멤버십(+역할)+스크립트+버전(블록2개, 선택 claim). slug/user/script/version 반환."""
    h, u, m, sc, v = (uuid.uuid4() for _ in range(5))
    slug = "h" + h.hex[:10]
    with owner.begin() as cn:
        cn.execute(text("insert into hospitals(id,slug,name) values(:h,:s,'T')"), {"h": h, "s": slug})
        cn.execute(text("insert into users(id,email) values(:u,:e)"), {"u": u, "e": u.hex + "@t.c"})
        cn.execute(text("insert into hospital_memberships(id,hospital_id,user_id) values(:m,:h,:u)"), {"m": m, "h": h, "u": u})
        if role:
            cn.execute(text("insert into membership_roles(id,hospital_id,membership_id,role) values(:i,:h,:m,:r)"),
                       {"i": uuid.uuid4(), "h": h, "m": m, "r": role})
        cn.execute(text("insert into scripts(id,hospital_id,topic) values(:s,:h,'t')"), {"s": sc, "h": h})
        cn.execute(text("insert into script_versions(id,hospital_id,script_id,version_no,source) values(:v,:h,:s,1,'migration')"),
                   {"v": v, "h": h, "s": sc})
        cn.execute(text("insert into version_approval_states(id,hospital_id,version_id,status) values(:i,:h,:v,'none')"),
                   {"i": uuid.uuid4(), "h": h, "v": v})
        for key, oi in [("blk_1", 0), ("blk_2", 1)]:
            b = uuid.uuid4()
            cn.execute(text("insert into script_blocks(id,hospital_id,version_id,stable_block_key,order_index,block_type,text) "
                            "values(:b,:h,:v,:k,:o,'explanation','원문')"), {"b": b, "h": h, "v": v, "k": key, "o": oi})
            if verified_claim and key == "blk_1":
                s_id, c_id = uuid.uuid4(), uuid.uuid4()
                cn.execute(text("insert into script_sentences(id,hospital_id,version_id,block_id,sentence_index,text,start_offset,end_offset,offset_unit,segmenter_version) "
                                "values(:s,:h,:v,:b,0,'원문',0,2,'codepoint','v1')"), {"s": s_id, "h": h, "v": v, "b": b})
                cn.execute(text("insert into claims(id,hospital_id,version_id,sentence_id,claim_index,claim_text,claim_type,detection_method) "
                                "values(:c,:h,:v,:s,0,'원문','statistic','migration')"), {"c": c_id, "h": h, "v": v, "s": s_id})
                cn.execute(text("insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,support_level,verification_status,medical_risk) "
                                "values(:i,:h,:c,'automated','a1','direct','verified','low')"), {"i": uuid.uuid4(), "h": h, "c": c_id})
        cn.execute(text("update scripts set current_version_id=:v where id=:s"), {"v": v, "s": sc})
    return {"slug": slug, "user_id": u, "script_id": sc, "version_id": v}

def test_unauthenticated_401(client, owner):
    d = _setup(owner)
    r = client.post(f"/api/h/{d['slug']}/scripts/{d['script_id']}/edit", json={"expected_current_version": str(d["version_id"]), "edits": {"blk_2": "x"}})
    assert r.status_code == 401

def test_edit_then_conflict(client, owner):
    d = _setup(owner, role="editor"); _login(client, d["user_id"])
    r = client.post(f"/api/h/{d['slug']}/scripts/{d['script_id']}/edit",
                    json={"expected_current_version": str(d["version_id"]), "edits": {"blk_2": "이명은 목과 관련될 수 있습니다."}})
    assert r.status_code == 201 and r.get_json()["version_id"]
    # 같은 구버전 기대 → 409(다른 편집 선반영)
    r2 = client.post(f"/api/h/{d['slug']}/scripts/{d['script_id']}/edit",
                     json={"expected_current_version": str(d["version_id"]), "edits": {"blk_2": "다시"}})
    assert r2.status_code == 409

def test_approve_requires_role_403(client, owner):
    d = _setup(owner, verified_claim=True); _login(client, d["user_id"])   # 역할 없음
    r = client.post(f"/api/h/{d['slug']}/versions/{d['version_id']}/approve", json={"policy": "p1"})
    assert r.status_code == 403

def test_approve_blocks_unverified_422(client, owner):
    # 미검증 claim이 있는 current version(작성자 NULL=migration) → 승인 422(자기승인 아님)
    d = _setup(owner, role="approver"); _login(client, d["user_id"])
    with owner.begin() as cn:
        hid = cn.execute(text("select hospital_id from script_versions where id=:v"), {"v": d["version_id"]}).scalar()
        b = cn.execute(text("select id from script_blocks where version_id=:v limit 1"), {"v": d["version_id"]}).scalar()
        s_id, c_id = uuid.uuid4(), uuid.uuid4()
        cn.execute(text("insert into script_sentences(id,hospital_id,version_id,block_id,sentence_index,text,start_offset,end_offset,offset_unit,segmenter_version) "
                        "values(:s,:h,:v,:b,0,'원문',0,2,'codepoint','v1')"), {"s": s_id, "h": hid, "v": d["version_id"], "b": b})
        cn.execute(text("insert into claims(id,hospital_id,version_id,sentence_id,claim_index,claim_text,claim_type,detection_method) "
                        "values(:c,:h,:v,:s,0,'원문','statistic','migration')"), {"c": c_id, "h": hid, "v": d["version_id"], "s": s_id})
        cn.execute(text("insert into claim_assessments(id,hospital_id,claim_id,assessment_kind,idempotency_key,support_level,verification_status,medical_risk) "
                        "values(:i,:h,:c,'automated','a1','unverified','pending','low')"), {"i": uuid.uuid4(), "h": hid, "c": c_id})
    ra = client.post(f"/api/h/{d['slug']}/versions/{d['version_id']}/approve", json={"policy": "p1"})
    assert ra.status_code == 422

def test_approve_success_and_audit(client, owner):
    d = _setup(owner, role="approver", verified_claim=True); _login(client, d["user_id"])
    r = client.post(f"/api/h/{d['slug']}/versions/{d['version_id']}/approve", json={"policy": "p1"})
    assert r.status_code == 200 and r.get_json()["ok"]
    # audit에 request_id 배선 확인 — audit_events는 app_rw 직접접근 불가라 owner로 확인
    with owner.connect() as cn:
        rid = cn.execute(text("select request_id from audit_events where entity_id=:v and action='approval.approve'"),
                         {"v": d["version_id"]}).scalar()
    assert rid and len(rid) == 32

def test_ui_version_page_renders_via_presentation(client, owner):
    # Step 7A: /studio 버전페이지가 공유 presentation으로 렌더되는 end-to-end 확인.
    d = _setup(owner, role="editor", verified_claim=True); _login(client, d["user_id"])
    r = client.get(f"/ui/h/{d['slug']}/versions/{d['version_id']}")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "<title>버전 1" in html                                    # 셸 렌더
    assert 'name="edit__blk_1"' in html and 'name="edit__blk_2"' in html  # 블록 편집 textarea
    assert f"/ui/h/{d['slug']}/scripts/{d['script_id']}/edit" in html     # 편집 액션 URL
    assert "근거 검증" in html                                        # 근거 패널
    assert 'name=_csrf' in html                                       # csrf 필드 주입


def test_diff_and_get_version(client, owner):
    d = _setup(owner, role="editor"); _login(client, d["user_id"])
    r = client.post(f"/api/h/{d['slug']}/scripts/{d['script_id']}/edit",
                    json={"expected_current_version": str(d["version_id"]), "edits": {"blk_2": "바뀐 문장입니다."}})
    v2 = r.get_json()["version_id"]
    rd = client.get(f"/api/h/{d['slug']}/versions/{v2}/diff?from={d['version_id']}")
    body = rd.get_json()
    assert rd.status_code == 200 and any(c["key"] == "blk_2" for c in body["changed"])
    rg = client.get(f"/api/h/{d['slug']}/versions/{v2}")
    assert rg.status_code == 200 and rg.get_json()["stale"] is True     # 미승인 → stale
