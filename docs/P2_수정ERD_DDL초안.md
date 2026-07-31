# P2 — 수정 ERD · Alembic DDL 초안 (GPT 검토 반영, 구현 전)

전면 재설계 아님. 기존 ERD 기준으로 **Critical·High 우선 수정**. 실제 구현은 이 보고 승인 후.

---

## 0. 지적별 처분 요약

### Critical — 전부 **반영**
| 지적 | 처분 | 조치 |
|---|---|---|
| 복합 FK가 교차 SELECT/UPDATE 못 막음 → RLS | **반영** | PostgreSQL RLS + runtime/migrate role 분리(§4) |
| claim.version≠block.version 섞임 | **반영** | 3열 복합 FK로 계보 고정(§2) |
| current/parent/comment/edit 동일 문제 | **반영** | 전부 3열 FK(§2,§3) |
| 커밋 후 롤백 불가 | **반영** | 커밋 전 canonical round-trip 검증, evidence는 후속(§8) |
| claim 불변 vs 검증 UPDATE 충돌 | **반영** | `claim_assessments` append-only + latest view(§6) |
| 승인 게이트가 unverified/partial 통과 | **반영** | `verified AND support_level NOT IN (unverified,unsupported)`, high-risk override(§9) |
| 익명 approve 토큰 | **반영** | 익명=comment_only, 승인=로그인/OTP(§9) |
| source 경로 교체로 재현 불가 | **반영** | `source_versions`(checksum·content-addressed)(§7) |

### High — 대부분 **반영**, 3건 **일부 반영**
| 지적 | 처분 |
|---|---|
| 순환 FK use_alter | **반영**(§3) |
| approvals 부분 유니크 index 형태 | **반영**(§2) |
| 승인 status/timestamp CHECK | **반영** |
| CHECK enum NOT NULL 누락 | **반영**(전 상태컬럼 NOT NULL) |
| sentence 테이블 부재 | **반영** `script_sentences`(§5) |
| 검증 이력 부재 | **반영** `claim_assessments`(§6) |
| 승인 재현성(assessment_set_hash) | **반영** approvals에 해시 3종(§9) |
| 동시 편집 잠금 | **반영**(§10) |
| actor=membership_id | **반영**(시스템/AI actor는 nullable+source) |
| membership 다중 역할 | **반영** `hospital_memberships` + `membership_roles` 분리 |
| style_rules scope CHECK | **반영** |
| global rule 유출 | **반영**(platform admin 승격 + 비식별 example만) |
| uuid[] → 조인테이블 | **반영** `style_rule_sources` |
| file_path 휘발 | **일부 반영**: 스키마(checksum·object_key·source_versions)는 지금, **객체스토리지 전환은 배포 영구화 단계** |
| audit_events | **일부 반영**: 테이블은 지금 추가, 이벤트 기록 배선은 각 기능 단계 |
| notification_outbox | **일부 반영**: 테이블은 지금, 발송 워커는 리뷰/승인 단계 |

### Medium/Low — **반영**(스키마·제약), 일부 런타임은 해당 단계
citext extension, PG-CI 테스트, FK 인덱스, `timestamptz NOT NULL DEFAULT now()`, `tc_*_ms`, offset 단위·CHECK, comment anchor CHECK, confidence `numeric CHECK 0..1`, source locator CHECK, claim evidence 다중 span, soft-delete, `ON DELETE` 명시, comment author 서버파생, jsonb `server_default`, status 파생, last_accessed 비동기, jobs.log 구조화 → **전부 반영**. rate-limit 저장소 = **일부 반영**(PG bucket 테이블 스키마 지금, 정책 배선은 리뷰링크 단계).

### **미반영 / 보류 (사유 명시)**
| 지적 | 처분 | 사유 |
|---|---|---|
| 리뷰 접근마다 access event 별도 테이블(`review_access_events`) | **보류** | `audit_events`로 통합 기록(중복 테이블 회피). 이상탐지 세분화 필요 시 분리 |
| Redis 기반 rate limit | **미반영(현 단계)** | 배포가 단일 인스턴스+무료라 우선 **PG bucket**로. 다중 인스턴스 확장 시 Redis 전환 |
| 알림 발송 워커 구현 | **보류** | outbox 테이블만. 발송은 리뷰/승인 기능 단계 |
| RLS를 관리형 PG에서 role 분리 | **반영(설계)·운영주의** | Render/관리형 PG는 슈퍼유저 제약이 있어 `CREATE ROLE`·`CREATE EXTENSION` 권한 확인 필요 → 운영 셋업 체크리스트에 명시 |

