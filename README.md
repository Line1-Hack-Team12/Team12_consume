# 소비 패턴 분석 서비스 · AI 파트 (Team12)

1호선톤 해커톤(주제: AI를 통한 학습과 성장) Team12 의 **AI 서비스 저장소**다. 사용자가 올린 은행·카드 앱 캡처와 거래내역 파일에서 거래를 뽑아 내고, 카테고리를 붙이고, 반복되는 소비 패턴을 사람 말로 설명하고, 그 시간이 오기 전에 알려 주는 문구를 만든다.

- 서비스(프론트): https://team12-consume.vercel.app
- 저장소: [AI](https://github.com/Line1-Hack-Team12/Team12_consume) · [프론트](https://github.com/Line1-Hack-Team12/Team12_consume_FE) · [백엔드](https://github.com/Line1-Hack-Team12/Team12_BE)

## 무엇을 하나

가계부는 적는 사람이 계속 적어야 유지된다. 이 서비스는 적는 대신 **이미 있는 기록**(앱 캡처, 은행 내보내기 파일)을 올리면 끝나게 하고, 합계 대신 **"목요일 밤마다 배달"** 같은 반복을 찾아 "그날 무슨 일이 있었나요?" 라고 묻는다. 사용자가 원인(야근·비·습관)을 답하면 그 조건이 오기 전에 미리 알려 준다.

```
업로드(캡처·xlsx) → 거래 추출 → 분류 → 검토 → 패턴 탐지 → 카드·보고서 → 사전 알림
   S01              AI /extract   AI       S03     백엔드      AI /narrate   AI /notify-copy
                    AI /parse     /classify
```

AI 가 하는 일은 **읽기**(캡처·파일 → 거래 행)와 **글쓰기**(수치 → 문장)뿐이다. 합산과 패턴 탐지는 백엔드 코드가 하고, AI 가 쓴 문장의 숫자는 입력에 있는 숫자인지 전부 검증한다. 이름·계좌·카드번호·잔액은 저장 전에 지운다.

## 이 저장소에 있는 것

| 경로 | 내용 |
|---|---|
| `backend/service.py` | FastAPI 서비스. 백엔드가 부르는 엔드포인트 6개 (`/extract`, `/extract/retry`, `/parse`, `/classify`, `/narrate`, `/notify-copy`) |
| `backend/f03_extraction.py` | 캡처 이미지 → 거래 행 (Claude 비전 + 후처리·개인정보 삭제) |
| `backend/bank_file_parser.py` | 토스뱅크 거래내역 xlsx → 거래 행 (모델 미사용) |
| `backend/classify.py` · `narrate.py` · `notify.py` | 가맹점 분류, 카드·보고서 문장, 알림 문구 |
| `backend/llm_common.py` | 모델 호출 공통부 (동시 호출 상한, JSON 검증, 교정 재시도, 폴백) |
| `backend/test_*.py` | 단위 테스트 94개 (모델 호출 없음) |
| `backend/eval_f03.py` · `EVAL_LOG.md` | 실제 캡처 정답 세트로 채점하는 도구와 실험 기록 |
| `backend/F03_INTERFACE.md` | 백엔드 연동용 요청·응답 명세 |
| `pipeline.md` | AI 파이프라인을 처음 보는 사람용으로 풀어 쓴 안내 |
| `FunctionalSpec.md` · `UserFlow.md` | 기능 명세와 사용자 흐름 |
| `.github/workflows/ai-image.yml` | `backend/` 변경 시 Docker 이미지 빌드 → Docker Hub → EC2 갱신 |

## 실행

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate      # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                  # ANTHROPIC_API_KEY 를 채운다
uvicorn service:app --port 8001                       # http://localhost:8001/docs
python -m pytest -q                                   # 테스트 (API 키 불필요)
```

Docker 로는 `docker build -t team12-ai ./backend` 뒤 `docker run --rm --env-file backend/.env -p 8001:8001 team12-ai`. 백엔드 저장소의 `docker-compose.yml` 에 `ai` 서비스로 들어가 있고, Spring 은 `AI_SERVER_URL=http://ai:8001` 로 부른다.

환경변수: `ANTHROPIC_API_KEY`(필수), `F03_MODEL`(기본 `claude-sonnet-5-5`), `AI_MAX_CONCURRENT_CALLS`(기본 8).

## 품질을 어떻게 확인했나

- 실제 앱 캡처 29장(거래 화면 20장 + 비거래 화면 9장), 135행의 정답 세트를 만들어 프롬프트·코드를 바꿀 때마다 채점했다. 최종: 금액 오독 0, 누락·과잉 0, 거래 화면 판정 29/29, 이름 잔존 0. 과정은 `backend/EVAL_LOG.md`.
- 추출 결과에 `extractor_version`·`prompt_version` 이 찍혀 어느 버전으로 뽑은 결과인지 추적된다.
- 설명·알림 문장은 숫자 검증을 통과해야 나가고, 실패하면 템플릿 문장으로 대체된다.
- 백엔드와 합쳐 실제 13개월치 파일로 S01 → S07 을 끝까지 돌려 확인했다.

## 범위

- 입력: 캡처 이미지(jpg/png/webp, 앱 무관), 토스뱅크 거래내역 xlsx. PDF 와 다른 은행 양식은 이번 범위 밖.
- 패턴: 요일 반복, 심야 반복, 연쇄 결제, 비 오는 날 반복(지역의 실제 강수일 기준, Open-Meteo), 다음날 아침 연쇄. 월급날 유형은 AI 쪽은 준비되어 있고 백엔드 탐지기는 다음 단계.
- 알림은 결제를 실시간으로 감지하지 않는다. 과거 반복을 보고 미리 알려 주는 것이며 모든 알림에 그 고지가 붙는다.

## 개인정보

캡처와 거래내역 원본, 정답 세트, 실행 결과는 저장소에 올리지 않는다 (`.gitignore`). API 키는 `.env` 와 컨테이너 환경변수로만 주입한다. Docker 이미지에는 서비스 코드 7개 파일만 들어간다.
