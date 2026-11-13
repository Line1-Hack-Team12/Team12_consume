# AI 서비스 인터페이스 (백엔드 연동용)

이 문서는 Python AI 서비스(`backend/service.py`)의 모든 엔드포인트를 정의한다. 1~8절 전부 구현됨 (추출·파일 파싱·분류·설명 생성·알림 문구). 각 절의 요청·응답 예시는 실제 서비스가 돌려주는 모양과 같다.

AI 파트가 제공하는 것: 캡처 이미지 N장 + 슬롯 달 → 거래 후보 JSON. 호출 단위는 "한 세션의 한 슬롯 달".
코드: `backend/f03_extraction.py` (Python 3.12, Anthropic SDK). 백엔드는 이 JSON 만 알면 된다.

## 1. 입력

| 항목 | 타입 | 설명 |
|---|---|---|
| assets | [(asset_id, 이미지 경로)] | 업로드된 캡처. asset_id 는 백엔드의 UploadedFile id 를 문자열로 |
| slot_month | "YYYY-MM" | 이 캡처들이 올라온 달 슬롯 |
| owner_name | 문자열, 선택 | 온보딩에서 받은 사용자 이름. 본인 계좌 이체 구분에만 쓰고 결과에는 "본인" 표식만 남음. 앞뒤에 다른 한글이 붙은 자리("하나은행" 의 "하나")는 이름으로 보지 않고, 한 글자 이름은 무시 |

지원 이미지: jpg / png / webp. 한 장당 5~6초, 여러 장은 동시 처리(최대 5장 동시).

## 2. 출력 (job 하나)

```json
{
  "job_id": "job_1a2b3c4d",
  "slot_month": "2026-10",
  "status": "succeeded",
  "model": "claude-sonnet-5-5",
  "extractor_version": "f03-0.10",
  "prompt_version": "p7",
  "assets": [
    {
      "asset_id": "17",
      "status": "succeeded",
      "app_guess": "토스",
      "displayed_month": "10월",
      "error_code": null,
      "error_message": null,
      "elapsed_sec": 4.2,
      "rows": [
        {
          "id": "tx_3f9a1c02b7",
          "asset_id": "17",
          "source_row_id": "17:row003",
          "date": "2026-10-15",
          "time": "12:14",
          "merchant": "메가커피 강남",
          "amount_krw": 2000,
          "direction": "out",
          "kind": "expense",
          "confidence": "high",
          "raw_text": "-2,000원 메가커피 강남 | 토스뱅크 체크카드",
          "flags": []
        }
      ]
    }
  ]
}
```

### job.status

| 값 | 뜻 | 백엔드 동작 |
|---|---|---|
| succeeded | 모든 장 처리 (skipped 포함) | 검토 단계로 |
| partial | 일부 장 실패 | 성공한 장 결과는 저장. 사용자에게 "일부 실패" 안내, 재시도 또는 부분 진행 |
| failed | 성공한 장 없음 | 재시도 안내 |
| no_transactions | 전부 거래 화면이 아님 | 캡처 교체 안내 |

### asset.status / error_code

| status | error_code | 뜻 | 재시도 |
|---|---|---|---|
| succeeded | – | 행 추출됨 | – |
| skipped | NOT_TRANSACTION_SCREEN | 거래 화면 아님(홈, 달력, 요약 화면 등) | 불가. 교체 안내 |
| failed | RATE_LIMITED, NETWORK_ERROR, UPSTREAM_UNAVAILABLE, API_ERROR, ANALYSIS_OUTPUT_INVALID, INTERNAL | 일시 오류 | 가능 (`retryable_assets()`) |
| failed | REFUSED, OUTPUT_TRUNCATED, UNSUPPORTED_MEDIA, BAD_REQUEST | 그 이미지로는 안 됨 | 불가. 교체 안내 (`replace_needed_assets()`) |

재시도는 실패한 장만 다시 돌리고 `merge_retry()` 로 원래 job 에 합친다. 성공한 장은 다시 돌리지 않는다 (행 id 가 바뀌어 사용자 수정이 날아감). 재시도 상한 2회.

### row 필드

