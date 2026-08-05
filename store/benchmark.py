"""유튜브 벤치마킹 → 기획 → 대본 데이터 모델 (Phase 1, 링크 기반 MVP).

원칙(개발지침):
 - 전부 additive. 기존 script/version/approval/RLS 무손상.
 - 병원별 멀티테넌트 RLS(app_rw = hospital_id GUC 일치, app_owner full).
 - 원본 자막 ≠ 분석결과 (필드/테이블 분리). 대용량 원본은 R2(object_assets), PG엔 정규화 텍스트·메타만.
 - 유튜브 의학주장은 evidence 자동승격 금지 → yt_claim_candidates.status=pending_verification.
 - 모든 주요 artifact에 provenance(model·prompt/policy version·content_hash·status·created_by·created_at).

테이블:
  benchmark_projects   프로젝트(영상 1~5 묶음 → 기획 → 대본)
  youtube_videos       영상 metadata(Data API, 누락필드 nullable)
  youtube_transcripts  자막(provider·status·정규화텍스트 or R2 object_key)
  benchmark_analyses   영상별 구조화 분석(jsonb)
  cross_syntheses      교차분석(jsonb)
  yt_claim_candidates  유튜브 의학주장 후보(검증 전 pending)
  content_plans        기획안 artifact(승인 상태 — 기존 script 승인과 분리)
  similarity_reports   원본 유사도 검사(문장·의미·사례·구조)
"""
from sqlalchemy import text

_TENANT_SET = "NULLIF(current_setting('app.hospital_id', true), '')::uuid"

def _policies(tbl, prefix):
    """표준 RLS 정책(app_rw 테넌트 격리 + app_owner full)."""
    return [
        f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;",
        f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY;",
        f"DROP POLICY IF EXISTS {prefix}_rw ON {tbl};",
        f"CREATE POLICY {prefix}_rw ON {tbl} TO app_rw "
        f"USING (hospital_id = {_TENANT_SET}) WITH CHECK (hospital_id = {_TENANT_SET});",
        f"DROP POLICY IF EXISTS {prefix}_def ON {tbl};",
        f"CREATE POLICY {prefix}_def ON {tbl} TO app_owner USING (true) WITH CHECK (true);",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO app_rw;",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO app_owner;",
    ]

