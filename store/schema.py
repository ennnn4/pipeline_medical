"""
P0/P1 데이터 모델 — SQLAlchemy Core 테이블 정의 (PostgreSQL 기준).

설계 근거: docs/P1_ERD_스키마보고.md, P2_수정ERD_DDL초안.md, P2_1_수정델타.md +
1단계 조건부 승인 5개 요건(version_approval_states 무결성, review_sessions,
claim_assessments effective 규칙, migration lease 원자화, users/role 확정).

- 테넌트 격리: 모든 테넌트 테이블에 hospital_id + UNIQUE(hospital_id, id) + 3열 복합 FK로 계보 고정.
- 불변(immutable): script_versions/blocks/sentences/claims/claim_assessments/source_versions (앱계층 강제).
- RLS·역할·SECURITY DEFINER 함수·view는 여기 없음 → store/rls_sql.py + Alembic revision의 raw SQL.
- 실제 실행/RLS 검증은 PostgreSQL 필요(로컬 환경엔 PG 없음 → 오프라인 DDL로 diff 확인).
"""
from sqlalchemy import (
    MetaData, Table, Column, ForeignKeyConstraint, UniqueConstraint,
    CheckConstraint, Index, PrimaryKeyConstraint, text,
    String, Text, Integer, BigInteger, Boolean, Numeric, LargeBinary,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB, TIMESTAMP

# 제약 자동 명명(Alembic 안정성)
NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=NAMING)

# ── 공통 컬럼 헬퍼 ─────────────────────────────────────────────
def uuid_pk():
    return Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
def hospital_col():
    return Column("hospital_id", UUID(as_uuid=True), nullable=False)
def ts(name, nullable=False, default_now=True):
    return Column(name, TIMESTAMP(timezone=True), nullable=nullable,
                  server_default=text("now()") if (default_now and not nullable) else None)

# 테넌트 테이블 공통: UNIQUE(hospital_id, id) → 자식의 (hospital_id, id) 복합 FK 참조 대상
def tenant_id_uq(t): return UniqueConstraint("hospital_id", "id", name=f"uq_{t}_hospital_id_id")

