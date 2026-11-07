"""
F03 구조화 추출 (Structured Extraction)
======================================

결제 내역 캡처 이미지 한 장을 Claude(비전)에 보내서 거래 목록 JSON으로 바꾸는 모듈입니다.
기능명세서 v1.0의 F03에 해당하며, 다음을 책임집니다.

  1. 이미지 전처리      : 긴 변 1568px 이하로 축소, base64 인코딩 (API 이미지 한도 대응)
  2. 모델 호출          : Claude Sonnet 5.5 + 구조화 출력(JSON 스키마) + 프롬프트 캐시
  3. 스키마 검증·교정   : 응답이 스키마에 안 맞으면 오류를 돌려주고 1회만 재시도
  4. 후처리(코드)       : 슬롯 연·월로 날짜 확정, 금액 정수화, 슬롯 월 불일치 플래그,
                         확신 낮은 행 표시, source_row_id 부여
  5. job 오케스트레이션 : 여러 장을 동시에(세마포어로 상한) 처리하고
                         succeeded / partial / failed 로 정리

설계 원칙 (기능명세서 3.3 "코드와 AI의 경계")
  - AI는 "읽기"만 한다. 날짜 확정, 금액 정규화, 합계 같은 숫자 처리는 전부 코드가 한다.
  - 한 이미지 = 한 요청. 파일별 성공·실패를 분리하고, 실패한 자산만 재시도한다.
  - 이미지·원문은 로그에 남기지 않는다. request_id, 소요 시간, 토큰 사용량만 기록한다.

사용 예 (CLI)
  $ pip install -r requirements.txt
  $ export ANTHROPIC_API_KEY=sk-ant-...        # 또는 `ant auth login`
  $ python f03_extraction.py ./captures --slot 2026-10
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # backend/.env 가 있으면 환경변수로 올림. 없으면 조용히 넘어감

import argparse
import asyncio
import base64
import io
import os
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError, field_validator

# Pillow는 "있으면 축소, 없으면 원본 그대로" 로 동작합니다. 10MB 캡처를 그대로 보내면
# API 이미지 한도에 걸릴 수 있으니 실제 서비스에서는 설치를 권장합니다.
try:
    from PIL import Image  # type: ignore

    _HAS_PIL = True
except ImportError:  # pragma: no cover
    _HAS_PIL = False

log = logging.getLogger("f03")

# ---------------------------------------------------------------------------
# 1. 설정값
# ---------------------------------------------------------------------------

MODEL = os.environ.get("F03_MODEL", "claude-sonnet-5-5")  # .env 의 F03_MODEL 로 교체 가능. 평가 세트로 claude-opus-5-5 와 비교 예정
EXTRACTOR_VERSION = "f03-0.1"           # 코드 변경 시 올림. 결과에 기록해 재추출 비교에 사용
PROMPT_VERSION = "p1"                   # SYSTEM_PROMPT 수정 시 올림 (p1, p2, ...)
EFFORT = "low"                       # 추출은 깊은 추론이 필요 없어서 low. 품질이 부족하면 medium
MAX_TOKENS = 8000                    # 캡처 1장 = 보통 15행 안팎. 사고 토큰도 여기 포함되므로 여유 있게
MAX_LONG_EDGE_PX = 1568              # API가 어차피 이 크기로 축소하므로 미리 줄여서 전송량을 아낌
CONCURRENCY = 5                      # 동시에 열어둘 요청 수. 분당 요청 제한(429) 회피용
REQUEST_TIMEOUT_SEC = 60.0           # 캡처 1장 추출은 5~10초면 끝남. 60초를 넘기면 비정상
ALLOWED_MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

# ---------------------------------------------------------------------------
# 2. 출력 스키마 (모델이 반환해야 하는 JSON 형태)
# ---------------------------------------------------------------------------
# 두 겹으로 정의합니다.
#   - EXTRACTION_SCHEMA : API의 output_config.format 에 넣는 JSON 스키마.
#                          서버가 이 스키마에 맞는 JSON만 생성하도록 강제합니다.
#   - ExtractionOutput  : 같은 내용을 pydantic 으로 한 번 더 검증합니다.
#                          (enum 값, 숫자 범위처럼 스키마만으로는 약한 부분을 잡기 위해)
#
# 값이 없을 때는 null 대신 빈 문자열 "" 을 쓰게 했습니다. 스키마가 단순해지고,
# 후처리에서 "" → None 으로 바꾸면 되기 때문입니다.

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # 이 캡처가 결제/거래 내역 화면인지. False 면 rows 를 비우고 이 자산은 제외합니다.
        "is_transaction_screen": {"type": "boolean"},
        # 어느 앱 캡처로 보이는지 (토스, 카카오뱅크, 삼성카드 ...). 모르면 "".
        "app_guess": {"type": "string"},
        # 캡처에 표시된 월. "YYYY-MM" 또는 "". 슬롯 월과 다르면 후처리에서 플래그를 세웁니다.
        "displayed_month": {"type": "string"},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "MM-DD. 캡처에 연도가 있으면 YYYY-MM-DD"},
                    "time": {"type": "string", "description": "HH:MM. 없으면 빈 문자열"},
                    "merchant": {"type": "string", "description": "가맹점·상대방 표기 원문. 100자 이내"},
                    "amount_krw": {"type": "integer", "description": "양수 정수 원. 부호는 direction 으로"},
                    "direction": {"type": "string", "enum": ["out", "in", "unknown"]},
                    "kind": {
                        "type": "string",
                        "enum": ["expense", "income", "transfer", "refund", "cancel", "unknown"],
                    },
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "raw_text": {"type": "string", "description": "그 행에 보이는 글자를 그대로"},
                },
                "required": ["date", "time", "merchant", "amount_krw", "direction", "kind", "confidence", "raw_text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["is_transaction_screen", "app_guess", "displayed_month", "rows"],
    "additionalProperties": False,
}


class ExtractedRow(BaseModel):
    """모델이 읽어낸 거래 한 행. 아직 '후보'이며 후처리 전입니다."""

    date: str
    time: str
    merchant: str = Field(max_length=100)
    amount_krw: int = Field(ge=0)
    direction: Literal["out", "in", "unknown"]
    kind: Literal["expense", "income", "transfer", "refund", "cancel", "unknown"]
    confidence: Literal["high", "medium", "low"]
    raw_text: str

    @field_validator("date")
    @classmethod
    def _date_shape(cls, v: str) -> str:
        # "MM-DD" 또는 "YYYY-MM-DD" 만 허용. 그 외는 스키마 오류로 간주해 교정 재시도를 유도합니다.
        parts = v.split("-")
        if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
            raise ValueError(f"date must be MM-DD or YYYY-MM-DD, got {v!r}")
        return v


class ExtractionOutput(BaseModel):
    is_transaction_screen: bool
    app_guess: str
    displayed_month: str
    rows: list[ExtractedRow]


# ---------------------------------------------------------------------------
# 3. 프롬프트
# ---------------------------------------------------------------------------
# 시스템 프롬프트는 모든 호출에서 완전히 같아야 프롬프트 캐시가 먹습니다.
# (날짜, 슬롯 정보처럼 호출마다 바뀌는 값은 절대 여기 넣지 말고 user 메시지로 보냅니다.)

SYSTEM_PROMPT = """당신은 한국 금융 앱(토스, 카카오뱅크, 카카오페이, 카드사 앱 등)의 결제 내역 캡처를
거래 목록 JSON으로 옮기는 추출기입니다. 판단이나 해석은 하지 않고, 화면에 보이는 것만 읽습니다.

