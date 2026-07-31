-- 오프라인 렌더 DDL (PostgreSQL). RLS/함수/뷰/역할은 store/rls_sql.py(작성예정)에.

CREATE TABLE audit_events (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID, 
	actor_membership_id UUID, 
	actor_review_link_id UUID, 
	action TEXT NOT NULL, 
	entity_type TEXT, 
	entity_id UUID, 
	request_id TEXT, 
	before_hash TEXT, 
	after_hash TEXT, 
	ip_hash TEXT, 
	ua_hash TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_audit_events PRIMARY KEY (id)
);
CREATE INDEX ix_audit_hospital_created ON audit_events (hospital_id, created_at DESC);
CREATE INDEX ix_audit_entity ON audit_events (entity_type, entity_id);

CREATE TABLE hospitals (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	slug TEXT NOT NULL, 
	name TEXT NOT NULL, 
	status TEXT DEFAULT 'active' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_hospitals PRIMARY KEY (id), 
	CONSTRAINT uq_hospitals_slug UNIQUE (slug), 
	CONSTRAINT ck_hospitals_status CHECK (status IN ('active','suspended','archived'))
);

CREATE TABLE notification_outbox (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID, 
	idempotency_key TEXT NOT NULL, 
	event_type TEXT NOT NULL, 
	recipient TEXT NOT NULL, 
	payload JSONB DEFAULT '{}'::jsonb NOT NULL, 
	status TEXT DEFAULT 'pending' NOT NULL, 
	attempts INTEGER DEFAULT 0 NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	sent_at TIMESTAMP WITH TIME ZONE, 
	CONSTRAINT pk_notification_outbox PRIMARY KEY (id), 
	CONSTRAINT uq_outbox_idempotency UNIQUE (idempotency_key), 
	CONSTRAINT ck_notification_outbox_status CHECK (status IN ('pending','sent','failed'))
);
CREATE INDEX ix_outbox_status ON notification_outbox (status);

CREATE TABLE rate_limit_buckets (
	bucket_key_hash BYTEA NOT NULL, 
	window_started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	request_count INTEGER DEFAULT 0 NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_rate_limit_buckets PRIMARY KEY (bucket_key_hash, window_started_at)
);
CREATE INDEX ix_rate_limit_expires ON rate_limit_buckets (expires_at);

CREATE TABLE users (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	email TEXT NOT NULL, 
	name TEXT, 
	pw_hash TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_users PRIMARY KEY (id), 
	CONSTRAINT uq_users_email UNIQUE (email), 
	CONSTRAINT ck_users_email_trimmed CHECK (email = btrim(email))
);

CREATE TABLE hospital_memberships (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	archived_at TIMESTAMP WITH TIME ZONE, 
	CONSTRAINT pk_hospital_memberships PRIMARY KEY (id), 
	CONSTRAINT fk_memberships_hospital FOREIGN KEY(hospital_id) REFERENCES hospitals (id), 
	CONSTRAINT fk_memberships_user FOREIGN KEY(user_id) REFERENCES users (id), 
	CONSTRAINT uq_memberships_hospital_user UNIQUE (hospital_id, user_id), 
	CONSTRAINT uq_hospital_memberships_hospital_id_id UNIQUE (hospital_id, id)
);

CREATE TABLE jobs (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	topic TEXT, 
	status TEXT, 
	ok BOOLEAN, 
	log TEXT, 
	started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_jobs PRIMARY KEY (id), 
	CONSTRAINT fk_jobs_hospital FOREIGN KEY(hospital_id) REFERENCES hospitals (id)
);
CREATE INDEX ix_jobs_hospital_updated ON jobs (hospital_id, updated_at DESC);

CREATE TABLE migration_imports (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	source_uri TEXT NOT NULL, 
	raw_sha256 TEXT NOT NULL, 
	canonical_sha256 TEXT, 
	migration_version TEXT NOT NULL, 
	status TEXT DEFAULT 'pending' NOT NULL, 
	script_id UUID, 
	version_id UUID, 
	error_code TEXT, 
	worker_id TEXT, 
	lease_token UUID, 
	attempt_count INTEGER DEFAULT 0 NOT NULL, 
	lease_expires_at TIMESTAMP WITH TIME ZONE, 
	last_heartbeat_at TIMESTAMP WITH TIME ZONE, 
	last_error_at TIMESTAMP WITH TIME ZONE, 
	started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	CONSTRAINT pk_migration_imports PRIMARY KEY (id), 
	CONSTRAINT fk_migration_imports_hospital FOREIGN KEY(hospital_id) REFERENCES hospitals (id), 
	CONSTRAINT uq_migration_imports_src UNIQUE (hospital_id, source_uri, raw_sha256, migration_version), 
	CONSTRAINT ck_migration_imports_status CHECK (status IN ('pending','imported','validated','failed'))
);
CREATE INDEX ix_migration_imports_status ON migration_imports (status, lease_expires_at);

