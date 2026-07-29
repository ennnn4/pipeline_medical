# boncure-pipeline

병원 유튜브 콘텐츠 제작 자동화 파이프라인. "자료 넣기 → KB → 대본 패키지 → 대시보드"를 한 번에 돌린다.
오늘 손으로 하던 작업을 코드로 옮긴 것. 결정론적 단계(수집·컴플라이언스·렌더)는 바로 실행되고, LLM 단계(분류·KB·대본)는 Anthropic API 키만 있으면 돈다.

## 설계 원칙
- **자동화 = 초안·데이터·렌더링까지.** 의학 검증·원장 검수·의료광고 심의 사인오프는 **사람**이 한다(자동 발행 금지).
- 병원별 설정은 `config/<hospital>.yaml` 하나로.

## 7단계
| # | 단계 | 모듈 | 자동화 |
|---|---|---|---|
| 1 | 수집·정규화 | `ingest/extract.py`, `ingest/youtube.py` | 🟢 스크립트 |
| 2 | 분류·색인 | `llm/prompts/classify.md` + `run.py` | 🟡 LLM |
| 3 | KB 생성 | `llm/prompts/{profile,disease,evidence,competitor}.md` | 🟡 LLM+사람검수 |
| 4 | 대본 패키지 | `llm/prompts/director.md` | 🟡 LLM |
| 5 | 컴플라이언스 게이트 | `compliance/rules.py` (+ `compliance.md`) | 🟢 규칙+LLM |
| 6 | 대시보드 렌더 | `render/render.py`, `render/frames.py` | 🟢 결정론 |
| 7 | 검수·발행 | (사람) | 🔴 사람만 |

## 빠른 시작
```bash
pip install -r requirements.txt

# 1) 자료를 data/raw/ 에 넣는다 (pdf/docx/hwp/txt/zip) + config에 유튜브 링크
# 2) 수집·정규화 (LLM 불필요)
python run.py ingest --hospital boncure

# 3) 컴플라이언스만 검사 (LLM 불필요) — 기존 패키지 md 검사 가능
python run.py compliance --file ../output/패키지/이명편_풀패키지_v1.md

# 4) 대시보드 렌더 (LLM 불필요) — kb json → html
python run.py render --hospital boncure

# 5) 전체 (LLM 필요: 분류·KB·대본)
export ANTHROPIC_API_KEY=...   # 또는  ant auth login
python run.py all --hospital boncure --topic 이명
```

## 모델
LLM 러너 기본 모델 `claude-opus-5` (adaptive thinking, effort high, 긴 출력은 streaming). `llm/runner.py`에서 변경.

## 사람이 잡는 게이트 (자동화 금지)
- 논문 근거 팩트체크 최종 확인, 원장 의학 검수(숙지 질문), 의료광고 사전심의, 발행. `compliance/rules.py`는 자동 1차 필터일 뿐 통과 보장 아님.
