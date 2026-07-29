# 사용 가이드 — 새 병원 붙이고 대본 뽑기

팀용 실전 매뉴얼. 명령은 `boncure-pipeline/` 폴더 안에서 실행.

---

## 0. 처음 한 번만 (셋업)

```bash
pip install -r requirements.txt
```

- **API 키**: `.env` 파일에 `ANTHROPIC_API_KEY=sk-ant-...` 한 줄. (한 번 넣으면 계속 씀. `.gitignore` 처리돼 있어 깃엔 안 올라감)
- 키 발급: https://console.anthropic.com/settings/keys · 결제(크레딧): https://console.anthropic.com/settings/billing
- `pdftotext` 바이너리 필요(수집용). 없으면 설치.

---

## 1. 새 병원 만들기

```bash
python run.py init --hospital seoul-ortho
```

- `seoul-ortho` = 병원 식별자(영문·하이픈, 아무거나).
- 자동 생성: `config/seoul-ortho.yaml`(설정) + `data/seoul-ortho/{raw,corpus,kb,out}` 폴더.
- 실행하면 다음에 뭘 할지 안내가 뜸.

## 2. 설정 파일 채우기

`config/seoul-ortho.yaml` 열어서 빈칸 수정:

| 항목 | 예시 | 설명 |
|---|---|---|
| `name` | 서울정형외과 | 병원명 |
| `host` | 김철수 | 기본 화자(원장). 편마다 다를 수 있음 |
| `tagline` | "무릎이 편해야 인생이 걷습니다" | 대시보드 히어로 부제(슬로건) |
| `diseases` | `[오십견, 무릎관절염, 허리디스크]` | 이 병원 주력 질환(질환별 KB 생성 대상) |
| `input_checklist` | (진료과 맞춰) | 어떤 자료가 필수인지. 한방 아니면 조정 |
| `youtube.competitor_channels` | (선택) | 경쟁 채널 |

## 3. 자료 넣기

`data/seoul-ortho/raw/` 폴더에 그 병원 자료를 **그냥 다 넣는다**:
- 원장 설문지·인터뷰, 논문 PDF, 강의자료(PPT/PDF), 기존 영상 대본, 홈페이지·블로그 텍스트 등
- **zip 통째로** 넣어도 알아서 풀어서 읽음
- 지원: pdf · docx · txt · hwp(pyhwp 설치 시) · zip

## 4. 돌리기 (한 줄)

```bash
python run.py all --hospital seoul-ortho --topic 오십견
```

이 한 줄이 **수집 → 분류 → KB 생성 → 대본 → 컴플라이언스 → 대시보드**를 순서대로 실행.

⚠️ `all`은 LLM 호출이 여러 번(질환 수만큼)이라 비용이 좀 나옴. 처음 KB 만들 때만 `all`, 이후엔 아래 편별 흐름 사용.

---

## 나오는 것

- **시작하자마자**: 입력 자료 체크리스트 리포트 — 받은 것/빠진 **필수** 자료 표시 (예: `❌ 원장인터뷰(필수)`)
- **KB**: `data/seoul-ortho/kb/` — 원장 프로파일·질환별·논문근거·경쟁분석 (JSON)
- **대본 패키지**: `data/seoul-ortho/out/오십견_package.json`
- **대시보드**: `data/seoul-ortho/out/오십견_package.html` ← 대본+화면+대사+스토리보드+산출물 12종+원장 검수
- **컴플라이언스**: 통과 못 하면 "발행 불가 + 뭐가 문제인지" 출력

---

## 두 종류 흐름

**A. 처음 병원 붙일 때** — KB부터 새로:
`init` → 설정 → 자료 → `all`

**B. 그 병원 다른 편 뽑을 때** — KB 있으니 대본만:

```bash
python run.py episode --hospital seoul-ortho --topic 무릎관절염
```

```bash
python run.py render --hospital seoul-ortho --file data/seoul-ortho/out/무릎관절염_package.json
```

---

## 단계별로 따로 돌리기 (원할 때)

```bash
python run.py ingest --hospital seoul-ortho
```
```bash
python run.py kb --hospital seoul-ortho
```
```bash
python run.py compliance --file data/seoul-ortho/out/오십견_package.json --edition 오십견
```

---

## ⚠️ 자동화 아닌 부분 (사람이 반드시)

파이프라인은 **초안·데이터·렌더링까지**. 아래는 자동 발행 금지:
- **논문 근거 팩트체크 최종 확인** (LLM 초안은 검증 필요)
- **원장 의학 검수** — 대시보드 맨 아래 "원장 검수" 섹션에서 O/X, 저장·내보내기
- **의료광고 사전심의** (한의사협회 등)
- **최종 발행 승인**

컴플라이언스 게이트가 PASS해도 그건 **자동 규칙 통과일 뿐**, 발행 보장이 아님.

---

## 자료가 부족하면?

- 시작 체크리스트가 **필수 누락을 경고**함.
- KB는 없는 정보를 지어내지 않고 **`[공백]`으로 표시** → 원장 인터뷰로 채우게.
- 결과물은 나오지만 **얇아지고 【검수】 항목이 늘어남**. (논문 없으면 근거 약함, 인터뷰 없으면 말투 추정 등)

부족할수록 사람이 채울 곳이 많아진다 = 자료를 잘 받는 게 품질의 핵심.
