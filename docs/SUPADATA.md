# Supadata 자동 자막 수집 — 운영 문서

벤치마킹 영상의 자막을 **버튼 클릭 시** 자동 수집한다. 무료 계정은 **월간 credits**(영상 개수 아님) 기준으로 관리한다.

## 흐름
영상 등록 → (카드의) **🎬 자막 자동 가져오기** 클릭 → Supadata 호출 → 자막 저장(available) → **이 영상 분석** 활성화.
공개 자막이 없거나 실패/한도소진이면 **직접 입력/파일 업로드(다글로)**가 자동으로 펼쳐진다. 유튜브 자막은 evidence로 자동 승격하지 않는다(분석·근거검증은 기존 흐름).

## 환경변수 (Render)
| 변수 | 기본 | 설명 |
|---|---|---|
| `SUPADATA_API_KEY` | — | supadata.ai 발급 키. **서버에서만** 사용(로그·클라 노출 안 함) |
| `SUPADATA_PROVIDER_ENABLED` | false | `true`여야 자동수집 켜짐(키와 함께 둘 다 필요) |
| `SUPADATA_MONTHLY_CREDIT_LIMIT` | 100 | 월 한도(credits). 유료 업그레이드 시 **이 값만 변경**(재배포 불필요) |
| `SUPADATA_CREDIT_WARNING_THRESHOLD` | 80 | 경고 임계(observability 이벤트) |
| `SUPADATA_TRANSCRIPT_MODE` | native | `native`=공개자막만(무자막이면 수동), `auto`=없으면 AI전사(credits 급증 주의) |
| `SUPADATA_ALLOW_AI_GENERATION` | false | AI 전사 허용 여부(무료 절약 기본 false) |

무료 MVP 권장: `MODE=native`, `ALLOW_AI_GENERATION=false` → 공개자막 있는 영상만 자동, 없으면 다글로/수동.

## 크레딧
- 공개 자막 조회 성공: 보통 **1 credit**. 응답 헤더 `x-billable-requests`의 실제 소비값을 기록(없으면 보수적 추정, `estimated=true`).
- AI 전사(무자막): **영상 1분당 ~2 credits** → 10분 영상 ≈ 20 credits(무료 100이면 5편). 그래서 기본 native.
- 사용량은 **전역**(무료 계정 전체 공유)으로 집계. 병원별 100 아님.
- 이중집계 방지: `transcript_provider_usage` `unique(provider, request_id, operation)`.

## 상태
`pending / fetching / transcribing / available / provider_failed / manual_required / quota_exhausted / rate_limited / config_error`
카드 뱃지는 한글(자막 준비 완료 / 음성을 글로 변환 중 / 자동수집 실패 / 자동수집 한도 소진 …).

## 한도 소진 시
- 자동수집 차단 + "이번 달 한도 소진" 안내 + **관리자에게 문의** 버튼(중복 방지, `admin_requests`).
- **직접 붙여넣기·파일 업로드·기존 자막 재사용·분석은 계속 가능**(전체 장애 아님).
- 유료 업그레이드: Supadata에서 플랜 올린 뒤 `SUPADATA_MONTHLY_CREDIT_LIMIT` 값만 변경.

## 관리자 화면
홈(운영자) → **🎬 자동 자막 현황** (`/admin/transcripts`, platform_operator 전용):
활성 여부 · 이번 달 크레딧(사용/한도/남음/사용률 바) · 요청 집계(성공/실패/수동/소진) · 한도상향 문의 목록·처리.
전역 집계는 SECURITY DEFINER 함수(운영자 확인)로 RLS 우회 없이 조회.

## Observability (구조화 이벤트, 민감정보 없음)
`transcript_fetch_started/succeeded/failed`, `transcript_generation_started`, `transcript_quota_warning/exhausted`,
`transcript_manual_required`, `transcript_admin_request_created`. 필드: hospital hash·provider·mode·status·credits·
monthly_credits·failure_code·request_id. **API키·자막원문·병원명·원시오류 body는 로그에 안 남김.**

## 보안 경계
provider 호출은 서버에서만(브라우저에서 Supadata 직접 호출 안 함). ActorContext + 병원별 RLS 유지.
전역 quota/문의만 definer 함수(운영자 확인)로 처리.

## 테스트
`python -m pytest tests/test_supadata.py` (DB 불필요, mock 13건: HTTP 200/202/206/401/402/429 rate↔quota/5xx/network/크레딧/추정).
오케스트레이션·quota·문의·관리자 게이트는 라이브 DB로 검증(mock provider).

## 코드 맵
- `services/supadata.py` — provider + config + result + HTTP 매핑
- `services/transcript_auto.py` — 오케스트레이션(게이트·저장·usage·observability)
- `store/transcript_usage.py` — 사용량/문의 테이블 + 전역합계·관리자 definer 함수
- `app.py` — `bm_auto_transcript`(전용 버튼), `bm_quota_help`(문의), `/admin/transcripts`
- `store/benchmark.py` `_ALTERS` — youtube_transcripts 컬럼/CHECK 확장

## 남은 개선(선택)
- 202 async 폴링(지수백오프) — native 모드에선 거의 발생 안 함(AI 전사 시 필요)
- AI 전사 예산 게이트(`auto` 모드에서 estimated_credits > remaining면 시작 안 함)