| 필드 | 값 | 설명 |
|---|---|---|
| id | "tx_" + 10자 | (asset, 행 순서, 슬롯) 해시. 같은 장을 재추출해도 같은 id |
| date | "YYYY-MM-DD" 또는 "" | "" 이면 화면에 날짜가 없던 행. flags 에 date_missing |
| time | "HH:MM" 또는 null | |
| merchant | 문자열 | 화면 표기 그대로. 사람 이름은 "개인", 사용자 본인은 "본인". 빈 값이면 merchant_missing |
| amount_krw | 양의 정수 | 부호 없음. 방향은 direction |
| direction | in / out / unknown | 이 계좌 기준 입금/출금 |
| kind | expense / income / transfer / self_transfer / refund / cancel / unknown | 아래 집계 규칙 참고 |
| confidence | high / medium / low | |
| raw_text | 문자열 | 그 행의 화면 글자. 계좌·카드번호·잔액·이름은 제거됨 |
| flags | 문자열 배열 | 아래 표 |

### flags → 검토 큐

| flag | 뜻 | 검토 큐 |
|---|---|---|
| date_missing | 날짜를 화면에서 못 읽음 | 올림 (필수값). 사용자가 날짜 입력 |
| merchant_missing | 가맹점 비어 있음 | 올림 (필수값) |
| unknown_direction | 입출금 방향 모름 | 올림. 합계에서 제외 |
| low_confidence | 모델이 자신 없음 | 올림. 금액 확인 |
| month_mismatch | 행 날짜가 슬롯 달과 다름 | 올림. 슬롯 수정 또는 제외 |
| kind_conflict | kind 와 direction 이 안 맞음 | 안 올림 (정보성) |

### kind 집계 규칙 (F07)

- expense + out → 지출. income + in → 수입.
- refund / cancel → 방향과 무관하게 지출 차감.
- self_transfer → 수입·지출 모두 제외 (본인 계좌 간 이동). direction 이 unknown 이어도 검토 큐에 올리지 않음.
  간편결제 머니 충전("네이버페이충전", "충전 | 토스뱅크 → 토스페이머니", "OO 결제 충전")과 본인 계좌 ↔ 페이 회사 이동("내 우리계좌 → 네이버파이낸셜")도
  f03-0.11 부터 self_transfer 로 나온다 (결정적 규칙). 백엔드 규칙(F07): 같은 달에 그 간편결제의 결제 행이 하나도 없으면 충전 행을 지출로 되돌린다.
  교통카드(티머니) 충전은 지출(expense) 그대로.
- transfer → out 이면 지출, in 이면 수입 (타인과의 이체).

## 3. 백엔드 엔티티와의 차이 (맞춰야 할 것)

현재 `Transaction` 엔티티에는 없는 값: direction, kind, flags, raw_text, source asset. `confidence` 는 Double 이 아니라 high/medium/low 문자열.
검토 큐는 flags 로 만들어지고, 패턴 분석(`AnalysisTransaction`)에는 direction 과 kind 가 필요하다. 최소한 direction, kind, flags, confidence(문자열), uploaded_file_id 를 Transaction 에 추가해야 한다.
업로드 파일의 바이트를 저장해야 한다 (현재는 파일명만 저장). 추출이 끝나면 이미지는 지워도 된다 (raw_text 로 원문 추적).

## 4. 호출 방식 (구현됨: `backend/service.py`)

Python 이므로 Spring 에서 직접 import 할 수 없다. FastAPI 서비스를 띄우고 Spring 이 HTTP 로 호출한다.

```
cd backend
pip install -r requirements.txt
cp .env.example .env    # ANTHROPIC_API_KEY 입력
uvicorn service:app --host 0.0.0.0 --port 8001
```

Swagger: `http://localhost:8001/docs`

| 메서드 | 경로 | 요청 (multipart/form-data) | 응답 |
|---|---|---|---|
| GET | /health | – | `{"ok": true, "model", "extractor_version", "prompt_version"}` |
| POST | /extract | `slot_month`(YYYY-MM), `images[]`(jpg/png/webp, 장당 10MB, 최대 20장), `asset_ids[]`(images 와 같은 순서·개수, UploadedFile id), `owner_name`(선택) | 2절의 job JSON |
| POST | /extract/retry | `original_job`(이전 응답 JSON 문자열), `images[]`, `asset_ids[]`(원 job 에서 failed 이고 재시도 가능한 것만), `owner_name` | 병합된 job JSON + `retry: {retried, still_retryable, replace_needed, max_retry_rounds}` |