CREATE TABLE sources (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	title TEXT NOT NULL, 
	source_type TEXT NOT NULL, 
	citation_metadata JSONB DEFAULT '{}'::jsonb NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	archived_at TIMESTAMP WITH TIME ZONE, 
	CONSTRAINT pk_sources PRIMARY KEY (id), 
	CONSTRAINT fk_sources_hospital FOREIGN KEY(hospital_id) REFERENCES hospitals (id), 
	CONSTRAINT uq_sources_hospital_id_id UNIQUE (hospital_id, id), 
	CONSTRAINT ck_sources_source_type CHECK (source_type IN ('paper','kb','survey','interview','lecture','competitor_meta','other'))
);

CREATE TABLE style_rules (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID, 
	doctor_membership_id UUID, 
	topic_scope TEXT, 
	scope TEXT NOT NULL, 
	category TEXT, 
	rule_text TEXT NOT NULL, 
	positive_example TEXT, 
	negative_example TEXT, 
	status TEXT DEFAULT 'proposed' NOT NULL, 
	approved_by_membership_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_style_rules PRIMARY KEY (id), 
	CONSTRAINT fk_style_rules_hospital FOREIGN KEY(hospital_id) REFERENCES hospitals (id), 
	CONSTRAINT ck_style_rules_scope CHECK (scope IN ('global','hospital','doctor','topic')), 
	CONSTRAINT ck_style_rules_status CHECK (status IN ('proposed','approved','retired')), 
	CONSTRAINT ck_style_rules_scope_hospital CHECK ((scope='global' AND hospital_id IS NULL) OR (scope<>'global' AND hospital_id IS NOT NULL)), 
	CONSTRAINT ck_style_rules_doctor_scope CHECK (scope<>'doctor' OR doctor_membership_id IS NOT NULL), 
	CONSTRAINT ck_style_rules_topic_scope CHECK (scope<>'topic' OR topic_scope IS NOT NULL)
);
CREATE INDEX ix_style_rules_status ON style_rules (status);
CREATE INDEX ix_style_rules_scope ON style_rules (scope);
CREATE INDEX ix_style_rules_hospital ON style_rules (hospital_id);

CREATE TABLE membership_roles (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	membership_id UUID NOT NULL, 
	role TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_membership_roles PRIMARY KEY (id), 
	CONSTRAINT fk_membership_roles_membership FOREIGN KEY(hospital_id, membership_id) REFERENCES hospital_memberships (hospital_id, id), 
	CONSTRAINT uq_membership_roles_unique UNIQUE (hospital_id, membership_id, role), 
	CONSTRAINT ck_membership_roles_role CHECK (role IN ('editor','reviewer','approver','admin'))
);

CREATE TABLE scripts (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	speaker_membership_id UUID, 
	topic TEXT NOT NULL, 
	status TEXT DEFAULT 'draft' NOT NULL, 
	current_version_id UUID, 
	created_by_membership_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	archived_at TIMESTAMP WITH TIME ZONE, 
	archived_by_membership_id UUID, 
	archive_reason TEXT, 
	CONSTRAINT pk_scripts PRIMARY KEY (id), 
	CONSTRAINT fk_scripts_hospital FOREIGN KEY(hospital_id) REFERENCES hospitals (id), 
	CONSTRAINT fk_scripts_created_by FOREIGN KEY(hospital_id, created_by_membership_id) REFERENCES hospital_memberships (hospital_id, id), 
	CONSTRAINT fk_scripts_speaker FOREIGN KEY(hospital_id, speaker_membership_id) REFERENCES hospital_memberships (hospital_id, id), 
	CONSTRAINT uq_scripts_hospital_id_id UNIQUE (hospital_id, id), 
	CONSTRAINT ck_scripts_status CHECK (status IN ('draft','in_review','approved','archived'))
);
CREATE INDEX ix_scripts_hospital_topic ON scripts (hospital_id, topic);