# ════════════════════════════════════════════════════════════
# 1. 테넌트 루트 · 사용자 · 멤버십
# ════════════════════════════════════════════════════════════
hospitals = Table(
    "hospitals", metadata,
    uuid_pk(),
    Column("slug", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    # 자기승인 정책(inv12) — 기본 금지. true여도 admin(self_override capability)+사유 필요.
    Column("allow_self_approval", Boolean, nullable=False, server_default=text("false")),
    ts("created_at"),
    UniqueConstraint("slug", name="uq_hospitals_slug"),
    CheckConstraint("status IN ('active','suspended','archived')", name="status"),
)

# 전역 테이블(테넌트 밖). app_rw 직접 SELECT 금지 → auth 함수/제한 view로만 접근(rls_sql).
users = Table(
    "users", metadata,
    uuid_pk(),
    Column("email", Text, nullable=False),          # citext는 rls_sql에서 도메인/extension 처리
    Column("name", Text),
    Column("pw_hash", Text),                          # reviewer(외부)는 NULL
    ts("created_at"),
    UniqueConstraint("email", name="uq_users_email"),
    CheckConstraint("email = btrim(email)", name="email_trimmed"),
)

hospital_memberships = Table(
    "hospital_memberships", metadata,
    uuid_pk(), hospital_col(),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    ts("created_at"),
    Column("archived_at", TIMESTAMP(timezone=True)),
    ForeignKeyConstraint(["hospital_id"], ["hospitals.id"], name="fk_memberships_hospital"),
    ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_memberships_user"),
    UniqueConstraint("hospital_id", "user_id", name="uq_memberships_hospital_user"),
    tenant_id_uq("hospital_memberships"),            # actor 복합 FK 참조 대상
)

# 멤버십당 역할 다중 허용(editor/reviewer/approver/admin)
membership_roles = Table(
    "membership_roles", metadata,
    uuid_pk(), hospital_col(),
    Column("membership_id", UUID(as_uuid=True), nullable=False),
    Column("role", Text, nullable=False),
    ts("created_at"),
    ForeignKeyConstraint(["hospital_id", "membership_id"],
                         ["hospital_memberships.hospital_id", "hospital_memberships.id"],
                         name="fk_membership_roles_membership"),
    UniqueConstraint("hospital_id", "membership_id", "role", name="uq_membership_roles_unique"),
    CheckConstraint("role IN ('editor','reviewer','approver','admin')", name="role"),
)

# ════════════════════════════════════════════════════════════
# 2. 대본 · 버전 · 블록 · 문장
# ════════════════════════════════════════════════════════════
scripts = Table(
    "scripts", metadata,
    uuid_pk(), hospital_col(),
    Column("speaker_membership_id", UUID(as_uuid=True)),   # 화자(선택)
    Column("topic", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'draft'")),  # 파생 취급
    Column("current_version_id", UUID(as_uuid=True)),      # 순환 FK → Alembic에서 use_alter로 추가
    Column("created_by_membership_id", UUID(as_uuid=True)),
    ts("created_at"), ts("updated_at"),
    Column("archived_at", TIMESTAMP(timezone=True)),
    Column("archived_by_membership_id", UUID(as_uuid=True)),
    Column("archive_reason", Text),
    ForeignKeyConstraint(["hospital_id"], ["hospitals.id"], name="fk_scripts_hospital"),
    ForeignKeyConstraint(["hospital_id", "created_by_membership_id"],
                         ["hospital_memberships.hospital_id", "hospital_memberships.id"],
                         name="fk_scripts_created_by"),
    ForeignKeyConstraint(["hospital_id", "speaker_membership_id"],
                         ["hospital_memberships.hospital_id", "hospital_memberships.id"],
                         name="fk_scripts_speaker"),
    # current_version 3열 FK: 반드시 이 script의 버전 → Alembic op.create_foreign_key(use_alter)
    #   (hospital_id, id, current_version_id) -> script_versions(hospital_id, script_id, id)
    UniqueConstraint("hospital_id", "id", name="uq_scripts_hospital_id_id"),  # (hospital_id,id,cur) 참조 위해 (hospital_id,id) 필요
    CheckConstraint("status IN ('draft','in_review','approved','archived')", name="status"),
    Index("ix_scripts_hospital_topic", "hospital_id", "topic"),
)

script_versions = Table(
    "script_versions", metadata,
    uuid_pk(), hospital_col(),
    Column("script_id", UUID(as_uuid=True), nullable=False),
    Column("parent_version_id", UUID(as_uuid=True)),
    Column("version_no", Integer, nullable=False),
    Column("source", Text, nullable=False),          # ai|editor|migration
    Column("creation_reason", Text),
    Column("source_package_hash", Text),             # 마이그레이션 원본 checksum
    Column("created_by_membership_id", UUID(as_uuid=True)),   # 편집자(source=editor) 또는 생성 요청자(source=ai)
    Column("generation_job_id", UUID(as_uuid=True)),          # source=ai일 때 어떤 job이 만들었는지(작성자 유형)
    # (hospital_id, generation_job_id)→generation_jobs 복합 FK는 Alembic/ensure에서(메타데이터 밖 테이블)
    ts("created_at"),
    ForeignKeyConstraint(["hospital_id", "script_id"], ["scripts.hospital_id", "scripts.id"],
                         name="fk_versions_script"),
    # parent = 같은 script
    ForeignKeyConstraint(["hospital_id", "script_id", "parent_version_id"],
                         ["script_versions.hospital_id", "script_versions.script_id", "script_versions.id"],
                         name="fk_versions_parent"),
    ForeignKeyConstraint(["hospital_id", "created_by_membership_id"],
                         ["hospital_memberships.hospital_id", "hospital_memberships.id"],
                         name="fk_versions_created_by"),
    UniqueConstraint("hospital_id", "script_id", "version_no", name="uq_versions_script_versionno"),
    UniqueConstraint("hospital_id", "script_id", "id", name="uq_versions_hospital_script_id"),  # current/parent/edit 참조용
    tenant_id_uq("script_versions"),                 # block/comment/link/approval 참조용
    CheckConstraint("source IN ('ai','editor','migration')", name="source"),
    Index("ix_versions_script", "hospital_id", "script_id"),
    Index("ix_versions_parent", "hospital_id", "parent_version_id"),
)

script_blocks = Table(
    "script_blocks", metadata,
    uuid_pk(), hospital_col(),
    Column("version_id", UUID(as_uuid=True), nullable=False),
    Column("stable_block_key", Text, nullable=False),
    Column("order_index", Integer, nullable=False),
    Column("block_type", Text, nullable=False),
    Column("scene", Text),
    Column("text", Text, nullable=False),
    Column("tc_start", Text), Column("tc_end", Text),
    Column("tc_start_ms", BigInteger), Column("tc_end_ms", BigInteger),
    Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    ts("created_at"),
    ForeignKeyConstraint(["hospital_id", "version_id"], ["script_versions.hospital_id", "script_versions.id"],
                         name="fk_blocks_version"),
    UniqueConstraint("hospital_id", "version_id", "order_index", name="uq_blocks_version_order"),
    UniqueConstraint("hospital_id", "version_id", "stable_block_key", name="uq_blocks_version_key"),
    UniqueConstraint("hospital_id", "version_id", "id", name="uq_blocks_hospital_version_id"),  # sentence/comment 참조용
    CheckConstraint("block_type IN ('intro','explanation','evidence','transition','analogy','example','summary','cta','other')", name="block_type"),
    CheckConstraint("tc_start_ms IS NULL OR tc_end_ms IS NULL OR (tc_start_ms >= 0 AND tc_end_ms >= tc_start_ms)", name="tc_ms_range"),
    Index("ix_blocks_version", "hospital_id", "version_id"),
)

script_sentences = Table(
    "script_sentences", metadata,
    uuid_pk(), hospital_col(),
    Column("version_id", UUID(as_uuid=True), nullable=False),
    Column("block_id", UUID(as_uuid=True), nullable=False),
    Column("sentence_index", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("start_offset", Integer, nullable=False),
    Column("end_offset", Integer, nullable=False),
    Column("offset_unit", Text, nullable=False),     # utf16|codepoint (브라우저 anchor 정합)
    Column("segmenter_version", Text, nullable=False),
    ts("created_at"),
    ForeignKeyConstraint(["hospital_id", "version_id", "block_id"],
                         ["script_blocks.hospital_id", "script_blocks.version_id", "script_blocks.id"],
                         name="fk_sentences_block"),
    UniqueConstraint("hospital_id", "version_id", "block_id", "sentence_index", name="uq_sentences_block_idx"),
    UniqueConstraint("hospital_id", "version_id", "id", name="uq_sentences_hospital_version_id"),  # claim 참조용
    CheckConstraint("offset_unit IN ('utf16','codepoint')", name="offset_unit"),
    CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="offset_range"),
    Index("ix_sentences_block", "hospital_id", "version_id", "block_id"),
)

# ════════════════════════════════════════════════════════════
# 3. 의학 주장(claim) · 검증(append-only) · 근거 소스
# ════════════════════════════════════════════════════════════
claims = Table(
    "claims", metadata,
    uuid_pk(), hospital_col(),
    Column("version_id", UUID(as_uuid=True), nullable=False),
    Column("sentence_id", UUID(as_uuid=True), nullable=False),
    Column("claim_index", Integer, nullable=False, server_default=text("0")),
    Column("claim_text", Text, nullable=False),
    Column("claim_type", Text, nullable=False),
    Column("detection_method", Text, nullable=False),   # llm|regex|migration
    ts("created_at"),
    ForeignKeyConstraint(["hospital_id", "version_id", "sentence_id"],
                         ["script_sentences.hospital_id", "script_sentences.version_id", "script_sentences.id"],
                         name="fk_claims_sentence"),
    UniqueConstraint("hospital_id", "sentence_id", "claim_index", name="uq_claims_sentence_idx"),
    tenant_id_uq("claims"),                          # assessment/claim_sources 참조용
    CheckConstraint("claim_type IN ('term_definition','cause','mechanism','treatment_effect','test_interpretation','statistic','study_result','association','patient_judgment','other')", name="claim_type"),
    CheckConstraint("detection_method IN ('llm','regex','migration')", name="detection_method"),
    Index("ix_claims_version", "hospital_id", "version_id"),
)

# 검증 결과 append-only. 사람 판정을 자동이 덮지 않도록 kind·supersedes·idempotency 포함.
claim_assessments = Table(
    "claim_assessments", metadata,
    uuid_pk(), hospital_col(),
    Column("claim_id", UUID(as_uuid=True), nullable=False),
    Column("assessment_kind", Text, nullable=False),     # automated|human_review|override|migration
    Column("idempotency_key", Text, nullable=False),     # 자동 재실행 중복 방지
    Column("supersedes_assessment_id", UUID(as_uuid=True)),
    Column("checker_version", Text),
    Column("model", Text), Column("prompt_hash", Text), Column("source_set_hash", Text),
    Column("support_level", Text, nullable=False),       # direct|partial|inferred|unsupported|unverified
    Column("verification_status", Text, nullable=False), # pending|verified|failed
    Column("medical_risk", Text, nullable=False),        # low|medium|high
    Column("rationale", Text),
    Column("created_by_membership_id", UUID(as_uuid=True)),
    ts("created_at"),
    ForeignKeyConstraint(["hospital_id", "claim_id"], ["claims.hospital_id", "claims.id"],
                         name="fk_assessments_claim"),
    ForeignKeyConstraint(["hospital_id", "supersedes_assessment_id"],
                         ["claim_assessments.hospital_id", "claim_assessments.id"],
                         name="fk_assessments_supersedes"),
    ForeignKeyConstraint(["hospital_id", "created_by_membership_id"],
                         ["hospital_memberships.hospital_id", "hospital_memberships.id"],
                         name="fk_assessments_created_by"),
    UniqueConstraint("hospital_id", "claim_id", "idempotency_key", name="uq_assessments_idempotency"),
    tenant_id_uq("claim_assessments"),
    CheckConstraint("assessment_kind IN ('automated','human_review','override','migration')", name="assessment_kind"),
    CheckConstraint("support_level IN ('direct','partial','inferred','unsupported','unverified')", name="support_level"),
    CheckConstraint("verification_status IN ('pending','verified','failed')", name="verification_status"),
    CheckConstraint("medical_risk IN ('low','medium','high')", name="medical_risk"),
    # human/override는 사람 actor 필수
    CheckConstraint("assessment_kind NOT IN ('human_review','override') OR created_by_membership_id IS NOT NULL", name="human_actor_required"),
    Index("ix_assessments_latest", "hospital_id", "claim_id", text("created_at DESC"), text("id DESC")),
)

sources = Table(
    "sources", metadata,
    uuid_pk(), hospital_col(),
    Column("title", Text, nullable=False),
    Column("source_type", Text, nullable=False),
    Column("citation_metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    ts("created_at"),
    Column("archived_at", TIMESTAMP(timezone=True)),
    ForeignKeyConstraint(["hospital_id"], ["hospitals.id"], name="fk_sources_hospital"),
    tenant_id_uq("sources"),
    CheckConstraint("source_type IN ('paper','kb','survey','interview','lecture','competitor_meta','other')", name="source_type"),
)

# 불변 콘텐츠 버전(content-addressed) — 승인 당시 근거 재현
source_versions = Table(
    "source_versions", metadata,
    uuid_pk(), hospital_col(),
    Column("source_id", UUID(as_uuid=True), nullable=False),
    Column("checksum", Text, nullable=False),
    Column("content_addressed_key", Text, nullable=False),   # 로컬=경로, 운영=object key
    Column("extractor_version", Text),
    Column("mime", Text), Column("size_bytes", BigInteger), Column("page_count", Integer),
    ts("created_at"),
    ForeignKeyConstraint(["hospital_id", "source_id"], ["sources.hospital_id", "sources.id"],
                         name="fk_source_versions_source"),
    UniqueConstraint("hospital_id", "source_id", "checksum", name="uq_source_versions_checksum"),
    tenant_id_uq("source_versions"),
    CheckConstraint("content_addressed_key <> ''", name="key_nonempty"),
)

claim_sources = Table(
    "claim_sources", metadata,
    uuid_pk(), hospital_col(),
    Column("claim_id", UUID(as_uuid=True), nullable=False),
    Column("source_version_id", UUID(as_uuid=True), nullable=False),
    Column("source_quote", Text),
    Column("page_or_location", Text),
    Column("span_hash", Text, nullable=False, server_default=text("''")),
    Column("relation_type", Text, nullable=False),
    Column("confidence", Numeric(5, 4)),
    Column("verified_by_membership_id", UUID(as_uuid=True)),
    Column("verified_at", TIMESTAMP(timezone=True)),
    ts("created_at"),
    ForeignKeyConstraint(["hospital_id", "claim_id"], ["claims.hospital_id", "claims.id"],
                         name="fk_claim_sources_claim"),
    ForeignKeyConstraint(["hospital_id", "source_version_id"],
                         ["source_versions.hospital_id", "source_versions.id"],
                         name="fk_claim_sources_sourceversion"),
    ForeignKeyConstraint(["hospital_id", "verified_by_membership_id"],
                         ["hospital_memberships.hospital_id", "hospital_memberships.id"],
                         name="fk_claim_sources_verified_by"),
    UniqueConstraint("hospital_id", "claim_id", "source_version_id", "span_hash", name="uq_claim_sources_span"),
    tenant_id_uq("claim_sources"),
    CheckConstraint("relation_type IN ('directly_supports','partially_supports','contradicts','context_only')", name="relation_type"),
    CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"),
)

# ════════════════════════════════════════════════════════════
# 4. 승인 상태(mutable 1행/버전) — append-only 이력은 audit_events
# ════════════════════════════════════════════════════════════
version_approval_states = Table(
    "version_approval_states", metadata,
    uuid_pk(), hospital_col(),
    Column("version_id", UUID(as_uuid=True), nullable=False),
    Column("status", Text, nullable=False, server_default=text("'none'")),
    Column("approver_membership_id", UUID(as_uuid=True)),
    Column("assessment_set_hash", Text),
    Column("version_content_hash", Text),
    Column("compliance_policy_version", Text),
    Column("decided_at", TIMESTAMP(timezone=True)),
    # superseded는 승인 상태가 아니라 '더 이상 current 아님' 수명주기(inv14, GPT) — 승인 이력은 status로 보존.
    Column("superseded_by_version_id", UUID(as_uuid=True)),
    Column("superseded_at", TIMESTAMP(timezone=True)),
    ts("updated_at"),
    ForeignKeyConstraint(["hospital_id", "version_id"], ["script_versions.hospital_id", "script_versions.id"],
                         name="fk_approval_states_version"),
    ForeignKeyConstraint(["hospital_id", "approver_membership_id"],
                         ["hospital_memberships.hospital_id", "hospital_memberships.id"],
                         name="fk_approval_states_approver"),
    ForeignKeyConstraint(["hospital_id", "superseded_by_version_id"],
                         ["script_versions.hospital_id", "script_versions.id"],
                         name="fk_approval_states_superseded_by"),
    UniqueConstraint("hospital_id", "version_id", name="uq_approval_states_version"),  # 버전당 1행
    tenant_id_uq("version_approval_states"),
    CheckConstraint("status IN ('none','approved','rejected','revoked')", name="status"),
    # revoked(승인 철회) → 결정자·시각 필수(rejected와 동일)
    CheckConstraint("status <> 'revoked' OR (approver_membership_id IS NOT NULL AND decided_at IS NOT NULL)",
                    name="revoked_fields"),
    # approved → 승인 메타 전부 NOT NULL
    CheckConstraint(
        "status <> 'approved' OR ("
        "approver_membership_id IS NOT NULL AND assessment_set_hash IS NOT NULL AND "
        "version_content_hash IS NOT NULL AND compliance_policy_version IS NOT NULL AND decided_at IS NOT NULL)",
        name="approved_fields"),
    # none → 승인 메타 NULL
    CheckConstraint(
        "status <> 'none' OR ("
        "approver_membership_id IS NULL AND assessment_set_hash IS NULL AND "
        "version_content_hash IS NULL AND compliance_policy_version IS NULL AND decided_at IS NULL)",
        name="none_fields"),
    # rejected → approver + decided_at NOT NULL
    CheckConstraint("status <> 'rejected' OR (approver_membership_id IS NOT NULL AND decided_at IS NOT NULL)",
                    name="rejected_fields"),
)

# ════════════════════════════════════════════════════════════
# 5. 외부 리뷰 링크 · 세션 · 댓글
# ════════════════════════════════════════════════════════════
review_links = Table(
    "review_links", metadata,
    uuid_pk(), hospital_col(),
    Column("version_id", UUID(as_uuid=True), nullable=False),
    Column("token_hash", LargeBinary, nullable=False),   # HMAC-SHA256 digest(bytea) — 원본 미저장
    Column("reviewer_name", Text),
    Column("permission", Text, nullable=False, server_default=text("'comment_only'")),
    Column("created_by_membership_id", UUID(as_uuid=True)),
    Column("expires_at", TIMESTAMP(timezone=True), nullable=False),
    Column("revoked_at", TIMESTAMP(timezone=True)),
    ts("created_at"),
    ForeignKeyConstraint(["hospital_id", "version_id"], ["script_versions.hospital_id", "script_versions.id"],
                         name="fk_review_links_version"),
    ForeignKeyConstraint(["hospital_id", "created_by_membership_id"],
                         ["hospital_memberships.hospital_id", "hospital_memberships.id"],
                         name="fk_review_links_created_by"),
    UniqueConstraint("token_hash", name="uq_review_links_token"),
    UniqueConstraint("hospital_id", "version_id", "id", name="uq_review_links_hospital_version_id"),  # comment 참조용
    tenant_id_uq("review_links"),
    CheckConstraint("permission IN ('comment_only','approve')", name="permission"),
    Index("ix_review_links_version", "hospital_id", "version_id"),
)

# 서버측 세션(링크 폐기 시 세션도 폐기 가능)
review_sessions = Table(
    "review_sessions", metadata,
    uuid_pk(), hospital_col(),
    Column("review_link_id", UUID(as_uuid=True), nullable=False),
    Column("session_token_hash", LargeBinary, nullable=False),
    Column("expires_at", TIMESTAMP(timezone=True), nullable=False),
    Column("revoked_at", TIMESTAMP(timezone=True)),
    Column("last_seen_at", TIMESTAMP(timezone=True)),
    ts("created_at"),
    ForeignKeyConstraint(["hospital_id", "review_link_id"], ["review_links.hospital_id", "review_links.id"],
                         name="fk_review_sessions_link"),
    UniqueConstraint("session_token_hash", name="uq_review_sessions_token"),
    tenant_id_uq("review_sessions"),
    Index("ix_review_sessions_link", "hospital_id", "review_link_id"),
)

review_comments = Table(
    "review_comments", metadata,
    uuid_pk(), hospital_col(),
    Column("version_id", UUID(as_uuid=True), nullable=False),
    Column("block_id", UUID(as_uuid=True)),
    Column("anchor_start", Integer), Column("anchor_end", Integer),
    Column("review_link_id", UUID(as_uuid=True)),            # 외부 댓글 출처(내부=NULL)
    Column("author_membership_id", UUID(as_uuid=True)),      # 내부 댓글 작성자
    Column("reviewer_name", Text, nullable=False),           # 서버 파생(요청값 불신)
    Column("comment", Text, nullable=False),                 # raw 저장 / 출력 시 escape
    Column("status", Text, nullable=False, server_default=text("'open'")),
    Column("resolved_by_membership_id", UUID(as_uuid=True)),
    Column("resolved_at", TIMESTAMP(timezone=True)),
    ts("created_at"),
    ForeignKeyConstraint(["hospital_id", "version_id"], ["script_versions.hospital_id", "script_versions.id"],
                         name="fk_comments_version"),
    ForeignKeyConstraint(["hospital_id", "version_id", "block_id"],
                         ["script_blocks.hospital_id", "script_blocks.version_id", "script_blocks.id"],
                         name="fk_comments_block"),
    ForeignKeyConstraint(["hospital_id", "version_id", "review_link_id"],
                         ["review_links.hospital_id", "review_links.version_id", "review_links.id"],
                         name="fk_comments_link"),
    ForeignKeyConstraint(["hospital_id", "author_membership_id"],
                         ["hospital_memberships.hospital_id", "hospital_memberships.id"],
                         name="fk_comments_author"),
    tenant_id_uq("review_comments"),
    CheckConstraint("status IN ('open','accepted','rejected','resolved','needs_discussion')", name="status"),
    # 전체댓글(block NULL)이면 anchor NULL; anchor 있으면 block NOT NULL
    CheckConstraint("(block_id IS NOT NULL) OR (anchor_start IS NULL AND anchor_end IS NULL)", name="anchor_needs_block"),
    CheckConstraint("(anchor_start IS NULL AND anchor_end IS NULL) OR (anchor_start >= 0 AND anchor_end > anchor_start)", name="anchor_range"),
    # 출처는 내부(author) 또는 외부(link) 중 하나 이상
    CheckConstraint("author_membership_id IS NOT NULL OR review_link_id IS NOT NULL", name="comment_origin"),
    Index("ix_comments_version", "hospital_id", "version_id"),
    Index("ix_comments_block", "hospital_id", "block_id"),
    Index("ix_comments_status", "hospital_id", "status"),
)

# ════════════════════════════════════════════════════════════
# 6. 수정 이벤트 · Style Rule(+출처 조인) · 감사 · 알림 · rate limit · 마이그레이션 · jobs
# ════════════════════════════════════════════════════════════
edits = Table(
    "edits", metadata,
    uuid_pk(), hospital_col(),
    Column("script_id", UUID(as_uuid=True), nullable=False),
    Column("from_version_id", UUID(as_uuid=True)),
    Column("to_version_id", UUID(as_uuid=True), nullable=False),
    Column("stable_block_key", Text, nullable=False),
    Column("before_text", Text), Column("after_text", Text),
    Column("category", Text, nullable=False),
    Column("reason", Text),
    Column("scope", Text, nullable=False, server_default=text("'hospital'")),
    Column("approved_for_learning", Boolean, nullable=False, server_default=text("false")),
    Column("created_by_membership_id", UUID(as_uuid=True)),
    ts("created_at"),
    ForeignKeyConstraint(["hospital_id", "script_id"], ["scripts.hospital_id", "scripts.id"],
                         name="fk_edits_script"),
    ForeignKeyConstraint(["hospital_id", "script_id", "from_version_id"],
                         ["script_versions.hospital_id", "script_versions.script_id", "script_versions.id"],
                         name="fk_edits_from_version"),
    ForeignKeyConstraint(["hospital_id", "script_id", "to_version_id"],
                         ["script_versions.hospital_id", "script_versions.script_id", "script_versions.id"],
                         name="fk_edits_to_version"),
    ForeignKeyConstraint(["hospital_id", "created_by_membership_id"],
                         ["hospital_memberships.hospital_id", "hospital_memberships.id"],
                         name="fk_edits_created_by"),
    tenant_id_uq("edits"),
    CheckConstraint("category IN ('tone','awkwardness','factual','source_grounding','transition','disclaimer','intro','cta','other')", name="category"),
    CheckConstraint("scope IN ('global','hospital','doctor','topic')", name="scope"),
    Index("ix_edits_script", "hospital_id", "script_id"),
    Index("ix_edits_category", "hospital_id", "category"),
)

# scope=global은 hospital_id NULL(테넌트 밖) — RLS는 rls_sql에서 작업별 정책
style_rules = Table(
    "style_rules", metadata,
    uuid_pk(),
    Column("hospital_id", UUID(as_uuid=True)),       # NULL = global
    Column("doctor_membership_id", UUID(as_uuid=True)),
    Column("topic_scope", Text),
    Column("scope", Text, nullable=False),
    Column("category", Text),
    Column("rule_text", Text, nullable=False),
    Column("positive_example", Text), Column("negative_example", Text),
    Column("status", Text, nullable=False, server_default=text("'proposed'")),
    Column("approved_by_membership_id", UUID(as_uuid=True)),
    ts("created_at"),
    ForeignKeyConstraint(["hospital_id"], ["hospitals.id"], name="fk_style_rules_hospital"),
    CheckConstraint("scope IN ('global','hospital','doctor','topic')", name="scope"),
    CheckConstraint("status IN ('proposed','approved','retired')", name="status"),
    # scope별 hospital_id NULL 조합 강제
    CheckConstraint("(scope='global' AND hospital_id IS NULL) OR (scope<>'global' AND hospital_id IS NOT NULL)", name="scope_hospital"),
    CheckConstraint("scope<>'doctor' OR doctor_membership_id IS NOT NULL", name="doctor_scope"),
    CheckConstraint("scope<>'topic' OR topic_scope IS NOT NULL", name="topic_scope"),
    Index("ix_style_rules_scope", "scope"),
    Index("ix_style_rules_hospital", "hospital_id"),
    Index("ix_style_rules_status", "status"),
)

# uuid[] 대신 조인 테이블(원소별 FK·테넌트 출처)
style_rule_sources = Table(
    "style_rule_sources", metadata,
    uuid_pk(),
    Column("style_rule_id", UUID(as_uuid=True), nullable=False),
    Column("origin_hospital_id", UUID(as_uuid=True), nullable=False),
    Column("edit_id", UUID(as_uuid=True), nullable=False),
    ts("created_at"),
    ForeignKeyConstraint(["style_rule_id"], ["style_rules.id"], name="fk_srs_rule"),
    ForeignKeyConstraint(["origin_hospital_id", "edit_id"], ["edits.hospital_id", "edits.id"],
                         name="fk_srs_edit"),
    UniqueConstraint("style_rule_id", "edit_id", name="uq_srs_rule_edit"),
)

audit_events = Table(
    "audit_events", metadata,
    uuid_pk(),
    Column("hospital_id", UUID(as_uuid=True)),        # 플랫폼(global) 행위는 NULL 가능
    Column("actor_membership_id", UUID(as_uuid=True)),
    Column("actor_review_link_id", UUID(as_uuid=True)),
    Column("action", Text, nullable=False),
    Column("entity_type", Text), Column("entity_id", UUID(as_uuid=True)),
    Column("request_id", Text),
    Column("before_hash", Text), Column("after_hash", Text),
    Column("ip_hash", Text), Column("ua_hash", Text),
    ts("created_at"),
    Index("ix_audit_hospital_created", "hospital_id", text("created_at DESC")),
    Index("ix_audit_entity", "entity_type", "entity_id"),
)

notification_outbox = Table(
    "notification_outbox", metadata,
    uuid_pk(),
    Column("hospital_id", UUID(as_uuid=True)),
    Column("idempotency_key", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("recipient", Text, nullable=False),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    ts("created_at"), Column("sent_at", TIMESTAMP(timezone=True)),
    UniqueConstraint("idempotency_key", name="uq_outbox_idempotency"),
    CheckConstraint("status IN ('pending','sent','failed')", name="status"),
    Index("ix_outbox_status", "status"),
)

rate_limit_buckets = Table(
    "rate_limit_buckets", metadata,
    Column("bucket_key_hash", LargeBinary, nullable=False),
    Column("window_started_at", TIMESTAMP(timezone=True), nullable=False),
    Column("request_count", Integer, nullable=False, server_default=text("0")),
    Column("expires_at", TIMESTAMP(timezone=True), nullable=False),
    PrimaryKeyConstraint("bucket_key_hash", "window_started_at", name="pk_rate_limit_buckets"),
    Index("ix_rate_limit_expires", "expires_at"),
)

migration_imports = Table(
    "migration_imports", metadata,
    uuid_pk(), hospital_col(),
    Column("source_uri", Text, nullable=False),
    Column("raw_sha256", Text, nullable=False),
    Column("canonical_sha256", Text),
    Column("migration_version", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("script_id", UUID(as_uuid=True)),
    Column("version_id", UUID(as_uuid=True)),
    Column("error_code", Text),
    # lease/동시성
    Column("worker_id", Text),
    Column("lease_token", UUID(as_uuid=True)),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("lease_expires_at", TIMESTAMP(timezone=True)),
    Column("last_heartbeat_at", TIMESTAMP(timezone=True)),
    Column("last_error_at", TIMESTAMP(timezone=True)),
    ts("started_at"), Column("completed_at", TIMESTAMP(timezone=True)),
    ForeignKeyConstraint(["hospital_id"], ["hospitals.id"], name="fk_migration_imports_hospital"),
    UniqueConstraint("hospital_id", "source_uri", "raw_sha256", "migration_version", name="uq_migration_imports_src"),
    CheckConstraint("status IN ('pending','imported','validated','failed')", name="status"),
    Index("ix_migration_imports_status", "status", "lease_expires_at"),
)

jobs = Table(
    "jobs", metadata,
    uuid_pk(), hospital_col(),
    Column("topic", Text),
    Column("status", Text),
    Column("ok", Boolean),
    Column("log", Text),
    ts("started_at"), ts("updated_at"),
    ForeignKeyConstraint(["hospital_id"], ["hospitals.id"], name="fk_jobs_hospital"),
    Index("ix_jobs_hospital_updated", "hospital_id", text("updated_at DESC")),
)

# 테넌트 RLS 대상 테이블(전역 users·style_rules(global)·rate_limit 제외 관리는 rls_sql에서)
TENANT_TABLES = [
    "hospital_memberships", "membership_roles", "scripts", "script_versions",
    "script_blocks", "script_sentences", "claims", "claim_assessments",
    "sources", "source_versions", "claim_sources", "version_approval_states",
    "review_links", "review_sessions", "review_comments", "edits",
    "migration_imports", "jobs",
]