---

## 1. 변경된 테이블·컬럼

**신규 테이블(8)**: `membership_roles`, `script_sentences`, `claim_assessments`, `source_versions`, `style_rule_sources`, `migration_imports`, `audit_events`, `notification_outbox`, `rate_limit_buckets`. (+ `claim_latest_assessment` = view)

**주요 변경**:
- `hospital_memberships`: `role` 컬럼 제거 → `membership_roles`로 분리. `UNIQUE(hospital_id,user_id)`.
- `scripts`: actor `created_by` → `created_by_membership_id`(nullable, 복합FK). `archived_at/by/reason` 추가. `status`는 파생 취급.
- `script_versions`: `created_by`→membership(nullable, AI/migration은 null + `source`). `UNIQUE(hospital_id,script_id,id)` 추가.
- `script_blocks`: `UNIQUE(hospital_id,version_id,id)` 추가. `tc_start_ms/tc_end_ms bigint` 추가(원문 text 보존). jsonb `server_default '{}'::jsonb NOT NULL`.
- `claims`: 검증 필드(`support_level/verification_status/medical_risk`) **제거** → `claim_assessments`로 이동. `sentence_id` 참조(3열FK). `claim_text/claim_type`만 불변 보유.
- `sources`: 물리 파일 컬럼 제거 → `source_versions`. 논리 문서 메타만.
- `claim_sources`: `source_id`→`source_version_id`. `page_or_location`·`span_hash` 추가. `confidence numeric(5,4) CHECK`.
- `approvals`: **append-only 결정 이벤트**로. `assessment_set_hash/version_content_hash/compliance_policy_version` 추가. status/timestamp CHECK.
- `review_links`: `token_hash bytea`(HMAC), 익명=comment_only. `permission='approve'` 익명 금지.
- `review_comments`: `reviewer_name` 요청값 신뢰 금지 → `author_membership_id`(내부) 또는 `review_link_id`(외부)에서 파생. anchor CHECK.
- `edits`: from/to version 3열FK, `created_by_membership_id`.
- `style_rules`: `source_edit_ids uuid[]` 제거 → `style_rule_sources`. scope 조합 CHECK.
- `jobs`: 이력형(id PK)+`hospital_id` FK. `log`는 크기제한/redaction, 상세는 후속 `job_events`(보류).
- 전 상태·역할 컬럼 `NOT NULL`. 전 시간 컬럼 `timestamptz NOT NULL DEFAULT now()`.

---

## 2. 추가·수정 UNIQUE 및 3열 복합 FK

**부모 UNIQUE(참조 대상)**:
```sql
ALTER TABLE script_versions ADD UNIQUE (hospital_id, script_id, id);   -- current/parent/edit 참조용
ALTER TABLE script_versions ADD UNIQUE (hospital_id, id);              -- block/claim/approval/link 참조용
ALTER TABLE script_blocks   ADD UNIQUE (hospital_id, version_id, id);  -- sentence/comment 참조용
ALTER TABLE script_sentences ADD UNIQUE (hospital_id, version_id, id); -- claim 참조용
ALTER TABLE review_links    ADD UNIQUE (hospital_id, version_id, id);  -- comment 참조용
ALTER TABLE hospital_memberships ADD UNIQUE (hospital_id, id);         -- actor 참조용
```
**3열 FK(계보 고정)**:
```sql
-- current version = 반드시 이 script의 버전
FK scripts(hospital_id, id, current_version_id)        -> script_versions(hospital_id, script_id, id)
-- parent = 같은 script
FK script_versions(hospital_id, script_id, parent_version_id) -> script_versions(hospital_id, script_id, id)
-- block -> version
FK script_blocks(hospital_id, version_id)              -> script_versions(hospital_id, id)
-- sentence -> block(같은 version)
FK script_sentences(hospital_id, version_id, block_id) -> script_blocks(hospital_id, version_id, id)
-- claim -> sentence(같은 version)  → block/version 계보 자동 고정
FK claims(hospital_id, version_id, sentence_id)        -> script_sentences(hospital_id, version_id, id)
-- comment -> block(같은 version)
FK review_comments(hospital_id, version_id, block_id)  -> script_blocks(hospital_id, version_id, id)
-- comment -> review_link(같은 version)
FK review_comments(hospital_id, version_id, review_link_id) -> review_links(hospital_id, version_id, id)
-- edit from/to = 같은 script
FK edits(hospital_id, script_id, from_version_id)      -> script_versions(hospital_id, script_id, id)
FK edits(hospital_id, script_id, to_version_id)        -> script_versions(hospital_id, script_id, id)
-- actor = 같은 병원 membership
FK scripts(hospital_id, created_by_membership_id)      -> hospital_memberships(hospital_id, id)
```
**approvals 부분 유니크**(제약 아님, 부분 인덱스):
```sql
CREATE UNIQUE INDEX uq_approvals_one_active ON approvals (hospital_id, version_id)
  WHERE status='approved' AND revoked_at IS NULL;
```
SQLAlchemy: `Index("uq_approvals_one_active", ..., unique=True, postgresql_where=and_(status=='approved', revoked_at.is_(None)))`.