읽기 규칙
- 거래 행만 추출합니다. 잔액, 소계, 누적 합계, 월 합계, 광고, 안내 문구, 탭 제목은 행이 아닙니다.
- 보이지 않거나 확실하지 않은 값은 추측하지 말고 빈 문자열("")로 두고 confidence 를 낮춥니다.
- 금액은 쉼표와 "원"을 제거한 양수 정수입니다. 부호나 색은 amount 에 넣지 말고 direction 으로 표현합니다.
  - "-", 빨간색/검은색 출금 표기, "결제", "출금" → direction "out"
  - "+", 파란색 입금 표기, "입금", "급여" → direction "in"
  - 어느 쪽인지 화면만으로 알 수 없으면 "unknown"
- kind: 일반 결제는 "expense", 입금은 "income", 계좌 이체는 "transfer",
  "환불"/"취소" 표기가 있으면 각각 "refund"/"cancel", 모르면 "unknown".
- 날짜에 연도가 없으면 MM-DD 로만 적습니다. 연도를 지어내지 마세요. 사용자 메시지에 이 캡처가
  속한 연·월 힌트가 있으니 displayed_month 는 화면에 실제로 보이는 월 표기를 기준으로 적습니다.
- 시각이 없는 행은 time 을 "" 로 둡니다.
- merchant 는 화면 표기 그대로(법인명, 지점명 포함) 적습니다. 카테고리로 바꾸지 마세요.
- raw_text 에는 그 행에 보이는 글자를 순서대로 그대로 옮깁니다.
- 이름, 계좌번호, 카드번호는 어떤 필드에도 넣지 않습니다.

