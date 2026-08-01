"""Render 등 관리형 PostgreSQL(비-superuser owner) 1회 배포 부트스트랩.

순서가 핵심:
  1) 스키마 create_all + scripts.current_version 순환 FK
  2) 시드(병원/승인자/스크립트 v1) — **RLS 켜기 전**, owner 평문 삽입
     (tenant 정책은 `TO app_rw`라, FORCE RLS 후엔 non-super owner가 tenant 테이블에 못 씀.
      또 불변 트리거 적용 후엔 버전/블록 INSERT가 막힘. 그래서 시드를 apply 이전에.)
  3) rls_sql.apply — 역할(app_owner/app_rw/app_auth/platform_admin)·정책·FORCE·grant·함수·lockdown
  4) app_rw 비밀번호 설정(관리형 PG는 trust 아님 → 실제 비번 필요)

사용:
  OWNER_URL="postgresql://<owner>:<pw>@<host>/<db>"  # Render External URL(그대로 OK)
  APP_RW_PASSWORD="<앱이 쓸 강한 비번>"
  python -m store.deploy_bootstrap [--reset] [--no-seed]

성공 시, 앱(Render 웹서비스)이 쓸 DATABASE_URL 힌트를 출력한다(Internal 호스트 + app_rw).
"""
import os, sys, uuid
from sqlalchemy import text
from werkzeug.security import generate_password_hash
import store.schema as S
import store.rls_sql as R
from store.db import make_engine

RESET = "--reset" in sys.argv
NO_SEED = "--no-seed" in sys.argv

OWNER_URL = os.environ.get("OWNER_URL")
APP_RW_PASSWORD = os.environ.get("APP_RW_PASSWORD")
if not OWNER_URL or not APP_RW_PASSWORD:
    sys.exit("환경변수 OWNER_URL, APP_RW_PASSWORD 필요")

eng = make_engine(OWNER_URL)  # 스킴 정규화 + (원격이면) SSL

# ── 1) (옵션) 리셋: public 스키마 초기화 ──────────────────
if RESET:
    with eng.begin() as cn:
        cn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        cn.execute(text("CREATE SCHEMA public"))
    print("[reset] public 스키마 재생성")

# ── 스키마 + 순환 FK ──────────────────────────────────────
S.metadata.create_all(eng)
with eng.begin() as cn:
    exists = cn.execute(text(
        "select 1 from pg_constraint where conname='fk_scripts_current_version'")).scalar()
    if not exists:
        cn.execute(text("ALTER TABLE scripts ADD CONSTRAINT fk_scripts_current_version "
                        "FOREIGN KEY (hospital_id, id, current_version_id) "
                        "REFERENCES script_versions (hospital_id, script_id, id)"))
print("[schema] 테이블 + current_version FK 준비")

