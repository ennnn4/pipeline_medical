# 기술부채 milestone — 물리 통합 (Step 7–9)

**상태**: 등록됨(미착수). **긴급도**: 낮음 — UX·데이터 무결성은 이미 통합돼 사용자 체감 문제 없음.
GPT 판정: "물리 통합은 도메인 통합이 아니라 운영·구조 개선. 안정 운영 후 별도 집중 작업/브랜치로." 무기한 미루지 않도록 여기 명시 등록.

## 배경 (지금 상태)
- 도메인·읽기 로직 service화 **완료** — mutation·권한·상태전이·승인게이트·export·이미지·읽기 workspace 모두 공통 service/DB 함수. `/studio`는 업무규칙을 소유하지 않는 프레젠테이션·호환 계층.
- 남은 건 **두 Flask 앱의 물리적 통합**뿐(DispatcherMiddleware 제거, /studio 은퇴). 회귀 표면은 넓고 신규 기능 이득은 적음 → 별도 milestone.

## 착수 전 선행(운영 마감) — 완료
- [x] Bootstrap 회귀 관문(fresh chain parity) — `tests/test_bootstrap_parity.py`(영구 관문).
- [x] 공유 raw 디렉터리 제약 문서화 — `docs/운영_공유raw디렉터리_제약.md`.
- [x] 경량 관측성 — `services/observability.py`(service_error·sqlstate·http/studio·reap_stale). redirect 후 놓친 legacy 호출을 로그 집계로 추적 가능.

## 구현 방식 (GPT 권장: 공유 presentation 경유 후 대시보드 소유로 수렴)
UI를 대시보드에 복붙(중복·불일치) 금지, 두 앱이 render helper를 영구 공유(제거 어려운 추상화)도 금지. 전환용 하이브리드:

```
클로저 내부 렌더링 제거 → 중립 templates/macros/static 추출
→ /studio가 먼저 새 presentation 사용 → 대시보드가 같은 presentation 사용
→ parity 확인 → 대시보드가 canonical owner → /studio redirect → studio app 제거
```

### 추출 대상 (web/api.py create_app 클로저)
`_page` · `_evidence_panel` · `_images_panel` · `_CSS` · `_u` · `_csrf_field`
→ `templates/scripts/{workspace,edit,versions,_evidence_panel,_images_panel,_approval_panel}.html`,
  `templates/macros/{forms,status,navigation}.html`, `static/css/workspace.css`, `static/js/workspace.js`,
  `presentation/{workspace_viewmodel,route_urls,formatters}.py`

### 공유 O / 공유 X
- 공유: Jinja template·macro·CSS·작은 formatter·workspace DTO→context 변환·상태배지/버튼 계산·CSRF field macro.
- 공유 X: Flask `request`/`session`/`redirect`·실제 route handler·DB 연결·service 호출·권한/mutation 최종 보안 판정.
- **URL은 service가 반환하지 않는다**(Flask route 종속 방지). 각 앱 route adapter가 `WorkspaceRouteUrls(edit=url_for(...), ...)`로 template에 주입. 전환 초기엔 /studio URL, 대시보드 route 준비되면 canonical dashboard URL.

## 단계
- **7A 추출**: `_CSS`→정적, panel/approval→Jinja partial/macro, `_page`→base/workspace template, `_csrf_field`→macro/context processor, `_u`→URL adapter, /studio가 새 template 렌더, HTML snapshot 테스트. **URL 불변**(화면만 클로저 밖으로).
- **7B 대시보드 route**: `/scripts/<id>/{versions,versions/<vid>,edit,evidence,approval,images}` — 각 route는 request parsing → `ActorContext.resolve` → workspace/query service → 공유 template만. 쓰기 route도 기존 service 그대로 호출.
- **8 parity**: block/claim/image 수·effective assessment·approval 상태·available action·stale 배지·CSRF·승인/반려/철회·export·이미지 재생성·타테넌트 접근·stale version 제출·예외→HTTP 매핑·audit. **redirect destination·flash message까지** 비교(DB 결과만 아님).
- **9 은퇴**: 내부 링크 대시보드 route로 변경 → /studio GET deep link 302 redirect → 기존 POST/API는 즉시 302 금지(method·body 손실) service 기반 thin compatibility wrapper 유지 → legacy endpoint 호출량(관측) 관찰 → 0되면 write endpoint 종료 → DispatcherMiddleware 제거 → studio app 제거 → 잔여 template/static 중복 제거.

## 함께 정리(용량 추세 따라)
- 이미지 bytea → R2/S3 (총량·증가량은 관측 대상).