오류 응답은 `{"detail": "..."}` 와 HTTP 400(입력 오류) / 413(이미지 크기) / 415(형식) / 409(병합 불가: 슬롯 불일치, 성공 자산 덮어쓰기).

- 동기 응답. 한 장 5~6초, 5장 동시 처리라 10장이면 15초 안팎. **Spring 의 HTTP 클라이언트 타임아웃을 120초 이상**으로.
- `asset_ids` 는 같은 이름의 폼 필드를 반복해서 보낸다 (Spring `MultiValueMap` 에 `add("asset_ids", "17")` 반복).
- 서비스는 아무것도 저장하지 않는다. 이미지는 요청 처리 중에만 임시 디렉터리에 있고 응답 후 삭제. 원 job JSON 은 백엔드가 보관했다가 재시도 때 돌려보낸다.
- 재시도 흐름: 응답의 `assets[].status == "failed"` 중 error_code 가 일시 오류인 것만 `/extract/retry` 로. `retry.replace_needed` 에 든 asset 은 사용자에게 교체를 안내. 상한 2회.

curl 예시:

```
curl -X POST http://localhost:8001/extract   -F slot_month=2026-10 -F owner_name=홍길동   -F asset_ids=17 -F images=@cap1.png   -F asset_ids=18 -F images=@cap2.jpg
```

Spring 쪽 흐름: 세션 생성 시 파일 저장 → 슬롯별로 `/extract` 호출 → rows 를 Transaction 으로 저장 → flags 로 검토 큐 구성 → 세션 상태 갱신(EXTRACTING → REVIEWING 또는 PARTIAL_FAILED).

## 5. 거래내역 파일 업로드 (`POST /parse`, 구현됨: `backend/bank_file_parser.py`)

캡처 여러 장 대신 은행 앱이 내보낸 거래내역 파일 한 개로 한 달 이상을 한 번에 넣는 주 경로. 지금은 **토스뱅크 xlsx** 만 지원.
(토스 앱 → 토스뱅크 계좌 → 관리 → 문서 → 거래내역서 → 엑셀. 발급 파일에는 열기 암호가 걸려 있다.)

| 요청 (multipart/form-data) | 설명 |
|---|---|
| `file` | xlsx (20MB 이하) |
| `asset_id` | 백엔드의 UploadedFile id |
| `owner_name` | 선택. 본인 계좌 이체 구분용 |
| `password` | 발급 파일의 열기 암호. 메모리에서만 쓰고 저장·로그하지 않음. 없으면 422 |
| `months[]` | 선택. 돌려줄 달(YYYY-MM, 반복). 비우면 파일의 모든 달. 같은 달이 두 번 오면 한 번만 돌려준다 |

응답은 2절과 같은 모양이고 다음이 다르다.

- `assets` 는 **달마다 하나** (`asset_id = "<asset_id>:<YYYY-MM>"`, `slot_month` 포함). 행이 없는 달은 `skipped` / `NO_ROWS_IN_MONTH`.
- 최상위에 `source: "file"`, `source_format: "toss_bank_xlsx"`, `parser_version`, `period`(파일의 첫·끝 거래일), `skipped_rows`.
- 모든 행 `confidence: "high"`, 플래그는 `merchant_missing` 외에 생기지 않음. 검토 큐에 올릴 것이 거의 없다.
- 읽는 열은 거래 일시·적요·거래 유형·거래 기관·거래 금액뿐. **거래 후 잔액, 상대 계좌번호, 메모, 머리글의 성명·계좌번호는 읽지 않는다.** 파일은 디스크에 쓰지 않는다.
- kind: 체크카드결제 → expense(양수면 refund), 프로모션입금·이자입금 → income, 내계좌간자동이체 → self_transfer, 자동이체·CMS·지로 → expense, 입금·출금·오픈뱅킹·펌뱅킹 → transfer(상대가 본인이면 self_transfer).
- 이체류 적요의 사람 이름은 "개인", 캡처 주인은 "본인". 한글 3자 단독·가려진 이름(홍*동)을 사람으로 본다. 2자·4자 상호는 그대로 둔다.

오류: 400(입력), 413(크기), 415(지원하지 않는 형식·구형 .xls·깨진 파일·머리글 없음), 422(암호 필요 또는 틀림). 이 엔드포인트는 입력이 어떤 모양이어도 500 을 내지 않는다.

