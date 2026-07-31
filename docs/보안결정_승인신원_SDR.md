# 보안 결정 기록 (SDR) — 승인 신원 신뢰경계

상태: **승인(Accepted)** · 결정일: 2026-08-01 · 대상: `fn_approve_version`의 `app.membership_id` GUC 신뢰

## 결정

`fn_approve_version`이 세션 GUC(`app.membership_id`)를 승인자 신원으로 신뢰하는 문제는,
**`app_rw`를 서버 전용 신뢰 DB role로 취급하는 앱계층 신뢰경계**로 **수용(accepted risk)**한다.
(대안인 별도 승인 서비스/서명 토큰, per-user DB role은 현 규모에 과함 — 아래 재검토 조건 참고.)

이는 "DB가 사용자를 암호학적으로 인증한다"는 뜻이 아니라 다음 위험을 **명시적으로 받아들이는 결정**이다.

> 서버 코드 또는 `app_rw` 자격증명이 침해되면 다른 membership으로 GUC를 설정해 승인자를 사칭할 수 있다.
> DB 함수의 역할 검사는 외부 공격자에 대한 독립 인증 경계가 아니라, 신뢰된 서버 내부의 실수와 비정상 호출을 줄이는 방어선이다.

## 고정 조건 (반드시 준수)

```
[승인 신원 신뢰경계 결정]
현재 단계에서는 app_rw를 서버 전용 신뢰 DB role로 취급한다.

- app_rw credential은 백엔드 서버와 승인된 배포 환경에서만 보유한다.
- 브라우저·모바일 앱·클라이언트에 DATABASE_URL 또는 app_rw 자격증명을 노출하지 않는다.
- 임의 SQL 실행 API, 사용자 입력 SQL, 디버그 SQL 엔드포인트를 제공하지 않는다.
- app.membership_id는 인증 완료 후 서버가 조회한 membership_id만 SET LOCAL로 설정한다.
- 요청 body, URL, 클라이언트 claim에서 받은 membership_id를 그대로 설정하지 않는다.
- membership이 현재 hospital에 속하고 archived_at IS NULL인지 승인 직전에 재검증한다.
- approver/admin 역할을 승인 직전에 재검증한다.
- membership_roles와 hospital_memberships의 직접 쓰기는 app_rw에서 금지한다.
- 승인 요청에는 인증 user_id, membership_id, hospital_id, request_id를 감사 로그에 기록한다.
- app_rw credential 노출 또는 서버 침해 시 승인자 사칭이 가능하다는 잔여 위험을 수용한다.
- fn_approve_version은 애플리케이션에서 직접 raw SQL로 호출하지 않고, repositories.approve_version 경로로만 호출한다(advisory lock 하 hash 계산 보장).
- 승인 HTTP endpoint 활성화 전 인증 user_id·request_id를 audit_events에 배선한다(현재는 hospital·actor·action·version·hash만 기록).

별도 승인 서비스·서명 토큰은 다음 조건에서 재검토한다.
- 외부 고객이 직접 DB에 접근하는 구조
- 여러 비신뢰 서비스가 동일 app_rw credential을 공유
- 법적·감사상 승인자의 독립적인 부인방지가 필요
- 승인 기능을 별도 보안 경계로 분리해야 하는 운영 요구 발생
```

## 코드에서 이미 강제되는 방어선 (v3, 실 PG 49 tests)
- 승인 직전 **active membership + approver/admin** 재검증: `fn_approve_version`이 `hospital_memberships.archived_at IS NULL` JOIN.
- **역할 자가부여 차단**: `membership_roles`·`hospital_memberships` 직접 쓰기 app_rw REVOKE.
- **승인 감사**: `audit_events`에 approval.approve + actor + hash 기록(승인 함수와 동일 트랜잭션).

## ⚠️ 이 SDR로 닫히는 범위 (사용자 명시)
이 결정은 **`app.membership_id` 신뢰 문제만** accepted-risk로 닫는다. 나머지는 **별도로 이미 해결**됨:

| GPT 지적 | 상태 |
|---|---|
| #2 승인자 GUC 신뢰 | **본 SDR로 accepted-risk 문서화** |
| review_links 직접 DML 우회 | ✅ v3: INSERT/UPDATE/DELETE REVOKE, 함수(fn_create/revoke)로만 |
| active membership 검사 | ✅ v3: archived 차단(테스트) |
| 승인·콘텐츠·assessment 동시성 race | ✅ v3: version advisory lock 직렬화(실 lock중첩 대기 테스트) |
| 승인 hash 검증(TOCTOU) | ✅ v3: hash를 version lock 하에 계산 |
| no-op content / 빈 버전 | ✅ v3: 블록수>0 검증 |
| RLS 정책 멱등 | ✅ v3: vas 정책 DROP 후 재생성 + apply 멱등 테스트 |