# ── 2) 시드(RLS 이전, owner 평문) ─────────────────────────
seeded = None
if not NO_SEED:
    already = None
    with eng.connect() as cn:
        already = cn.execute(text("select id from hospitals where slug='boncure'")).scalar()
    if already:
        print(f"[seed] 이미 존재(slug=boncure, id={already}) — 건너뜀")
    else:
        # 블록 구성: SEED_PACKAGE(out/<topic>_package.json) 지정 시 실제 대본, 아니면 2블록 플레이스홀더
        import io as _io, json as _json
        from store.migrate import block_type_of
        pkg_path = os.environ.get("SEED_PACKAGE")
        if pkg_path:
            sc_list = _json.load(_io.open(pkg_path, encoding="utf-8")).get("script") or []
            blocks = [(f"blk_{i+1}", i, block_type_of(b.get("block") or ""),
                       (b.get("scene") or "")[:2000], b.get("say") or "",
                       _json.dumps({"block_label": b.get("block") or "", "tags": b.get("tags") or []}, ensure_ascii=False))
                      for i, b in enumerate(sc_list) if (b.get("say") or "").strip()]
        else:
            blocks = [("blk_1", 0, "explanation", None, "안녕하세요, 한의사 송정현입니다.", "{}"),
                      ("blk_2", 1, "explanation", None, "오늘 영상도 끝까지 함께해 주세요. 구독과 좋아요 부탁드립니다.", "{}")]
        h, u, m, sc, v = (uuid.uuid4() for _ in range(5))
        with eng.begin() as cn:
            cn.execute(text("insert into hospitals(id,slug,name) values(:h,'boncure','본큐어한의원')"), {"h": h})
            cn.execute(text("insert into users(id,email,name,pw_hash) values(:u,'demo@boncure.kr','데모원장',:p)"),
                       {"u": u, "p": generate_password_hash("demo1234")})
            cn.execute(text("insert into hospital_memberships(id,hospital_id,user_id) values(:m,:h,:u)"),
                       {"m": m, "h": h, "u": u})
            cn.execute(text("insert into membership_roles(id,hospital_id,membership_id,role) values(:i,:h,:m,'approver')"),
                       {"i": uuid.uuid4(), "h": h, "m": m})
            cn.execute(text("insert into scripts(id,hospital_id,topic) values(:s,:h,'이명')"), {"s": sc, "h": h})
            cn.execute(text("insert into script_versions(id,hospital_id,script_id,version_no,source) "
                            "values(:v,:h,:s,1,'migration')"), {"v": v, "h": h, "s": sc})
            cn.execute(text("insert into version_approval_states(id,hospital_id,version_id,status) "
                            "values(:i,:h,:v,'none')"), {"i": uuid.uuid4(), "h": h, "v": v})
            for key, oi, bt, scn, tx, md in blocks:
                cn.execute(text("insert into script_blocks(id,hospital_id,version_id,stable_block_key,order_index,"
                                "block_type,scene,text,metadata) values(:b,:h,:v,:k,:o,:bt,:scn,:tx,cast(:md as jsonb))"),
                           {"b": uuid.uuid4(), "h": h, "v": v, "k": key, "o": oi, "bt": bt, "scn": scn, "tx": tx, "md": md})
            cn.execute(text("update scripts set current_version_id=:v where id=:s"), {"v": v, "s": sc})
        seeded = {"hospital": h, "version": v, "script": sc}
        print(f"[seed] 병원 boncure / 로그인 demo@boncure.kr·demo1234(승인자) / 스크립트 v1({len(blocks)}블록)")

# ── 3) RLS·역할·정책·함수·grant·lockdown ─────────────────
R.apply(eng)
print("[rls] 역할·정책·FORCE RLS·함수·lockdown 적용")

# ── 4) app_rw 실제 비밀번호 설정 ──────────────────────────
_pw_lit = APP_RW_PASSWORD.replace("'", "''")   # ALTER ROLE는 유틸리티문(파라미터 바인딩 불가) → 안전 이스케이프 후 인라인
with eng.begin() as cn:
    cn.execute(text(f"ALTER ROLE app_rw WITH LOGIN PASSWORD '{_pw_lit}'"))
print("[role] app_rw 비밀번호 설정 완료")

# ── 앱용 DATABASE_URL 힌트(Internal 호스트 + app_rw) ──────
# OWNER_URL에서 host/db만 추출해 사용자(user:pass)만 app_rw로 치환한 형태를 안내.
print("\n=== 완료 ===")
print("Render 웹서비스 환경변수 DATABASE_URL 에 넣을 값(내부 호스트 사용):")
print("  postgresql+pg8000://app_rw:<APP_RW_PASSWORD>@<INTERNAL_HOST>/<DB>")
print("  · <INTERNAL_HOST> = Render Internal URL의 호스트(예: dpg-xxxxx-a)")
print("  · <DB> = 데이터베이스명")
if seeded:
    print(f"\n데모 버전 페이지: /ui/h/boncure/versions/{seeded['version']}")