같은 달에 캡처와 파일이 둘 다 들어오면 같은 거래가 두 번 생긴다. 백엔드는 (날짜, 금액, 방향) 이 같은 행을 중복 후보로 묶는 기존 로직을 그대로 쓰면 된다.

## 6. 가맹점 분류 `POST /classify` (구현됨: `backend/classify.py`)

F07. 가맹점명을 상위 카테고리 7개와 하위 태그로 분류한다. 백엔드가 가맹점 캐시를 먼저 보고, 없는 가맹점만 모아서 한 번에 보낸다.

요청 (JSON):

```json
{
  "items": [
    {"merchant": "GS25 역삼점", "kind": "expense", "raw_text": "-4,500원 GS25 역삼점 | 토스뱅크 체크카드"},
    {"merchant": "(주)우아한형제들", "kind": "expense", "raw_text": null}
  ]
}
```

| 필드 | 설명 |
|---|---|
| items[].merchant | 추출 결과의 merchant 그대로. 한 번에 최대 100개, 중복은 백엔드가 제거 |
| items[].kind | 추출 결과의 kind. expense·refund·cancel 외(income, transfer, self_transfer)는 보내지 않는다 (분류 대상 아님) |
| items[].raw_text | 선택. 결제 수단·메모가 분류에 도움이 될 때 |

응답:

```json
{
  "items": [
    {"merchant": "GS25 역삼점", "category": "living", "subcategory": "편의점", "confidence": "high"},
    {"merchant": "(주)우아한형제들", "category": "delivery", "subcategory": null, "confidence": "high"}
  ],
  "model": "claude-sonnet-5-5", "prompt_version": "c2"
}
```

| 값 | 허용 |
|---|---|
| category | food, delivery, cafe, shopping, transport, living, other |
| subcategory | food: 외식·주점 / shopping: 온라인·오프라인 / transport: 택시·대중교통 / living: 편의점·구독·주거·통신·의료 / other: 이체·여가·차량·미분류 / 그 외 null |
| confidence | high, medium, low. low 는 백엔드가 캐시에 넣지 않고 세션 범위로만 쓴다 |

- 응답 순서는 요청 순서와 같고 개수도 같다. 모델이 허용값 밖을 내면 서비스가 other/null/low 로 바꿔 돌려준다 (백엔드는 enum 검증을 다시 할 필요 없음).
- 소요: 57개 실측 10초. 100개씩 나눠 동시 3묶음까지 호출. 같은 세션에서 같은 가맹점을 두 번 보내지 않는다.
- 모델 오류·거부·스키마 실패는 그 묶음을 other/미분류/low 로 돌려주고 HTTP 500 을 내지 않는다. 분류 실패가 분석을 막지 않게 하기 위해서다. low 는 캐시에 넣지 않으므로 다음 세션에서 다시 시도된다.
- 백엔드 규칙(F07): 공통 캐시 저장은 confidence high·medium 만. 사용자 수정은 세션별 override 로 저장하고 캐시보다 우선.

## 7. 설명 생성 `POST /narrate` (구현됨: `backend/narrate.py`)

F11·F15·F16. 백엔드가 계산한 패턴 수치로 카드 문장·가설 질문·행동 제안·태그 추천·총평을 만든다. **숫자와 거래 ID 는 입력을 그대로 쓰고, 서비스가 검증한다.** 말투를 바꿀 때도 같은 요청에 tone 만 바꿔 다시 부른다 (분석 재실행 없음). 백엔드는 (snapshot_id, tone) 으로 결과를 캐시한다.

요청 (JSON):

