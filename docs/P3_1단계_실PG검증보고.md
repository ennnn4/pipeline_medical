# P0 1단계 — 실제 PostgreSQL 검증 보고 (10항목)

**테스트 환경**: PostgreSQL **18.4**(로컬, `postgresql-binaries` 휠로 관리자·Docker 없이 기동) + `pg8000` 순수드라이버 + SQLAlchemy 2.0.51 / Alembic 1.18.5. Python 3.14.
전부 **실 PG 실측**(SQLite 아님). 커밋: schema.py·db.py·rls_sql.py·migrate.py·nlp/segment.py·alembic/0001.

---

## 1. 생성되는 테이블·view 목록

**테이블 25**: hospitals, users, hospital_memberships, membership_roles, scripts, script_versions, script_blocks, script_sentences, claims, claim_assessments, sources, source_versions, claim_sources, version_approval_states, review_links, review_sessions, review_comments, edits, style_rules, style_rule_sources, audit_events, notification_outbox, rate_limit_buckets, migration_imports, jobs.
**view 2**: claim_latest_assessment(정보용·recency), claim_effective_assessment(승인용·사람판정 우선). 둘 다 `security_invoker=true`.
**함수 3**: exchange_review_token(bytea), lookup_user_for_login(text), get_current_user(uuid) — 전부 SECURITY DEFINER·고정 search_path.

## 2. CHECK / UNIQUE / FK 목록 (실측 카운트)

- **FOREIGN KEY 38** — 3열 복합 FK로 계보 고정(예: `claims(hospital_id,version_id,sentence_id)→script_sentences(hospital_id,version_id,id)`), current_version은 `scripts(hospital_id,id,current_version_id)→script_versions(hospital_id,script_id,id)`.
- **UNIQUE 35** — `(hospital_id,id)`(복합 FK 참조), `(hospital_id,version_id,id)`, `(hospital_id,script_id,version_no)`, review_links.token_hash 등. approvals 활성 유니크는 폐기(§version_approval_states로 대체).
- **CHECK 195**(NOT NULL 포함) — enum CHECK(status/role/support_level/…), version_approval_states 상태별 필드조합, claim_assessments `human_actor_required`, offset/tc/confidence 범위, comment anchor, style_rules scope 조합 등.

## 3. RLS policy · role 권한표

| role | 용도 | 권한 |
|---|---|---|
| **app_owner** | DDL·마이그레이션 | 소유자, BYPASSRLS(웹요청 금지) |
| **app_rw** | 런타임 웹 | 테넌트 테이블 CRUD(RLS 스코프). **users·review_links 직접 SELECT 없음**. exchange_review_token EXECUTE |
| **app_auth** | 인증 | lookup_user_for_login·get_current_user EXECUTE만 |
| **platform_admin** | global rule | style_rules global 승격 |

- 테넌트 정책: `USING/WITH CHECK (hospital_id = NULLIF(current_setting('app.hospital_id',true),'')::uuid)`. ENABLE+FORCE RLS.
- style_rules 작업별 정책(SELECT=global+현재병원 / INSERT·UPDATE·DELETE=현재병원, global 금지).
- **실측**: app_rw로 H1 컨텍스트 → H1행만, H2 컨텍스트 → H2행만, 무컨텍스트 → 0행. 교차병원 INSERT는 WITH CHECK로 거부.

## 4. approval stale 판정 방식

- `version_approval_states`에 승인 시점의 `version_content_hash`·`assessment_set_hash`·`compliance_policy_version` 저장.
- **최종 출력·내보내기 시**: 현재 계산한 세 해시가 저장값과 **모두 일치할 때만 "유효 승인"**. 하나라도 다르면(=대본 수정 또는 claim 재평가 또는 정책 변경) 상태행을 즉시 바꾸지 않아도 **출력 차단 + `approval_stale` 표시**.
- 실측: 상태 CHECK 검증됨(`approved`인데 필드 누락 → SQLSTATE 23514 거부). 해시 비교 자체는 앱 계층(3·4단계에서 배선).

## 5. review_sessions 구조

`review_sessions(id, hospital_id, review_link_id[3열FK], session_token_hash[uq], expires_at, revoked_at, last_seen_at, created_at)`. 토큰 교환 후 원본 아닌 session token을 HttpOnly 쿠키로. **review_link 폐기 시 관련 review_sessions도 폐기**(매 요청 링크+세션 만료·폐기 확인). 구조 생성 완료, 런타임 흐름은 5단계.

## 6. effective claim assessment 선택 규칙 (실측 통과)