CREATE TABLE source_versions (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	source_id UUID NOT NULL, 
	checksum TEXT NOT NULL, 
	content_addressed_key TEXT NOT NULL, 
	extractor_version TEXT, 
	mime TEXT, 
	size_bytes BIGINT, 
	page_count INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_source_versions PRIMARY KEY (id), 
	CONSTRAINT fk_source_versions_source FOREIGN KEY(hospital_id, source_id) REFERENCES sources (hospital_id, id), 
	CONSTRAINT uq_source_versions_checksum UNIQUE (hospital_id, source_id, checksum), 
	CONSTRAINT uq_source_versions_hospital_id_id UNIQUE (hospital_id, id), 
	CONSTRAINT ck_source_versions_key_nonempty CHECK (content_addressed_key <> '')
);

CREATE TABLE script_versions (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	script_id UUID NOT NULL, 
	parent_version_id UUID, 
	version_no INTEGER NOT NULL, 
	source TEXT NOT NULL, 
	creation_reason TEXT, 
	source_package_hash TEXT, 
	created_by_membership_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_script_versions PRIMARY KEY (id), 
	CONSTRAINT fk_versions_script FOREIGN KEY(hospital_id, script_id) REFERENCES scripts (hospital_id, id), 
	CONSTRAINT fk_versions_parent FOREIGN KEY(hospital_id, script_id, parent_version_id) REFERENCES script_versions (hospital_id, script_id, id), 
	CONSTRAINT fk_versions_created_by FOREIGN KEY(hospital_id, created_by_membership_id) REFERENCES hospital_memberships (hospital_id, id), 
	CONSTRAINT uq_versions_script_versionno UNIQUE (hospital_id, script_id, version_no), 
	CONSTRAINT uq_versions_hospital_script_id UNIQUE (hospital_id, script_id, id), 
	CONSTRAINT uq_script_versions_hospital_id_id UNIQUE (hospital_id, id), 
	CONSTRAINT ck_script_versions_source CHECK (source IN ('ai','editor','migration'))
);
CREATE INDEX ix_versions_parent ON script_versions (hospital_id, parent_version_id);
CREATE INDEX ix_versions_script ON script_versions (hospital_id, script_id);

CREATE TABLE edits (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	script_id UUID NOT NULL, 
	from_version_id UUID, 
	to_version_id UUID NOT NULL, 
	stable_block_key TEXT NOT NULL, 
	before_text TEXT, 
	after_text TEXT, 
	category TEXT NOT NULL, 
	reason TEXT, 
	scope TEXT DEFAULT 'hospital' NOT NULL, 
	approved_for_learning BOOLEAN DEFAULT false NOT NULL, 
	created_by_membership_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_edits PRIMARY KEY (id), 
	CONSTRAINT fk_edits_script FOREIGN KEY(hospital_id, script_id) REFERENCES scripts (hospital_id, id), 
	CONSTRAINT fk_edits_from_version FOREIGN KEY(hospital_id, script_id, from_version_id) REFERENCES script_versions (hospital_id, script_id, id), 
	CONSTRAINT fk_edits_to_version FOREIGN KEY(hospital_id, script_id, to_version_id) REFERENCES script_versions (hospital_id, script_id, id), 
	CONSTRAINT fk_edits_created_by FOREIGN KEY(hospital_id, created_by_membership_id) REFERENCES hospital_memberships (hospital_id, id), 
	CONSTRAINT uq_edits_hospital_id_id UNIQUE (hospital_id, id), 
	CONSTRAINT ck_edits_category CHECK (category IN ('tone','awkwardness','factual','source_grounding','transition','disclaimer','intro','cta','other')), 
	CONSTRAINT ck_edits_scope CHECK (scope IN ('global','hospital','doctor','topic'))
);
CREATE INDEX ix_edits_category ON edits (hospital_id, category);
CREATE INDEX ix_edits_script ON edits (hospital_id, script_id);

