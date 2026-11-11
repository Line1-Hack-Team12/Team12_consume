# F03 추출기 인터페이스 (백엔드 연동용)

AI 파트가 제공하는 것: 캡처 이미지 N장 + 슬롯 달 → 거래 후보 JSON. 호출 단위는 "한 세션의 한 슬롯 달".
코드: `backend/f03_extraction.py` (Python 3.12, Anthropic SDK). 백엔드는 이 JSON 만 알면 된다.

## 1. 입력

| 항목 | 타입 | 설명 |
|---|---|---|
| assets | [(asset_id, 이미지 경로)] | 업로드된 캡처. asset_id 는 백엔드의 UploadedFile id 를 문자열로 |
| slot_month | "YYYY-MM" | 이 캡처들이 올라온 달 슬롯 |
| owner_name | 문자열, 선택 | 온보딩에서 받은 사용자 이름. 본인 계좌 이체 구분에만 쓰고 결과에는 "본인" 표식만 남음 |

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

## 5. 환경

- `ANTHROPIC_API_KEY` 는 AI 서비스 쪽에만 둔다. Spring 은 키를 모른다.
- 거래내역 파일(엑셀·PDF) 업로드 경로도 같은 서비스에 `POST /parse` 로 추가 예정. 출력 JSON 은 동일 형식 (asset.status 와 rows 만 같고, confidence 는 전부 high).