```json
{
  "snapshot_id": "snap_12",
  "tone": "coach",
  "period": {"start": "2026-08-01", "end": "2026-10-31", "slots": ["2026-08", "2026-09", "2026-10"]},
  "want": ["cards", "summary", "report"],
  "patterns": [
    {
      "pattern_id": "pat_3",
      "type": "weekday",
      "condition": {"weekday": 4, "hour_from": 21, "hour_to": 24, "category": "delivery", "subcategory": null, "weather": null},
      "observed": {"hit": 11, "window": 13, "unit": "주"},
      "sum_krw": 286000,
      "chained_sum_krw": 94000,
      "evidence_grade": "strong",
      "evidence_transactions": [
        {"id": "tx_3f9a1c02b7", "date": "2026-10-23", "time": "22:14", "merchant": "(주)우아한형제들", "amount_krw": 27000, "category": "delivery", "subcategory": null},
        {"id": "tx_8b2e...", "date": "2026-10-16", "time": "21:50", "merchant": "(주)우아한형제들", "amount_krw": 24500, "category": "delivery", "subcategory": null}
      ],
      "chain_transactions": [
        {"id": "tx_c1...", "date": "2026-10-24", "time": "10:40", "merchant": "CU 역삼점", "amount_krw": 6800, "category": "living", "subcategory": "편의점", "lag_minutes": 746}
      ]
    }
  ],
  "summary": {
    "total_spend_krw": 1243500,
    "by_category": [{"category": "food", "sum_krw": 412000, "share": 0.33}],
    "by_month": [{"month": "2026-08", "sum_krw": 401000}],
    "limits": {"days_without_upload": 4, "transactions_without_time": 12, "weather_skipped": true}
  },
  "subscriptions": [{"merchant": "유튜브 프리미엄", "monthly_krw": 14900, "observed_months": 3, "price_increased": false}]
}
```

| 필드 | 설명 |
|---|---|
| tone | coach, friend, grandma |
| want | 생성할 것. cards(카드 문장), summary(결과 홈 요약 한 줄), report(리포트 총평·관찰·행동·한계) 중 선택 |
| patterns[].type | weekday, late_night, rain, chain, next_morning_chain, payday |
| patterns[].condition | 탐지기 조건. 문장에 쓸 사실 (요일·시간대·카테고리·날씨) |
| patterns[].observed | "13주 중 11회" 의 숫자. 문장은 이 숫자만 쓴다 |
| evidence_transactions | 카드에 보여줄 증거. 3건 이상 권장, 최대 10건. id 는 snapshot 의 거래 id |
| chain_transactions | 후속 결제. chained_sum_krw 의 근거 |
| summary, subscriptions | want 에 summary·report 가 있을 때만 필요 |

응답:

```json
{
  "cards": [
    {
      "pattern_id": "pat_3",
      "title": "목요일 밤 배달",
      "observation": "최근 13주 중 11주, 목요일 21시 이후에 배달 주문이 있었어요. 직접 비용 286,000원에 다음날 편의점·카페 94,000원이 따라왔어요.",
      "hypothesis_question": "목요일 밤에 유독 주문이 몰리는 이유가 있을까요?",
      "action": "목요일 저녁에는 주문 전에 냉장고를 한 번 열어 보세요.",
      "recommended_tags": ["overtime", "stress", "habit"],
      "evidence_transaction_ids": ["tx_3f9a1c02b7", "tx_8b2e..."]
    }
  ],
  "summary_line": "지난 3개월, 목요일 밤 배달이 가장 큰 반복 지출이었어요.",
  "report": {
    "overview": ["문장 1", "문장 2", "문장 3"],
    "observations": [{"pattern_id": "pat_3", "sentence": "..."}],
    "actions": ["...", "..."],
    "limits_sentence": "업로드가 없는 날 4일과 시각이 없는 거래 12건은 시간대 분석에서 빠졌어요."
  },
  "model": "claude-sonnet-5-5", "prompt_version": "n2", "tone": "coach"
}
```

서비스가 보장하는 것 (백엔드는 저장만 하면 됨):