**FK 인덱스 보강**(PG는 FK 자동인덱스 없음): current/parent version, source_version, review_link, from/to version 등 자식 FK 컬럼에 인덱스.

---

## 3. scripts ↔ current_version 순환 FK 처리

- `DEFERRABLE`은 **런타임 검증 시점** 옵션일 뿐 **DDL 생성 순환을 못 푼다** → GPT 지적 반영.
- 방식: `scripts`를 `current_version_id NULL`로 **FK 없이 먼저 생성** → `script_versions` 생성 → `op.create_foreign_key(..., use_alter=True)`로 3열 FK 추가. 앱 흐름은 `script insert → v1 insert → scripts.current_version_id update`(deferrable 불필요).
- SQLAlchemy 모델은 `use_alter=True, name=...`로 선언해 Alembic이 alter로 분리 생성.

---

## 4. RLS 정책 + runtime/migration role 분리

- **role 분리**: `app_owner`(테이블 소유·DDL·마이그레이션, `BYPASSRLS`) / `app_rw`(런타임, 소유자 아님, BYPASSRLS 아님).
- 모든 **테넌트 테이블**:
```sql
ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <t> FORCE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON <t>
  USING      (hospital_id = current_setting('app.hospital_id', true)::uuid)
  WITH CHECK (hospital_id = current_setting('app.hospital_id', true)::uuid);
```
- 앱은 트랜잭션 시작 시 `SET LOCAL app.hospital_id='<uuid>'`(세션·멤버십·리뷰토큰에서 **서버가 결정**). repository 베이스가 강제.
- 예외: `users`(전역, RLS 없음). `style_rules`는 특수 정책 — `USING (scope='global' OR hospital_id = current_setting('app.hospital_id')::uuid)`, 단 global INSERT/승격은 `app_owner`(platform admin)만.
- **운영 주의**: 관리형 PG(Render 등)는 `CREATE ROLE`·`CREATE EXTENSION` 권한 제약이 있을 수 있음 → 운영 셋업 체크리스트에 확인 항목 추가. RLS는 DB 레벨 최종 방어, 앱 repository의 `hospital_id` 조건은 1차 방어(이중).

---

## 5. `script_sentences` 구조

```
script_sentences
- id uuid PK
- hospital_id uuid NOT NULL
- version_id uuid NOT NULL
- block_id uuid NOT NULL
- sentence_index int NOT NULL
- text text NOT NULL
- start_offset int NOT NULL, end_offset int NOT NULL
- offset_unit text NOT NULL CHECK(offset_unit IN ('utf16','codepoint'))  -- 브라우저 anchor 정합
- segmenter_version text NOT NULL
- created_at timestamptz NOT NULL DEFAULT now()
FK (hospital_id, version_id, block_id) -> script_blocks(hospital_id, version_id, id)
UNIQUE (hospital_id, version_id, block_id, sentence_index)
UNIQUE (hospital_id, version_id, id)
CHECK (end_offset > start_offset AND start_offset >= 0)
```
- claim이 없는 일반 문장도 식별 → 댓글·diff·claim 연결의 공통 앵커. 분할기 교체 시 `segmenter_version`으로 추적, 재분할=새 버전에서 수행(기존 문장 불변).

---

## 6. `claim_assessments` append-only + 최신 조회