자주 틀리는 지점 (특히 주의)
- 숫자 0 과 8, 1 과 7 을 혼동하기 쉽습니다. 쉼표 위치로 자릿수를 다시 확인하세요.
- 한 행이 두 줄로 표시되는 앱이 있습니다(가맹점명 줄 + 금액/시각 줄). 두 줄을 한 행으로 합칩니다.
- 화면 맨 위나 아래에 잘린 행은 금액이 온전히 보일 때만 포함하고 confidence 를 "low" 로 둡니다.

결제 내역 화면이 아니면(홈 화면, 광고, 다른 앱) is_transaction_screen 을 false 로 두고 rows 는 빈 배열로 둡니다."""


def _user_prompt(slot_month: str) -> str:
    """호출마다 달라지는 부분. 슬롯의 연·월만 힌트로 줍니다."""
    y, m = slot_month.split("-")
    return (
        f"이 캡처는 {int(y)}년 {int(m)}월 슬롯에 올라온 결제 내역입니다. "
        "화면에 보이는 거래 행을 모두 추출하세요."
    )


# ---------------------------------------------------------------------------
# 4. 결과 데이터 구조 (후처리 이후, 다른 모듈이 쓰는 형태)
# ---------------------------------------------------------------------------


@dataclass
class TransactionCandidate:
    """후처리가 끝난 거래 후보 한 건. F04 검토 표의 입력입니다."""

    id: str
    asset_id: str
    source_row_id: str
    date: str                      # YYYY-MM-DD (슬롯 연·월로 확정됨)
    time: str | None               # HH:MM 또는 None
    merchant: str
    amount_krw: int
    direction: str                 # out / in / unknown
    kind: str
    confidence: str                # high / medium / low (모델 참고값. 화면에 정확도로 표시하지 않음)
    raw_text: str
    flags: list[str] = field(default_factory=list)   # "month_mismatch", "low_confidence", "unknown_direction"


@dataclass
class AssetExtraction:
    """이미지 한 장의 추출 결과."""

    asset_id: str
    status: Literal["succeeded", "failed", "skipped"]
    rows: list[TransactionCandidate] = field(default_factory=list)
    app_guess: str = ""
    displayed_month: str = ""
    error_code: str | None = None       # NOT_TRANSACTION_SCREEN / ANALYSIS_OUTPUT_INVALID / REFUSED / API_ERROR ...
    error_message: str | None = None
    request_id: str | None = None
    elapsed_sec: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class ExtractionJobResult:
    """여러 장을 묶은 job 결과. 기능명세서의 queued/running/succeeded/partial/failed 에 대응."""

    job_id: str
    slot_month: str
    status: Literal["succeeded", "partial", "failed"]
    assets: list[AssetExtraction]

    @property
    def all_rows(self) -> list[TransactionCandidate]:
        return [r for a in self.assets if a.status == "succeeded" for r in a.rows]


# ---------------------------------------------------------------------------
# 5. 이미지 전처리
# ---------------------------------------------------------------------------


def prepare_image(path: Path) -> tuple[str, str]:
    """
    파일을 읽어 (media_type, base64 문자열) 로 돌려줍니다.

    - Pillow 가 있으면 긴 변을 MAX_LONG_EDGE_PX 이하로 줄입니다. 모델은 어차피 이 크기로
      축소해서 보므로 품질 손실 없이 전송량과 토큰을 아낄 수 있습니다.
    - WebP 는 그대로 보내도 되지만, 축소가 필요한 경우 JPEG 로 다시 인코딩합니다.
    """
    ext = path.suffix.lower()
    if ext not in ALLOWED_MEDIA:
        raise ValueError(f"unsupported image type: {ext}")
    media_type = ALLOWED_MEDIA[ext]
    data = path.read_bytes()

    if _HAS_PIL:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            longest = max(w, h)
            if longest > MAX_LONG_EDGE_PX:
                scale = MAX_LONG_EDGE_PX / longest
                im = im.convert("RGB").resize((round(w * scale), round(h * scale)))
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=90)
                data, media_type = buf.getvalue(), "image/jpeg"

    # base64 문자열에 줄바꿈이 섞이면 API가 거부하므로 standard_b64encode 를 씁니다.
    return media_type, base64.standard_b64encode(data).decode("utf-8")


# ---------------------------------------------------------------------------
# 6. 모델 호출 (이미지 한 장)
# ---------------------------------------------------------------------------


def _build_messages(media_type: str, b64: str, slot_month: str) -> list[dict[str, Any]]:
    """이미지 블록을 먼저, 지시 텍스트를 뒤에 두는 것이 비전 요청의 기본 형태입니다."""
    return [
        {
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": _user_prompt(slot_month)},
            ],
        }
    ]


async def _call_model(client: anthropic.AsyncAnthropic, messages: list[dict[str, Any]]) -> anthropic.types.Message:
    """
    Claude 호출 한 번. 여기서 정한 것들:

    - model/effort        : Sonnet 5.5 는 사고(thinking)가 기본으로 켜져 있어서 따로 끄지 않고
                            effort 만 low 로 내립니다. ({"type": "disabled"} 는 이 모델에서 400)
    - output_config.format: JSON 스키마를 지정하면 첫 text 블록이 항상 스키마에 맞는 JSON 입니다.
    - fallbacks="default" : 안전 분류기가 요청을 거부하면 서버가 다른 모델로 한 번 더 시도합니다.
                            Claude API 전용 베타라 Bedrock/Vertex 로 옮기면 이 두 줄은 빼야 합니다.
    - cache_control       : 시스템 프롬프트를 캐시합니다. 같은 프롬프트로 여러 장을 보내므로
                            두 번째 호출부터는 입력 비용이 크게 줄어듭니다.
    """
    return await client.beta.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
        output_config={
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
        },
    )


def _first_text(message: anthropic.types.Message) -> str:
    """응답에서 첫 text 블록을 꺼냅니다. (사고 블록이 앞에 올 수 있으니 type 으로 찾습니다)"""
    for block in message.content:
        if block.type == "text":
            return block.text
    raise ValueError("no text block in response")


def _usage_dict(message: anthropic.types.Message) -> dict[str, int]:
    u = message.usage
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


async def extract_one(
    client: anthropic.AsyncAnthropic,
    asset_id: str,
    image_path: Path,
    slot_month: str,
) -> AssetExtraction:
    """
    이미지 한 장을 끝까지 처리합니다: 전처리 → 호출 → 검증(+1회 교정) → 후처리.
    예외를 밖으로 던지지 않고 AssetExtraction.status 로 결과를 표현합니다.
    그래야 job 단계에서 "이 장만 실패" 를 partial 로 묶을 수 있습니다.
    """
    started = time.perf_counter()
    result = AssetExtraction(asset_id=asset_id, status="failed")

    try:
        media_type, b64 = prepare_image(image_path)
    except Exception as e:  # 깨진 파일, 지원하지 않는 형식
        result.error_code, result.error_message = "UNSUPPORTED_MEDIA", str(e)
        return result

    messages = _build_messages(media_type, b64, slot_month)
    parsed: ExtractionOutput | None = None

    # 최대 2번 시도: 1차 호출 → (스키마 위반 시) 오류를 알려주고 1번만 교정 재시도.
    for attempt in (1, 2):
        try:
            message = await _call_model(client, messages)
        except anthropic.BadRequestError as e:
            # 이미지 크기 초과, 잘못된 파라미터 등. 재시도해도 같은 결과라 바로 실패 처리합니다.
            result.error_code, result.error_message = "BAD_REQUEST", e.message
            break
        except anthropic.RateLimitError as e:
            # SDK가 이미 2회 재시도한 뒤에도 429 면 여기로 옵니다. job 레벨에서 다시 시도하게 둡니다.
            result.error_code, result.error_message = "RATE_LIMITED", e.message
            break
        except anthropic.APIStatusError as e:
            result.error_code = "UPSTREAM_UNAVAILABLE" if e.status_code >= 500 else "API_ERROR"
            result.error_message = f"{e.status_code}: {e.message}"
            break
        except anthropic.APIConnectionError as e:
            result.error_code, result.error_message = "NETWORK_ERROR", str(e)
            break

        result.request_id = message._request_id
        result.usage = _usage_dict(message)

        # 안전 분류기 거부. fallbacks="default" 가 이미 한 번 다른 모델로 시도한 뒤의 최종 결과입니다.
        if message.stop_reason == "refusal":
            category = message.stop_details.category if message.stop_details else None
            result.error_code, result.error_message = "REFUSED", f"category={category}"
            break

        # 출력이 max_tokens 에 잘리면 JSON 이 깨집니다. 행이 비정상적으로 많은 캡처일 때 생깁니다.
        if message.stop_reason == "max_tokens":
            result.error_code, result.error_message = "ANALYSIS_OUTPUT_INVALID", "output truncated (max_tokens)"
            break

        try:
            text = _first_text(message)
            parsed = ExtractionOutput.model_validate(json.loads(text))
            break  # 성공
        except (ValueError, ValidationError) as e:
            if attempt == 2:
                result.error_code, result.error_message = "ANALYSIS_OUTPUT_INVALID", str(e)[:500]
                break
            # 1회 교정: 모델의 응답(사고 블록 포함)을 그대로 돌려주고 무엇이 틀렸는지 알려줍니다.
            # 사고 블록을 수정하거나 빼면 안 됩니다(이 모델은 블록이 대화에 묶여 있습니다).
            log.info("schema repair retry asset=%s request_id=%s", asset_id, result.request_id)
            messages = messages + [
                {"role": "assistant", "content": message.content},
                {"role": "user", "content": f"출력이 스키마와 맞지 않습니다: {str(e)[:300]}\n같은 캡처를 다시 읽어 스키마에 맞는 JSON 만 출력하세요."},
            ]

    result.elapsed_sec = round(time.perf_counter() - started, 2)
    if parsed is None:
        return result

    # ---- 후처리 (여기부터는 전부 코드가 결정) ----
    if not parsed.is_transaction_screen or not parsed.rows:
        result.status = "skipped"
        result.error_code = "NOT_TRANSACTION_SCREEN"
        result.app_guess, result.displayed_month = parsed.app_guess, parsed.displayed_month
        return result

    result.status = "succeeded"
    result.app_guess, result.displayed_month = parsed.app_guess, parsed.displayed_month
    result.rows = [postprocess_row(r, i, asset_id, slot_month, parsed.displayed_month) for i, r in enumerate(parsed.rows)]
    return result


# ---------------------------------------------------------------------------
# 7. 후처리 (코드 담당)
# ---------------------------------------------------------------------------


def resolve_date(raw: str, slot_month: str) -> tuple[str, bool]:
    """
    모델이 준 날짜("10-23" 또는 "2026-10-23")를 슬롯 연·월로 확정합니다.
    반환: (YYYY-MM-DD, 슬롯 월과 다른지 여부)

    - 연도가 없으면 슬롯의 연도를 붙입니다. 월이 슬롯과 다르면 그대로 두되 mismatch 를 True 로.
      (예: 10월 슬롯인데 "09-30" 이 보이면 9월 30일로 두고 F04 에서 "9월로 옮길까요?" 제안)
    - 1월 슬롯에 12월 날짜가 보이는 경우처럼 연도가 넘어가는 케이스는 전년도로 보정합니다.
    """
    slot_y, slot_m = (int(x) for x in slot_month.split("-"))
    parts = [int(p) for p in raw.split("-")]
    if len(parts) == 3:
        y, m, d = parts
    else:
        m, d = parts
        y = slot_y
        if m == 12 and slot_m == 1:   # 1월 슬롯에 보이는 12월 → 전년도
            y = slot_y - 1
    try:
        resolved = date(y, m, d)
    except ValueError:
        # 2월 30일 같은 불가능한 날짜 → 슬롯 1일로 두고 플래그. (모델 오독 신호)
        return f"{slot_y:04d}-{slot_m:02d}-01", True
    return resolved.isoformat(), (resolved.year, resolved.month) != (slot_y, slot_m)


def postprocess_row(
    row: ExtractedRow, index: int, asset_id: str, slot_month: str, displayed_month: str
) -> TransactionCandidate:
    """모델 출력 한 행 → 검토 표가 쓰는 거래 후보. 숫자·날짜 확정은 전부 여기서."""
    resolved_date, mismatch = resolve_date(row.date, slot_month)
    flags: list[str] = []
    if mismatch or (displayed_month and displayed_month != slot_month):
        flags.append("month_mismatch")
    if row.confidence == "low":
        flags.append("low_confidence")
    if row.direction == "unknown":
        flags.append("unknown_direction")       # 합계에서 제외하고 사용자 확인 요청 (F04)

    return TransactionCandidate(
        id=f"tx_{uuid.uuid4().hex[:10]}",
        asset_id=asset_id,
        source_row_id=f"{asset_id}:row{index:03d}",   # 원문 추적용. 이미지 삭제 후에도 raw_text 로 남음
        date=resolved_date,
        time=row.time or None,
        merchant=row.merchant.strip()[:100],
        amount_krw=abs(int(row.amount_krw)),
        direction=row.direction,
        kind=row.kind,
        confidence=row.confidence,
        raw_text=row.raw_text,
        flags=flags,
    )


# ---------------------------------------------------------------------------
# 8. job 오케스트레이션 (여러 장 동시 처리)
# ---------------------------------------------------------------------------


async def run_extraction_job(
    assets: list[tuple[str, Path]],
    slot_month: str,
    client: anthropic.AsyncAnthropic | None = None,
    concurrency: int = CONCURRENCY,
) -> ExtractionJobResult:
    """
    assets: [(asset_id, 이미지 경로), ...]  (업로드 단계에서 accepted 된 것만)
    slot_month: "YYYY-MM"

    동작
    - 첫 장을 먼저 보내 시스템 프롬프트 캐시를 만들어 두고, 나머지를 동시에 보냅니다.
      (완전히 동시에 쏘면 캐시가 생기기 전에 출발해 캐시를 못 탑니다)
    - 세마포어로 동시 요청 수를 제한해 429 를 피합니다.
    - 결과는 자산별로 모으고, 전체 상태를 succeeded / partial / failed 로 정리합니다.
    """
    own_client = client is None
    client = client or anthropic.AsyncAnthropic(timeout=REQUEST_TIMEOUT_SEC)
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    sem = asyncio.Semaphore(concurrency)

    async def guarded(asset_id: str, path: Path) -> AssetExtraction:
        async with sem:
            return await extract_one(client, asset_id, path, slot_month)

    try:
        results: list[AssetExtraction] = []
        if assets:
            # 1) 캐시 워밍: 첫 장은 단독으로
            results.append(await guarded(*assets[0]))
            # 2) 나머지는 동시에
            if len(assets) > 1:
                rest = await asyncio.gather(*(guarded(a, p) for a, p in assets[1:]))
                results.extend(rest)
    finally:
        if own_client:
            await client.close()

    ok = sum(1 for r in results if r.status == "succeeded")
    failed = sum(1 for r in results if r.status == "failed")
    if ok == 0:
        status: Literal["succeeded", "partial", "failed"] = "failed"
    elif failed > 0:
        status = "partial"
    else:
        status = "succeeded"      # skipped(결제 화면 아님)만 섞인 경우도 succeeded 로 봅니다

    for r in results:  # 개인정보 없는 운영 로그만 남깁니다 (이미지·원문 제외)
        log.info(
            "asset=%s status=%s rows=%d code=%s request_id=%s %.1fs usage=%s",
            r.asset_id, r.status, len(r.rows), r.error_code, r.request_id, r.elapsed_sec, r.usage,
        )
    return ExtractionJobResult(job_id=job_id, slot_month=slot_month, status=status, assets=results)


def retryable_assets(job: ExtractionJobResult) -> list[str]:
    """partial 일 때 다시 보낼 자산만 고릅니다. (성공·skipped 는 재사용, 실패만 재시도)"""
    return [a.asset_id for a in job.assets if a.status == "failed" and a.error_code not in ("UNSUPPORTED_MEDIA", "BAD_REQUEST")]


# ---------------------------------------------------------------------------
# 9. CLI (로컬 테스트용)
# ---------------------------------------------------------------------------


def _main() -> None:
    ap = argparse.ArgumentParser(description="F03 구조화 추출 로컬 테스트")
    ap.add_argument("folder", help="캡처 이미지가 들어 있는 폴더")
    ap.add_argument("--slot", required=True, help="슬롯 연·월, 예: 2026-10")
    ap.add_argument("--out", default="extraction_result.json", help="결과 JSON 저장 경로")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    folder = Path(args.folder)
    paths = sorted(p for p in folder.iterdir() if p.suffix.lower() in ALLOWED_MEDIA)
    assets = [(f"img_{i+1:02d}", p) for i, p in enumerate(paths)]
    if not assets:
        raise SystemExit(f"no images in {folder}")

    job = asyncio.run(run_extraction_job(assets, args.slot))

    # 사람이 읽기 좋은 요약
    print(f"\njob {job.job_id} → {job.status}  ({len(job.all_rows)} rows from {len(job.assets)} assets)")
    for a in job.assets:
        print(f"  {a.asset_id:8} {a.status:9} rows={len(a.rows):3} app={a.app_guess or '-':10} "
              f"month={a.displayed_month or '-':8} {a.error_code or ''} {a.elapsed_sec}s")
    for r in job.all_rows[:10]:
        print(f"    {r.date} {r.time or '--:--'} {r.merchant[:18]:18} {r.amount_krw:>9,} {r.direction:7} {r.confidence:6} {','.join(r.flags)}")
    if len(job.all_rows) > 10:
        print(f"    ... {len(job.all_rows) - 10} more")

    # 전체 결과 저장 (검토 표 UI 개발·평가 스크립트 입력으로 사용)
    file_of = {aid: p.name for aid, p in assets}   # 평가 스크립트가 정답 파일과 짝짓는 키
    payload = {
        "job_id": job.job_id, "slot_month": job.slot_month, "status": job.status,
        "model": MODEL, "extractor_version": EXTRACTOR_VERSION, "prompt_version": PROMPT_VERSION,
        "effort": EFFORT,
        "assets": [
            {**{k: v for k, v in a.__dict__.items() if k != "rows"},
             "file": file_of.get(a.asset_id, ""),
             "rows": [r.__dict__ for r in a.rows]}
            for a in job.assets
        ],
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsaved → {args.out}")


if __name__ == "__main__":
    _main()