- `evidence_transaction_ids` 는 **그 패턴의** evidence id 부분집합이고 min(3, 증거 수) 건 이상. chain_transactions 의 id 는 증거로 쓸 수 없다. 어긋나면 오류 목록을 붙여 1회 교정 재시도, 그래도 틀리면 HTTP 422 `{"detail": {"error_code": "ANALYSIS_OUTPUT_INVALID", "errors": ["cards[pat_3].action: 입력에 없는 숫자 50,000 → 문장: ..."]}}`.
- 문장에 나오는 숫자는 전부 요청 숫자 집합에 있어야 한다. 카드: hit·window·sum_krw·chained_sum_krw·둘의 합·hour_from/to·증거·후속 거래의 금액과 날짜. 요약·리포트: 기간(연·월·슬롯 수)·total_spend_krw·카테고리 합계와 share 를 반올림한 %·월별 합계·limits 수치·구독 월액과 합계, 그리고 모든 카드 숫자. 어긋나면 위와 같이 처리. 반올림("약 29만 원")·비율·평균은 나오지 않는다.
- `recommended_tags` 는 F13 허용값(overtime, rain, stress, habit, no_specific_reason) 중 정확히 3개. 모델이 중복·허용값 밖·개수 틀림으로 내면 서비스가 패턴 유형별 기본값으로 채워 교정한다 (재시도 사유 아님). custom 은 추천하지 않는다.
- cards 는 요청 patterns 순서대로 하나씩. want 에 없는 항목은 `cards: []` / `summary_line: null` / `report: null`. 응답에 `snapshot_id` 를 그대로 되돌려 준다.
- 요청 제한: patterns 최대 10개, evidence_transactions 패턴당 1~10건(id 중복 불가), chain_transactions 최대 10건, pattern_id 중복 불가. 어기면 422 (FastAPI 검증).
- 모델 호출 자체가 실패(429, 5xx, 네트워크)하면 502 `{"detail": {"error_code": "ANALYSIS_FAILED"}}`. 백엔드는 세션을 실패로 돌리지 말고 재시도 버튼으로 같은 요청을 다시 보내면 된다.
- observation 은 관찰 사실만("비 오는 날 배달 2건"), 원인 단정 없음. hypothesis_question 은 질문 한 문장, action 은 한 문장.
- 말투 변환 실패 시 중립 표현으로 대체하고 응답 `tone` 에 "neutral" 을 적는다.
- 소요: 카드 3장 + 요약 + 리포트 실측 9~11초 (말투별). 백엔드는 세션 상태 ANALYZING 으로 두고 호출하고 HTTP 타임아웃은 120초 이상.

## 8. 알림 문구 `POST /notify-copy` (구현됨: `backend/notify.py`)

F14. 카드 토글을 켜거나 태그를 바꿀 때 알림 미리보기 문구를 만든다. 필수 안내 문장은 모델이 아니라 서비스 코드가 붙인다.

요청:

```json
{
  "tone": "grandma",
  "pattern": {"pattern_id": "pat_3", "type": "weekday", "condition": {"weekday": 4, "hour_from": 21, "hour_to": 24, "category": "delivery"},
              "observed": {"hit": 11, "window": 13, "unit": "주"}, "sum_krw": 286000, "chained_sum_krw": 94000},
  "cause_tags": ["overtime", "stress"],
  "scheduled_time": "20:00"
}
```

응답:

```json
{
  "title": "목요일 밤이다",
  "body": "야근하고 지친 목요일, 13주 중 11주를 배달로 때웠지. 오늘은 냉장고부터 열어 봐라.",
  "notice": "과거 기록을 참고한 사전 안내이며 새 결제를 감지한 결과가 아닙니다.",
  "model": "claude-sonnet-5-5", "prompt_version": "a1"
}
```

- `notice` 는 항상 같은 문장이고 서비스 코드가 붙인다. 프론트는 body 아래에 그대로 표시한다. 응답에 `pattern_id` 와 `tone` 도 돌려준다 (거부 폴백 시 "neutral").
- title 20자, body 80자 이내 (공백 포함, 서비스가 길이를 검사). 태그 맥락은 "야근하고 지친"처럼 문구에만 반영하고, 당일 야근 여부를 판정하지 않는다.
- cause_tags 는 F13 허용값(overtime, rain, stress, habit, no_specific_reason)과 특수 상태(unknown, not_applicable, skipped), custom 만 받는다. 그 외 값은 422. 비어 있거나 not_applicable 이 섞여 있으면 태그 맥락 없이 패턴 수치만으로 쓴다. custom 의 본문은 보내지 않는다 (서비스는 허용값 태그만 문구에 반영).
- 문구의 숫자는 pattern 수치(hit, window, sum_krw, chained_sum_krw, 둘의 합, hour_from/to)와 scheduled_time 의 숫자만 허용. 조건에 없는 카테고리 단어(주점 패턴에 "배달")도 오류. 길이·숫자·카테고리 오류는 1회 교정, 그래도 틀리면 422 `{"detail": {"error_code": "ANALYSIS_OUTPUT_INVALID", "errors": [...]}}`. 모델 호출 실패는 502 `ANALYSIS_FAILED`.
- 소요: 실측 2~3초 (가끔 9초). 토글·태그 변경 직후 미리보기라 동기 호출로 충분하다. 같은 (pattern_id, tone, cause_tags) 는 백엔드가 캐시해도 된다.