```
claims (불변): id, hospital_id, version_id, sentence_id, claim_index, claim_text, claim_type, detection_method, created_at
  FK (hospital_id, version_id, sentence_id) -> script_sentences(hospital_id, version_id, id)
  UNIQUE (hospital_id, sentence_id, claim_index), UNIQUE (hospital_id, id)

claim_assessments (append-only):
- id uuid PK, hospital_id, claim_id
- checker_version text NOT NULL, model text, prompt_hash text, source_set_hash text
- support_level text NOT NULL CHECK(direct|partial|inferred|unsupported|unverified)
- verification_status text NOT NULL CHECK(pending|verified|failed)
- medical_risk text NOT NULL CHECK(low|medium|high)
- rationale text, actor_membership_id uuid NULL, created_at timestamptz NOT NULL DEFAULT now()
FK (hospital_id, claim_id) -> claims(hospital_id, id)
UNIQUE (hospital_id, id)
```
- claim 본문 불변 유지, 검증은 **행 추가만**(UPDATE 없음) → 승인 당시 상태 재현 가능.
- 최신 조회 view:
```sql
CREATE VIEW claim_latest_assessment AS
SELECT DISTINCT ON (claim_id) *
FROM claim_assessments ORDER BY claim_id, created_at DESC;
```
- **승인 게이트**는 이 view 기준: `verification_status='verified' AND support_level NOT IN ('unverified','unsupported')`. high-risk의 partial/inferred는 명시적 override(사유·approver 필요).

---

## 7. `source_versions` 구조 (근거 재현성)

```
sources (논리 문서): id, hospital_id, title, source_type, citation_metadata jsonb, created_at, archived_at
source_versions (불변 콘텐츠):
- id uuid PK, hospital_id, source_id
- checksum text NOT NULL              -- sha256
- content_addressed_key text NOT NULL -- 저장소 키(로컬은 경로, 운영은 object key)
- extractor_version text, mime text, size_bytes bigint, page_count int, created_at
FK (hospital_id, source_id) -> sources(hospital_id, id)
UNIQUE (hospital_id, source_id, checksum), UNIQUE (hospital_id, id)
CHECK (content_addressed_key <> '')
```
- `claim_sources.source_version_id`(3열FK) + `page_or_location` + `span_hash`. `UNIQUE(hospital_id, claim_id, source_version_id, span_hash)` → 같은 논문 여러 구간 허용.
- 파일 교체 = **새 source_version**(기존 불변) → 과거 승인이 가리킨 정확한 콘텐츠 재현.

---

## 8. `migration_imports` + 재실행·rollback

```
migration_imports
- id uuid PK, hospital_id
- source_uri text NOT NULL, raw_sha256 text NOT NULL, canonical_sha256 text
- migration_version text NOT NULL
- status text NOT NULL CHECK(pending|imported|validated|failed)
- script_id uuid NULL, version_id uuid NULL, error_code text
- started_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz NULL
UNIQUE (hospital_id, source_uri, raw_sha256, migration_version)
```
**순서(커밋 전 검증)**:
```
원본 durable backup + checksum → JSON schema 검증 → in-memory 변환
→ canonical round-trip 비교(메모리) → BEGIN
→ script/version/block/sentence/claim(unverified) 삽입
→ DB에서 재조회 canonical 비교 → 불일치면 ROLLBACK(commit 전)
→ 일치면 COMMIT → migration_imports=imported
→ [별도] evidence checker 실행 → claim_assessments append → status=validated
```
- **evidence 실패는 구조 롤백 사유 아님** → claim은 unverified/failed로 남기고 **승인만 차단**(GPT 반영).
- 멱등: `migration_imports` UNIQUE로 **성공(validated)만 skip**, 실패건은 재시도 가능(hash-skip이 실패 데이터 영구화하던 문제 해결).
- **렌더 비교 분리**: `say`·block 순서·scene·timecode·tags = **100% 정확 일치**; HTML은 DOM canonicalization 후 보조 비교(CSS·속성순서·생성시각·비의미 공백 제외); 의료 문장·수치·단위 = **fuzzy 금지**.
- 원본 JSON = `out/_backup/`(읽기전용) 보존, 미삭제.

---

## 9. 익명 approve 제거 후 승인 방식

