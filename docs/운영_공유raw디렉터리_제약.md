# 운영 제약 — 공유 raw 디렉터리 (job별 격리 도입 전까지 유효)

**결정 상태**: 현행 유지. job별 디렉터리 격리(Step 7–9와 별개의 저장소 개선)를 **도입하기 전까지는** 아래 불변식을 반드시 지킨다. heartbeat가 있어도 이 정책은 자동으로 대체되지 않는다.

## 왜 제약이 필요한가

생성 파이프라인은 병원별 단일 작업 디렉터리 `data/<h>/raw/`를 공유한다(`app.py::_run_pipeline` → `_clear_raw_dir(data_dir(h,"raw"))` → `materialize_job_snapshot(...)`). **job별 하위 디렉터리가 아니다.** 따라서 같은 병원에서 두 job이 동시에 실행되면 서로의 raw/ 를 덮어써 스냅샷–실행 정합이 깨진다.

## 불변식 (모두 만족해야 함)

1. **병원당 active job 1개** — DB 부분 유니크 인덱스 `uq_genjobs_one_active ON generation_jobs(hospital_id) WHERE status IN (pending,generating,ingesting,generated)`가 강제. 두 번째 실행권 획득은 `claim_job` → `hospital_busy`로 거부(`app.py`가 "다른 대본을 생성 중"으로 안내).
2. **정상 워커 heartbeat 살아있는 동안 reap 금지** — `reap_stale`은 `coalesce(heartbeat_at,started_at,created_at) < now()-1800s`인 job만 stale 처리. 실행 중 워커는 30초마다 `heartbeat_job`(worker_token 일치 시)으로 heartbeat_at 갱신 → 살아있는 워커는 회수되지 않음.
3. **병원 내 병렬 실행 금지** — 위 (1)로 애초에 두 번째 job이 generating에 못 들어감. UI/스케줄러에서 같은 병원 병렬 트리거를 열지 않는다.
4. **lease 강제 탈취·병렬 실행을 job-dir 이전에는 열지 않는다** — stale 회수는 worker_token을 NULL로 무효화해 늦은 워커의 상태전이·적재를 차단(`mark_job`/`heartbeat_job`/`ingest`가 worker_token 일치를 요구)하지만, 이는 "죽은 것으로 판정된 job의 잔여 쓰기 차단"이지 "살아있는 job을 뺏어 병렬 실행"이 아니다. 후자는 job별 디렉터리 없이는 금지.

## 강제 지점 (코드)

| 불변식 | 강제 위치 |
|---|---|
| active 1개 유니크 | `store/ingest.py` `uq_genjobs_one_active` + `claim_job` (CAS pending→generating) |
| heartbeat 갱신 | `app.py::_run_pipeline` 스트리밍 루프 30초 주기 → `ingest.heartbeat_job(worker_token)` |
| stale 판정 임계 | `ingest.reap_stale(older_than_sec=1800)` (heartbeat 없는 잔여만) |
| 늦은 워커 차단 | `mark_job`/`heartbeat_job`이 `worker_token=:wt` 조건 요구; `reap_stale`이 회수 시 worker_token=NULL |
| raw/ 복원 원자성 | `materialize_job_snapshot` checksum·size 검증 후 atomic rename |

## 해제 조건 (이 문서를 폐기해도 되는 시점)

`data/<h>/<job_id>/raw/` 처럼 **job별 디렉터리 격리**가 도입되면 (1)(3)(4)의 "병원당 1개·병렬 금지" 제약을 완화할 수 있다. 그 전에는 병원당 동시 실행 확장(멀티 워커·재실행 병렬)을 도입하지 말 것.