## 9. 환경

- `ANTHROPIC_API_KEY` 는 AI 서비스 쪽에만 둔다. Spring 은 키를 모른다.
- 의존성: `requirements.txt` (anthropic, fastapi, uvicorn, python-multipart, pillow, openpyxl, msoffcrypto-tool).
- 환경변수: `ANTHROPIC_API_KEY`(필수), `F03_MODEL`(기본 claude-sonnet-5-5). `F07_MODEL`·`F11_MODEL`·`F14_MODEL` 로 기능별 모델을 바꿀 수 있다. `.env.example` 참고.
- `AI_MAX_CONCURRENT_CALLS`(기본 8): 프로세스 전체의 동시 모델 호출 상한. 추출 5장·분류 3묶음 같은 모듈별 상한은 "한 요청 안"의 값이고, 요청이 여러 개 겹치면 이 값이 전체를 묶는다. 429 가 잦으면 내리고, 요청이 밀리면 올린다 (`llm_common.py`).
- 모델 클라이언트는 프로세스에 하나만 만들어 연결 풀을 공유한다. 파일 파싱·이미지 축소는 스레드로 보내서 긴 작업 중에도 `/health` 가 멈추지 않는다.

### Docker (`backend/Dockerfile`)

이미지에는 `requirements.txt` 와 서비스 코드 7개 파일(`service.py`, `llm_common.py`, `f03_extraction.py`, `bank_file_parser.py`, `classify.py`, `narrate.py`, `notify.py`)만 들어간다 (`.dockerignore` 가 .env·samples·truth·결과 json·테스트를 막고, COPY 도 파일을 하나씩 지정). 비루트 사용자 `app` 으로 실행하고 `/health` 로 헬스체크한다. 실측 이미지 211MB, 기동 2초 안팎.

```bash
docker build -t team12-ai ./backend
docker run --rm --env-file backend/.env -p 127.0.0.1:8001:8001 team12-ai
```

docker compose 에 넣을 때의 `ai` 서비스 조각. **`ports:` 가 아니라 `expose:` 를 쓴다.** 이 서비스에는 인증이 없어서 호스트 밖으로 열리면 누구나 이미지를 올리고 API 크레딧을 쓸 수 있다. Spring 컨테이너만 `http://ai:8001` 로 부른다.

```yaml
services:
  ai:
    build: ./backend
    env_file: ./backend/.env          # ANTHROPIC_API_KEY. 저장소에 없음, 각자 .env.example 을 복사해 채움
    expose: ["8001"]                  # compose 네트워크 안에서만. 디버깅용으로 열려면 ports: ["127.0.0.1:8001:8001"]
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8001/health"]
      interval: 10s
      timeout: 3s
      start_period: 15s
      retries: 3
    restart: unless-stopped

  api:                                # Spring
    build: ./spring
    ports: ["8080:8080"]
    environment:
      AI_BASE_URL: http://ai:8001     # Spring 의 AI 클라이언트 base URL. HTTP 타임아웃 120초 이상
    depends_on:
      ai:
        condition: service_healthy
```

- 컨테이너는 아무것도 디스크에 남기지 않는다. 업로드 이미지는 요청 동안 `/tmp` 에만 있고 응답 후 지운다. 볼륨이 필요 없다.
- 로그는 stdout 으로만 나가고 이미지·원문·이름·키를 적지 않는다. `docker logs ai` 로 요청별 건수·상태만 보인다.
- 배포: 이 레포의 `.github/workflows/ai-image.yml` 이 main 의 `backend/**` 변경을 Docker Hub `<계정>/line1thon-ai:latest` 로 올리고, `EC2_HOST` 시크릿이 있으면 EC2 의 `~/app` 에서 `docker compose pull ai && up -d ai` 를 실행한다. 필요한 시크릿 4개는 워크플로 파일 머리에 적혀 있다. `ANTHROPIC_API_KEY` 는 EC2 의 `~/app/.env` 에만 둔다.
- 서버에 올리면 컨테이너에서 `api.anthropic.com` 으로 나가는 아웃바운드가 열려 있어야 한다. 크레딧이 떨어지면 모든 AI 호출이 502 `ANALYSIS_FAILED`(분류는 other/미분류/low 폴백)로 떨어지므로 시연 전 잔액을 확인한다.