- `review_links.permission`: 익명 링크는 **`comment_only`만**. `approve`는 익명 발급 금지(발급 API에서 차단).
- **최종 승인** = (a) 로그인 approver membership, 또는 (b) 이메일 OTP·일회성 challenge로 신원 확인 후. bearer 링크만으로 승인 불가.
- `approvals`(append-only 결정): `status`(approved|revoked|rejected) + `approved_at/revoked_at` 조합 CHECK, `approver_membership_id`, `assessment_set_hash`·`version_content_hash`·`compliance_policy_version` → **무엇을 근거로 승인했는지** 재현.
- 승인 전 게이트: 금지어 PASS + 모든 medical claim이 latest assessment로 `verified AND support_level∈(direct[, partial/inferred+override])`. high-risk는 direct 요구.

---

## 10. 동시 편집 충돌 방지

- 편집 트랜잭션: `SELECT ... FROM scripts WHERE id=? FOR UPDATE` → `version_no` 할당 → 새 version 생성 → `scripts.current_version_id` **compare-and-swap**(`WHERE current_version_id = :expected`). 불일치 = 다른 편집이 선반영 → **HTTP 409**.
- 클라이언트는 편집 시작 시 본 `expected_current_version_id`를 제출. 낙관적 동시성.
- 스키마: 별도 lock 컬럼 불필요(행 잠금+CAS). 필요 시 `scripts.lock_version int`(optimistic) 추가 옵션.

---

## 11. 기존 설계에서 유지한 항목

- PostgreSQL·SQLAlchemy·Alembic, 공용 DB + 전 테넌트 테이블 `hospital_id`.
- UUID PK(단, 표현은 "열거 난이도 완화"로 정정; 실제 통제는 RLS+authz).
- version/block/claim/sentence/source_version **immutable**, 편집=새 버전.
- `out/*_package.json` = 내보내기·백업(원본 아님).
- 근거 대조 = evidence checker가 **원문 재대조**(director 자기신고 불신).
- Style Rule은 **사람 승인분만** 프롬프트 주입, scope(global/hospital/doctor/topic) 검색.
- 파일 본체는 DB 밖(경로/키+checksum만).

---

## 12. Alembic upgrade / downgrade 순서

**upgrade**:
```
1. CREATE EXTENSION IF NOT EXISTS citext; (+ pgcrypto for gen_random_uuid)
2. roles/RLS 준비(app_owner/app_rw) — 운영은 DBA 스크립트로 분리 가능
3. hospitals → users → hospital_memberships → membership_roles
4. scripts (current_version_id FK 없이)
5. script_versions
6. add FK scripts.current_version (use_alter)
7. script_blocks → script_sentences → claims → claim_assessments
8. sources → source_versions → claim_sources
9. approvals → review_links → review_comments
10. edits → style_rules → style_rule_sources
11. jobs → migration_imports → audit_events → notification_outbox → rate_limit_buckets
12. CREATE VIEW claim_latest_assessment
13. ENABLE/FORCE RLS + CREATE POLICY (테넌트 테이블 전부)
14. 부분 유니크 인덱스·FK 보조 인덱스
```
**downgrade**: 역순. RLS/policy drop → view drop → 인덱스 drop → 테이블 drop(자식→부모) → `scripts.current_version` FK를 use_alter로 먼저 제거 후 scripts drop → extension은 보존(다른 스키마 영향).

데이터 마이그레이션(`store/migrate.py`)은 Alembic과 **분리**(멱등 스크립트), 스키마 upgrade 후 실행.

---

## 남은 확인 (기본값 제안)
1. 리뷰 토큰 = **HMAC-SHA256(bytea)** 저장 + 접근 후 세션쿠키 교환·clean URL redirect·`Referrer-Policy:no-referrer`·`Cache-Control:no-store`·CSRF — 이대로?
2. 댓글 = **저장은 raw, 출력 시 문맥별 escape(단일 인코딩)** — GPT 지적대로 double-encode 회피. 이대로?
3. rate limit = 우선 **PG bucket 테이블**(무료·단일 인스턴스), 확장 시 Redis — 이대로?

이 10개(3열FK·RLS·sentences·assessments·source_versions·migration_imports·익명approve제거·enum NOT NULL·동시성·uuid[]제거) 반영한 수정 ERD/DDL 초안입니다. **승인해주시면 1단계 구현(모델+Alembic+migrate.py, 이명 1편) 착수**하겠습니다.