_DDL = [
"""CREATE TABLE IF NOT EXISTS benchmark_projects (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hospital_id uuid NOT NULL REFERENCES hospitals(id),
  title text NOT NULL,
  status text NOT NULL DEFAULT 'draft',      -- draft|analyzing|planned|scripted|archived
  created_by_membership_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_bp_status CHECK (status IN ('draft','analyzing','planned','scripted','archived'))
);""",

"""CREATE TABLE IF NOT EXISTS youtube_videos (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hospital_id uuid NOT NULL REFERENCES hospitals(id),
  project_id uuid NOT NULL REFERENCES benchmark_projects(id) ON DELETE CASCADE,
  video_id text,                             -- 유튜브 video id(nullable: URL만 있을 수도)
  url text NOT NULL,
  title text, description text, thumbnail_url text,
  channel_id text, channel_name text,
  published_at timestamptz,
  view_count bigint, like_count bigint, comment_count bigint, subscriber_count bigint,
  caption_status text,                       -- available|none|unknown
  metadata_fetched_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, url)
);""",

"""CREATE TABLE IF NOT EXISTS youtube_transcripts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hospital_id uuid NOT NULL REFERENCES hospitals(id),
  video_ref uuid NOT NULL REFERENCES youtube_videos(id) ON DELETE CASCADE,
  provider text NOT NULL,                    -- external|manual|upload|stt
  status text NOT NULL DEFAULT 'pending',    -- pending|fetching|available|provider_failed|manual_required|completed
  lang text, has_timestamps boolean DEFAULT false,
  normalized_text text,                      -- 검색·분석용 정규화 텍스트(작으면 PG)
  object_key text,                           -- 대용량 원본(SRT/VTT/오디오)은 R2 object_assets 키
  source_note text,                          -- 출처·provider 기록(약관·저작권 추적)
  char_count int,
  fetched_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_yt_provider CHECK (provider IN ('external','manual','upload','stt')),
  CONSTRAINT ck_yt_status CHECK (status IN ('pending','fetching','available','provider_failed','manual_required','completed'))
);""",

"""CREATE TABLE IF NOT EXISTS benchmark_analyses (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hospital_id uuid NOT NULL REFERENCES hospitals(id),
  project_id uuid NOT NULL REFERENCES benchmark_projects(id) ON DELETE CASCADE,
  video_ref uuid NOT NULL REFERENCES youtube_videos(id) ON DELETE CASCADE,
  analysis jsonb NOT NULL,                   -- 영상별 구조화 분석(원본 자막과 분리)
  model text, prompt_version text, content_hash text,
  created_at timestamptz NOT NULL DEFAULT now()
);""",

"""CREATE TABLE IF NOT EXISTS cross_syntheses (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hospital_id uuid NOT NULL REFERENCES hospitals(id),
  project_id uuid NOT NULL REFERENCES benchmark_projects(id) ON DELETE CASCADE,
  synthesis jsonb NOT NULL,                  -- 교차분석(공통·차이·흥행요소·금지 고유표현·검증필요 주장)
  model text, prompt_version text, content_hash text,
  created_at timestamptz NOT NULL DEFAULT now()
);""",

"""CREATE TABLE IF NOT EXISTS yt_claim_candidates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hospital_id uuid NOT NULL REFERENCES hospitals(id),
  project_id uuid NOT NULL REFERENCES benchmark_projects(id) ON DELETE CASCADE,
  claim_text text NOT NULL, claim_type text,
  population text, condition text, intervention text, comparator text, outcome text,
  timeframe text, numeric_value text,
  source_video_ids jsonb,                    -- 어느 영상들에서 나온 주장인가
  status text NOT NULL DEFAULT 'pending_verification',  -- pending_verification|supported|limited|conflicting|unsupported|rejected
  linked_claim_card_id uuid,                 -- Phase 2: Claim Ledger 카드 연결(지금은 null)
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_ytc_status CHECK (status IN ('pending_verification','supported','limited','conflicting','unsupported','rejected'))
);""",

"""CREATE TABLE IF NOT EXISTS content_plans (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hospital_id uuid NOT NULL REFERENCES hospitals(id),
  project_id uuid NOT NULL REFERENCES benchmark_projects(id) ON DELETE CASCADE,
  plan jsonb NOT NULL,                       -- 기획안(주제·이유·타깃·승인주장·미검증주장·원장관점·순서·훅·CTA·유사도위험)
  status text NOT NULL DEFAULT 'draft',      -- draft|approved|rejected|superseded
  approved_by_membership_id uuid, approved_at timestamptz,
  script_version_id uuid,                    -- 이 기획으로 생성된 대본 버전(브릿지)
  model text, prompt_version text, content_hash text,
  created_by_membership_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_cp_status CHECK (status IN ('draft','approved','rejected','superseded'))
);""",

"""CREATE TABLE IF NOT EXISTS similarity_reports (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hospital_id uuid NOT NULL REFERENCES hospitals(id),
  project_id uuid NOT NULL REFERENCES benchmark_projects(id) ON DELETE CASCADE,
  script_version_id uuid,                    -- 검사한 대본 버전
  report jsonb NOT NULL,                     -- verbatim/semantic/example/structure overlap + risk + flagged
  risk text,                                 -- low|medium|high
  created_at timestamptz NOT NULL DEFAULT now()
);""",
]

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_ytv_project ON youtube_videos(project_id);",
    "CREATE INDEX IF NOT EXISTS ix_ytt_video ON youtube_transcripts(video_ref);",
    "CREATE INDEX IF NOT EXISTS ix_ba_project ON benchmark_analyses(project_id);",
    "CREATE INDEX IF NOT EXISTS ix_ytc_project ON yt_claim_candidates(project_id);",
    "CREATE INDEX IF NOT EXISTS ix_cp_project ON content_plans(project_id);",
    "CREATE INDEX IF NOT EXISTS ix_bp_hospital ON benchmark_projects(hospital_id);",
]

_TABLES = [
    ("benchmark_projects", "bp"), ("youtube_videos", "ytv"), ("youtube_transcripts", "ytt"),
    ("benchmark_analyses", "ba"), ("cross_syntheses", "cs"), ("yt_claim_candidates", "ytc"),
    ("content_plans", "cp"), ("similarity_reports", "sr"),
]

def ensure_benchmark_schema(owner_engine):
    """벤치마크 스키마 + RLS + 인덱스(멱등·비파괴). deploy_bootstrap에서 호출."""
    with owner_engine.begin() as cn:
        for ddl in _DDL:
            cn.execute(text(ddl))
        for ix in _INDEXES:
            cn.execute(text(ix))
        for tbl, prefix in _TABLES:
            for s in _policies(tbl, prefix):
                cn.execute(text(s))