CREATE TABLE review_links (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	version_id UUID NOT NULL, 
	token_hash BYTEA NOT NULL, 
	reviewer_name TEXT, 
	permission TEXT DEFAULT 'comment_only' NOT NULL, 
	created_by_membership_id UUID, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_review_links PRIMARY KEY (id), 
	CONSTRAINT fk_review_links_version FOREIGN KEY(hospital_id, version_id) REFERENCES script_versions (hospital_id, id), 
	CONSTRAINT fk_review_links_created_by FOREIGN KEY(hospital_id, created_by_membership_id) REFERENCES hospital_memberships (hospital_id, id), 
	CONSTRAINT uq_review_links_token UNIQUE (token_hash), 
	CONSTRAINT uq_review_links_hospital_version_id UNIQUE (hospital_id, version_id, id), 
	CONSTRAINT uq_review_links_hospital_id_id UNIQUE (hospital_id, id), 
	CONSTRAINT ck_review_links_permission CHECK (permission IN ('comment_only','approve'))
);
CREATE INDEX ix_review_links_version ON review_links (hospital_id, version_id);

CREATE TABLE script_blocks (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	version_id UUID NOT NULL, 
	stable_block_key TEXT NOT NULL, 
	order_index INTEGER NOT NULL, 
	block_type TEXT NOT NULL, 
	scene TEXT, 
	text TEXT NOT NULL, 
	tc_start TEXT, 
	tc_end TEXT, 
	tc_start_ms BIGINT, 
	tc_end_ms BIGINT, 
	metadata JSONB DEFAULT '{}'::jsonb NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_script_blocks PRIMARY KEY (id), 
	CONSTRAINT fk_blocks_version FOREIGN KEY(hospital_id, version_id) REFERENCES script_versions (hospital_id, id), 
	CONSTRAINT uq_blocks_version_order UNIQUE (hospital_id, version_id, order_index), 
	CONSTRAINT uq_blocks_version_key UNIQUE (hospital_id, version_id, stable_block_key), 
	CONSTRAINT uq_blocks_hospital_version_id UNIQUE (hospital_id, version_id, id), 
	CONSTRAINT ck_script_blocks_block_type CHECK (block_type IN ('intro','explanation','evidence','transition','analogy','example','summary','cta','other')), 
	CONSTRAINT ck_script_blocks_tc_ms_range CHECK (tc_start_ms IS NULL OR tc_end_ms IS NULL OR (tc_start_ms >= 0 AND tc_end_ms >= tc_start_ms))
);
CREATE INDEX ix_blocks_version ON script_blocks (hospital_id, version_id);

CREATE TABLE version_approval_states (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	version_id UUID NOT NULL, 
	status TEXT DEFAULT 'none' NOT NULL, 
	approver_membership_id UUID, 
	assessment_set_hash TEXT, 
	version_content_hash TEXT, 
	compliance_policy_version TEXT, 
	decided_at TIMESTAMP WITH TIME ZONE, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_version_approval_states PRIMARY KEY (id), 
	CONSTRAINT fk_approval_states_version FOREIGN KEY(hospital_id, version_id) REFERENCES script_versions (hospital_id, id), 
	CONSTRAINT fk_approval_states_approver FOREIGN KEY(hospital_id, approver_membership_id) REFERENCES hospital_memberships (hospital_id, id), 
	CONSTRAINT uq_approval_states_version UNIQUE (hospital_id, version_id), 
	CONSTRAINT uq_version_approval_states_hospital_id_id UNIQUE (hospital_id, id), 
	CONSTRAINT ck_version_approval_states_status CHECK (status IN ('none','approved','rejected')), 
	CONSTRAINT ck_version_approval_states_approved_fields CHECK (status <> 'approved' OR (approver_membership_id IS NOT NULL AND assessment_set_hash IS NOT NULL AND version_content_hash IS NOT NULL AND compliance_policy_version IS NOT NULL AND decided_at IS NOT NULL)), 
	CONSTRAINT ck_version_approval_states_none_fields CHECK (status <> 'none' OR (approver_membership_id IS NULL AND assessment_set_hash IS NULL AND version_content_hash IS NULL AND compliance_policy_version IS NULL AND decided_at IS NULL)), 
	CONSTRAINT ck_version_approval_states_rejected_fields CHECK (status <> 'rejected' OR (approver_membership_id IS NOT NULL AND decided_at IS NOT NULL))
);