```sql
claim_effective_assessment: DISTINCT ON (hospital_id, claim_id)
 WHERE assessment_kind IN ('override','human_review','automated')   -- migration 제외
 ORDER BY ... CASE kind override=3, human_review=2, automated=1 DESC, created_at DESC, id DESC
```
- **실측**: 사람 판정(human_review, older) 뒤에 더 최신 automated(unsupported)를 넣어도 **effective = human_review(direct)** — 자동이 사람판정 못 덮음. migration만 있는 claim 33/34는 effective에서 제외(승인근거 아님).
- 승인 게이트: effective가 `verified AND support_level ∉ (unverified,unsupported)`. high-risk의 partial/inferred는 override 필요.

## 7. migration lease SQL

```sql
-- 획득(경쟁 방지): 만료 pending을 원자적으로 인계
UPDATE migration_imports SET worker_id=:w, lease_token=gen_random_uuid(),
  lease_expires_at=now()+interval '5 min', last_heartbeat_at=now()
WHERE id = (SELECT id FROM migration_imports
            WHERE status='pending' AND (lease_expires_at IS NULL OR lease_expires_at<now())
            ORDER BY started_at FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING *;
-- heartbeat/갱신: lease_token 일치 워커만
UPDATE migration_imports SET last_heartbeat_at=now(), lease_expires_at=now()+interval '5 min'
WHERE id=:id AND lease_token=:tok;
```
- `migration_imports`에 worker_id·lease_token·attempt_count·lease_expires_at·last_heartbeat_at·last_error_at 필드 생성 완료. 구조 import + status='imported' + script_id/version_id는 **script/version 삽입과 동일 트랜잭션 커밋**(실측: 마이그레이션 후 imported 1행, 재실행 skip). 다중워커 경쟁 실측은 pytest에서.

## 8. Alembic upgrade / downgrade 테스트 (실측 통과)

fresh DB에서 `upgrade head`(28객체=25테이블+2뷰+alembic_version, 함수3, current_version FK 1) → `downgrade base`(alembic_version만 잔존) → `재 upgrade head`(28) **왕복 재현 성공**. 순환 FK는 테이블 생성 후 `op.execute(ALTER ... ADD CONSTRAINT)`로 추가·downgrade에서 선 삭제.

## 9. 실제 PostgreSQL 테스트 결과 (요약)

| 검증 | 결과 | 증거 |
|---|---|---|
| 스키마 생성 | ✅ | 25테이블·38FK·195CHECK·35UNIQUE |
| 3열 복합 FK 계보 고정 | ✅ | 교차버전 claim → **SQLSTATE 23503** 거부, 정상건 성공 |
| 승인 무결성 CHECK | ✅ | approved 필드누락 → **23514** 거부 |
| human_actor_required | ✅ | human_review에 actor 없음 → 23514 거부 |
| RLS 테넌트 격리 | ✅ | H1:2·H2:1·무컨텍스트:0행, 교차 INSERT WITH CHECK 거부 |
| **RLS 빈문자열 버그** | ✅ 발견·수정 | `current_setting()::uuid` 빈값 22P02 → `NULLIF(...,'')` |
| 이명 마이그레이션 | ✅ | 19블록·192문장·34claim, canonical 왕복, span위반 0, 멱등 재실행 skip |
| claim=unverified | ✅ | 34 assessment 전부 migration/unverified/pending |
| 토큰교환 부트스트랩 | ✅ | app_rw 직접 review_links 0행, exchange_review_token 우회 성공 |
| effective(사람 우선) | ✅ | 최신 automated가 human_review 못 덮음 |
| Alembic up/down/재up | ✅ | 왕복 재현 |

## 10. SQLite에서 검증하지 못한 항목 → 전부 실 PG로 검증

SQLite로는 불가한 PG 전용 기능들을 **실 PostgreSQL 18.4로 직접 검증**: RLS(ENABLE/FORCE/policy), `security_invoker` view, 부분 유니크 인덱스, **다열 복합 FK의 실제 거부(23503)**, SECURITY DEFINER 함수, `current_setting`/`set_config` 테넌트 컨텍스트, `citext`/`jsonb`/`bytea`/`numeric` 타입. → 단위 로직(문장분할·canonical)만 SQLite/무DB로도 가능, 무결성·격리·함수·뷰는 PG 필수(그래서 실 PG로 함).

---

## 남은 마무리(1단계 잔여)
- **pytest 스위트 정식화**: 위 실측들을 재현 가능한 pytest로 묶기(현재는 스크립트 실측). 다중워커 lease 경쟁·approval_stale·review_session 폐기 흐름 포함.
- **approval_stale·review_session 런타임 배선**: 3·5단계에서(앱 계층).
- **per-claim evidence 재검사**: 4단계(Source Grounding). 현재 마이그레이션은 claim=unverified까지.

토대(스키마·복합FK·RLS·마이그레이션·토큰교환·effective·Alembic)는 **실 PG로 검증 완료**. 승인 주시면 pytest 정식화 + 3단계(편집·diff·재검사)로 진행.