CREATE TABLE review_comments (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	version_id UUID NOT NULL, 
	block_id UUID, 
	anchor_start INTEGER, 
	anchor_end INTEGER, 
	review_link_id UUID, 
	author_membership_id UUID, 
	reviewer_name TEXT NOT NULL, 
	comment TEXT NOT NULL, 
	status TEXT DEFAULT 'open' NOT NULL, 
	resolved_by_membership_id UUID, 
	resolved_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_review_comments PRIMARY KEY (id), 
	CONSTRAINT fk_comments_version FOREIGN KEY(hospital_id, version_id) REFERENCES script_versions (hospital_id, id), 
	CONSTRAINT fk_comments_block FOREIGN KEY(hospital_id, version_id, block_id) REFERENCES script_blocks (hospital_id, version_id, id), 
	CONSTRAINT fk_comments_link FOREIGN KEY(hospital_id, version_id, review_link_id) REFERENCES review_links (hospital_id, version_id, id), 
	CONSTRAINT fk_comments_author FOREIGN KEY(hospital_id, author_membership_id) REFERENCES hospital_memberships (hospital_id, id), 
	CONSTRAINT uq_review_comments_hospital_id_id UNIQUE (hospital_id, id), 
	CONSTRAINT ck_review_comments_status CHECK (status IN ('open','accepted','rejected','resolved','needs_discussion')), 
	CONSTRAINT ck_review_comments_anchor_needs_block CHECK ((block_id IS NOT NULL) OR (anchor_start IS NULL AND anchor_end IS NULL)), 
	CONSTRAINT ck_review_comments_anchor_range CHECK ((anchor_start IS NULL AND anchor_end IS NULL) OR (anchor_start >= 0 AND anchor_end > anchor_start)), 
	CONSTRAINT ck_review_comments_comment_origin CHECK (author_membership_id IS NOT NULL OR review_link_id IS NOT NULL)
);
CREATE INDEX ix_comments_status ON review_comments (hospital_id, status);
CREATE INDEX ix_comments_version ON review_comments (hospital_id, version_id);
CREATE INDEX ix_comments_block ON review_comments (hospital_id, block_id);

CREATE TABLE review_sessions (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	review_link_id UUID NOT NULL, 
	session_token_hash BYTEA NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	last_seen_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_review_sessions PRIMARY KEY (id), 
	CONSTRAINT fk_review_sessions_link FOREIGN KEY(hospital_id, review_link_id) REFERENCES review_links (hospital_id, id), 
	CONSTRAINT uq_review_sessions_token UNIQUE (session_token_hash), 
	CONSTRAINT uq_review_sessions_hospital_id_id UNIQUE (hospital_id, id)
);
CREATE INDEX ix_review_sessions_link ON review_sessions (hospital_id, review_link_id);

CREATE TABLE script_sentences (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	version_id UUID NOT NULL, 
	block_id UUID NOT NULL, 
	sentence_index INTEGER NOT NULL, 
	text TEXT NOT NULL, 
	start_offset INTEGER NOT NULL, 
	end_offset INTEGER NOT NULL, 
	offset_unit TEXT NOT NULL, 
	segmenter_version TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_script_sentences PRIMARY KEY (id), 
	CONSTRAINT fk_sentences_block FOREIGN KEY(hospital_id, version_id, block_id) REFERENCES script_blocks (hospital_id, version_id, id), 
	CONSTRAINT uq_sentences_block_idx UNIQUE (hospital_id, version_id, block_id, sentence_index), 
	CONSTRAINT uq_sentences_hospital_version_id UNIQUE (hospital_id, version_id, id), 
	CONSTRAINT ck_script_sentences_offset_unit CHECK (offset_unit IN ('utf16','codepoint')), 
	CONSTRAINT ck_script_sentences_offset_range CHECK (start_offset >= 0 AND end_offset > start_offset)
);
CREATE INDEX ix_sentences_block ON script_sentences (hospital_id, version_id, block_id);

CREATE TABLE style_rule_sources (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	style_rule_id UUID NOT NULL, 
	origin_hospital_id UUID NOT NULL, 
	edit_id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_style_rule_sources PRIMARY KEY (id), 
	CONSTRAINT fk_srs_rule FOREIGN KEY(style_rule_id) REFERENCES style_rules (id), 
	CONSTRAINT fk_srs_edit FOREIGN KEY(origin_hospital_id, edit_id) REFERENCES edits (hospital_id, id), 
	CONSTRAINT uq_srs_rule_edit UNIQUE (style_rule_id, edit_id)
);

CREATE TABLE claims (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	version_id UUID NOT NULL, 
	sentence_id UUID NOT NULL, 
	claim_index INTEGER DEFAULT 0 NOT NULL, 
	claim_text TEXT NOT NULL, 
	claim_type TEXT NOT NULL, 
	detection_method TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_claims PRIMARY KEY (id), 
	CONSTRAINT fk_claims_sentence FOREIGN KEY(hospital_id, version_id, sentence_id) REFERENCES script_sentences (hospital_id, version_id, id), 
	CONSTRAINT uq_claims_sentence_idx UNIQUE (hospital_id, sentence_id, claim_index), 
	CONSTRAINT uq_claims_hospital_id_id UNIQUE (hospital_id, id), 
	CONSTRAINT ck_claims_claim_type CHECK (claim_type IN ('term_definition','cause','mechanism','treatment_effect','test_interpretation','statistic','study_result','association','patient_judgment','other')), 
	CONSTRAINT ck_claims_detection_method CHECK (detection_method IN ('llm','regex','migration'))
);
CREATE INDEX ix_claims_version ON claims (hospital_id, version_id);

CREATE TABLE claim_assessments (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	claim_id UUID NOT NULL, 
	assessment_kind TEXT NOT NULL, 
	idempotency_key TEXT NOT NULL, 
	supersedes_assessment_id UUID, 
	checker_version TEXT, 
	model TEXT, 
	prompt_hash TEXT, 
	source_set_hash TEXT, 
	support_level TEXT NOT NULL, 
	verification_status TEXT NOT NULL, 
	medical_risk TEXT NOT NULL, 
	rationale TEXT, 
	created_by_membership_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_claim_assessments PRIMARY KEY (id), 
	CONSTRAINT fk_assessments_claim FOREIGN KEY(hospital_id, claim_id) REFERENCES claims (hospital_id, id), 
	CONSTRAINT fk_assessments_supersedes FOREIGN KEY(hospital_id, supersedes_assessment_id) REFERENCES claim_assessments (hospital_id, id), 
	CONSTRAINT fk_assessments_created_by FOREIGN KEY(hospital_id, created_by_membership_id) REFERENCES hospital_memberships (hospital_id, id), 
	CONSTRAINT uq_assessments_idempotency UNIQUE (hospital_id, claim_id, idempotency_key), 
	CONSTRAINT uq_claim_assessments_hospital_id_id UNIQUE (hospital_id, id), 
	CONSTRAINT ck_claim_assessments_assessment_kind CHECK (assessment_kind IN ('automated','human_review','override','migration')), 
	CONSTRAINT ck_claim_assessments_support_level CHECK (support_level IN ('direct','partial','inferred','unsupported','unverified')), 
	CONSTRAINT ck_claim_assessments_verification_status CHECK (verification_status IN ('pending','verified','failed')), 
	CONSTRAINT ck_claim_assessments_medical_risk CHECK (medical_risk IN ('low','medium','high')), 
	CONSTRAINT ck_claim_assessments_human_actor_required CHECK (assessment_kind NOT IN ('human_review','override') OR created_by_membership_id IS NOT NULL)
);
CREATE INDEX ix_assessments_latest ON claim_assessments (hospital_id, claim_id, created_at DESC, id DESC);

CREATE TABLE claim_sources (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	hospital_id UUID NOT NULL, 
	claim_id UUID NOT NULL, 
	source_version_id UUID NOT NULL, 
	source_quote TEXT, 
	page_or_location TEXT, 
	span_hash TEXT DEFAULT '' NOT NULL, 
	relation_type TEXT NOT NULL, 
	confidence NUMERIC(5, 4), 
	verified_by_membership_id UUID, 
	verified_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_claim_sources PRIMARY KEY (id), 
	CONSTRAINT fk_claim_sources_claim FOREIGN KEY(hospital_id, claim_id) REFERENCES claims (hospital_id, id), 
	CONSTRAINT fk_claim_sources_sourceversion FOREIGN KEY(hospital_id, source_version_id) REFERENCES source_versions (hospital_id, id), 
	CONSTRAINT fk_claim_sources_verified_by FOREIGN KEY(hospital_id, verified_by_membership_id) REFERENCES hospital_memberships (hospital_id, id), 
	CONSTRAINT uq_claim_sources_span UNIQUE (hospital_id, claim_id, source_version_id, span_hash), 
	CONSTRAINT uq_claim_sources_hospital_id_id UNIQUE (hospital_id, id), 
	CONSTRAINT ck_claim_sources_relation_type CHECK (relation_type IN ('directly_supports','partially_supports','contradicts','context_only')), 
	CONSTRAINT ck_claim_sources_confidence_range CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);

