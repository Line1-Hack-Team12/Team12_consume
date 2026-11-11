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
import hashlib
import re
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

MODEL = os.environ.get("F03_MODEL", "claude-sonnet-5-5")
# 캡처 주인의 이름. 온보딩에서 받은 값을 서비스가 호출마다 넘기며, 평가 때는 .env 의 F03_OWNER_NAME 을 씁니다.
# 저장소·로그·결과 파일 어디에도 적지 않습니다 (결과에는 "본인" 표식만 남음).
OWNER_NAME = os.environ.get("F03_OWNER_NAME", "").strip()  # .env 의 F03_MODEL 로 교체 가능. 평가 세트로 claude-opus-5-5 와 비교 예정
EXTRACTOR_VERSION = "f03-0.10"           # 코드 변경 시 올림. 결과에 기록해 재추출 비교에 사용
PROMPT_VERSION = "p7"                   # SYSTEM_PROMPT 수정 시 올림 (p1, p2, ...)
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
        # 캡처에 표시된 월 표기 그대로 ("8월", "2026.10" 등). 후처리에서 숫자만 뽑아 슬롯 월과 비교합니다.
        "displayed_month": {"type": "string"},
        # 행마다 날짜가 없는 화면(달력에서 하루를 고른 일별 보기 등)에서 헤더에 보이는 날짜.
        # 모든 행에 공통 적용됩니다. "DD", "MM-DD", "YYYY-MM-DD" 중 보이는 만큼. 없으면 "".
        "header_date": {
            "type": "string",
            "description": "행마다 날짜가 없을 때 화면 헤더·달력에서 선택된 날짜. DD 또는 MM-DD 또는 YYYY-MM-DD. 없으면 빈 문자열",
        },
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "MM-DD. 캡처에 연도가 있으면 YYYY-MM-DD. 그 행에 날짜가 보이지 않으면 빈 문자열"},
                    "time": {"type": "string", "description": "HH:MM. 없으면 빈 문자열"},
                    "merchant": {"type": "string", "description": "가맹점·상대방 표기 원문. 100자 이내"},
                    "amount_krw": {"type": "integer", "description": "양수 정수 원. 부호는 direction 으로"},
                    "direction": {"type": "string", "enum": ["out", "in", "unknown"]},
                    "kind": {
                        "type": "string",
                        "enum": ["expense", "income", "transfer", "self_transfer", "refund", "cancel", "unknown"],
                    },
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "raw_text": {"type": "string", "description": "그 행에 보이는 글자를 그대로"},
                },
                "required": ["date", "time", "merchant", "amount_krw", "direction", "kind", "confidence", "raw_text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["is_transaction_screen", "app_guess", "displayed_month", "header_date", "rows"],
    "additionalProperties": False,
}


def _check_date_shape(v: str) -> str:
    """허용: "" / "DD" / "MM-DD" / "YYYY-MM-DD". 그 외("10월 23일" 등)는 스키마 오류로 보고 교정 재시도를 유도합니다."""
    if v == "":
        return v
    parts = v.split("-")
    # isascii 를 함께 보는 이유: str.isdigit() 은 "²" 같은 유니코드 숫자도 True 라서 int() 에서 터집니다.
    if len(parts) not in (1, 2, 3) or not all(p.isascii() and p.isdigit() for p in parts):
        # 메시지에 입력값을 넣지 않습니다 (오류 문자열이 error_message 로 저장되므로 원문이 새지 않게)
        raise ValueError("date must be '', DD, MM-DD or YYYY-MM-DD")
    return v


class ExtractedRow(BaseModel):
    """모델이 읽어낸 거래 한 행. 아직 '후보'이며 후처리 전입니다."""

    date: str
    time: str
    merchant: str = Field(max_length=100)
    amount_krw: int = Field(ge=0)
    direction: Literal["out", "in", "unknown"]
    kind: Literal["expense", "income", "transfer", "self_transfer", "refund", "cancel", "unknown"]
    confidence: Literal["high", "medium", "low"]
    raw_text: str

    @field_validator("date")
    @classmethod
    def _date_shape(cls, v: str) -> str:
        return _check_date_shape(v)


class ExtractionOutput(BaseModel):
    is_transaction_screen: bool
    app_guess: str
    displayed_month: str
    header_date: str = ""          # 행에 날짜가 없는 화면의 공통 날짜 (없으면 "")
    rows: list[ExtractedRow]

    @field_validator("header_date")
    @classmethod
    def _header_date_shape(cls, v: str) -> str:
        return _check_date_shape(v)


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
  본인 계좌 사이의 이동("내 계좌 이체", 양쪽이 모두 본인 계좌)은 "self_transfer",
  "환불"/"취소" 표기가 있으면 각각 "refund"/"cancel", 모르면 "unknown".
- 날짜에 연도가 없으면 MM-DD 로만 적습니다. 연도를 지어내지 마세요. 사용자 메시지에 이 캡처가
  속한 연·월 힌트가 있으니 displayed_month 는 화면에 실제로 보이는 월 표기를 기준으로 적습니다.
- 날짜의 우선순위: (1) 행 자체에 찍힌 날짜("10.26", "26일 월요일", "10월 26일" 등 그 행 안이나 행 바로 옆에 붙은 표기)
  → 반드시 그 행의 date 에 적습니다. 행에 날짜 표기가 있는데 비워 두면 안 됩니다. (2) 행 위에 있는 날짜 구분 헤더
  ("26일 월요일" 같은 줄 아래로 그 날의 행들이 이어지는 형식) → 그 헤더 아래 행들의 date 에 그 날짜를 적습니다.
  (3) 행에도 구분 헤더에도 없고 화면 상단·달력에서 고른 날짜 하나만 있는 일별 보기 → 각 행의 date 를 "" 로 두고
  그 날짜를 header_date 에 적습니다 (일만 보이면 "DD", 월·일이 보이면 "MM-DD").
  행에 날짜가 있는 보통 목록 화면에서는 header_date 를 "" 로 둡니다. 어디에도 날짜가 없으면 둘 다 "" 로 두고 추측하지 않습니다.
- 시각이 없는 행은 time 을 "" 로 둡니다.
- merchant 는 화면 표기 그대로(법인명, 지점명 포함) 적습니다. 카테고리로 바꾸지 마세요.
- raw_text 에는 그 행에 보이는 글자를 순서대로 그대로 옮깁니다.
- 계좌번호, 카드번호, 잔액은 어떤 필드에도 넣지 않습니다.
- 사람 이름(송금·입금 상대방, 통장 주인)은 개인정보라 추출하지 않습니다. 상대방이 사람 이름이면
  merchant 를 "개인" 으로 적고 kind 는 "transfer" 로 둡니다. raw_text 에서도 이름 자리를 "개인" 으로 바꿉니다.
  상호·법인·서비스명(카카오페이, 네이버파이낸셜, OO식당)은 이름이 아니므로 그대로 적습니다.
  이름 뒤에 괄호 설명이 붙어도("홍길동(모임통장)", "김철수(생활비)") 사람 이름이므로 "개인" 으로 적습니다.
  "내 OO 통장 → 상대" 의 상대가 금융기관·가맹점·서비스가 아니면 사람으로 보고 "개인" 으로 적습니다.
- 사용자 메시지에 이 캡처 주인의 이름이 주어지면, 그 이름이 보이는 자리는 "개인" 대신 "본인" 으로 적습니다
  (merchant 와 raw_text 모두). 이름 자체는 어디에도 적지 않습니다. 상대가 "본인" 이고 나머지 한쪽이 본인 계좌면
  kind 는 "self_transfer" 입니다. 단 "홍길동(모임통장)" 처럼 괄호 설명이 붙은 계좌는 본인 명의라도 공동 자금이므로
  "개인" + "transfer" 로 둡니다. "홍*동" 처럼 가려진 이름은 성과 끝 글자가 주인 이름과 같으면 "본인" 으로 보되
  confidence 를 "medium" 으로 둡니다.

자주 틀리는 지점 (특히 주의)
- 숫자 0 과 8, 1 과 7 을 혼동하기 쉽습니다. 쉼표 위치로 자릿수를 다시 확인하세요.
- 한 행이 두 줄로 표시되는 앱이 있습니다(가맹점명 줄 + 금액/시각 줄). 두 줄을 한 행으로 합칩니다.
- 화면 맨 위나 아래에 잘린 행은 금액이 온전히 보일 때만 포함하고 confidence 를 "low" 로 둡니다.
  금액이 안 보이는 행(시각이나 잔액만 남은 조각)은 아예 포함하지 않습니다. amount_krw 0 은 쓰지 않습니다.
- 목록이 최신순이고 맨 위 행들 위에 날짜 구분 헤더가 잘려 안 보이고 행 자체에도 날짜가 없으면 그 행들의 date 는 "" 로 둡니다.
  아래 헤더 날짜를 빌려 쓰지 않습니다. 단, 행 자체에 날짜가 찍혀 있으면 헤더가 안 보여도 그 날짜를 적습니다.

결제 내역 화면이 아니면(홈 화면, 광고, 다른 앱) is_transaction_screen 을 false 로 두고 rows 는 빈 배열로 둡니다.
거래 목록 전체가 아니라 일부만 추려 보여주는 요약 화면도 false 로 둡니다: 월간 달력·카테고리별 합계, "고정지출", "구독",
"통신요금" 처럼 특정 묶음만 모아 놓은 화면, 정기결제 관리 화면. 이런 화면의 항목은 전체 거래 목록에 이미 들어 있어
따로 추출하면 중복됩니다. 반면 전체 거래 목록을 날짜·검색어로 걸러 본 화면은 거래 화면입니다."""


def _user_prompt(slot_month: str, owner_name: str = "") -> str:
    """호출마다 달라지는 부분. 슬롯의 연·월과 캡처 주인의 이름(있으면)을 힌트로 줍니다.

    이름은 시스템 프롬프트(캐시 대상)가 아니라 여기에 넣습니다. 모델은 이름을 출력하지 않고 "본인" 표식만 냅니다.
    """
    y, m = slot_month.split("-")
    text = (
        f"이 캡처는 {int(y)}년 {int(m)}월 슬롯에 올라온 결제 내역입니다. "
        "화면에 보이는 거래 행을 모두 추출하세요."
    )
    if owner_name:
        text += f' 이 캡처 주인의 이름은 "{owner_name}" 입니다. 이 이름이 보이는 자리는 "본인" 으로 적고 이름은 적지 마세요.'
    return text


# ---------------------------------------------------------------------------
# 4. 결과 데이터 구조 (후처리 이후, 다른 모듈이 쓰는 형태)
# ---------------------------------------------------------------------------


@dataclass
class TransactionCandidate:
    """후처리가 끝난 거래 후보 한 건. F04 검토 표의 입력입니다."""

    id: str
    asset_id: str
    source_row_id: str
    date: str                      # YYYY-MM-DD (슬롯 연·월로 확정됨). 날짜를 못 읽은 행은 "" + flags "date_missing"
    time: str | None               # HH:MM 또는 None
    merchant: str
    amount_krw: int
    direction: str                 # out / in / unknown
    kind: str
    confidence: str                # high / medium / low (모델 참고값. 화면에 정확도로 표시하지 않음)
    raw_text: str
    flags: list[str] = field(default_factory=list)
    # F04 검토 큐 게이트 대상: date_missing, merchant_missing(필수값 누락) / low_confidence / month_mismatch / unknown_direction
    # 정보성(게이트 아님): kind_conflict


@dataclass
class AssetExtraction:
    """이미지 한 장의 추출 결과."""

    asset_id: str
    status: Literal["succeeded", "failed", "skipped"]
    rows: list[TransactionCandidate] = field(default_factory=list)
    app_guess: str = ""
    displayed_month: str = ""
    error_code: str | None = None       # NOT_TRANSACTION_SCREEN / ANALYSIS_OUTPUT_INVALID / OUTPUT_TRUNCATED / REFUSED / RATE_LIMITED / API_ERROR / INTERNAL ...
    error_message: str | None = None
    request_id: str | None = None
    elapsed_sec: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)


JobStatus = Literal["succeeded", "partial", "failed", "no_transactions"]


@dataclass
class ExtractionJobResult:
    """
    여러 장을 묶은 job 결과. 기능명세서의 queued/running/succeeded/partial/failed 에 대응.
    no_transactions: 실패는 없는데 모든 장이 결제 화면이 아니어서(skipped) 거래가 0건. 재시도가 아니라 캡처 교체 안내.
    """

    job_id: str
    slot_month: str
    status: JobStatus
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


def _build_messages(media_type: str, b64: str, slot_month: str, owner_name: str = "") -> list[dict[str, Any]]:
    """이미지 블록을 먼저, 지시 텍스트를 뒤에 두는 것이 비전 요청의 기본 형태입니다."""
    return [
        {
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": _user_prompt(slot_month, owner_name)},
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
    owner_name: str | None = None,
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

    owner_name = OWNER_NAME if owner_name is None else owner_name
    messages = _build_messages(media_type, b64, slot_month, owner_name)
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
            # 같은 설정으로 다시 보내도 같은 결과라 재시도 대상이 아닙니다 (사용자에게 캡처 교체 안내)
            result.error_code, result.error_message = "OUTPUT_TRUNCATED", "output truncated (max_tokens)"
            break

        try:
            text = _first_text(message)
            parsed = ExtractionOutput.model_validate(json.loads(text))
            break  # 성공
        except (ValueError, ValidationError) as e:
            if attempt == 2:
                result.error_code, result.error_message = "ANALYSIS_OUTPUT_INVALID", summarize_validation_error(e)[:500]
                break
            # 1회 교정: 모델의 응답(사고 블록 포함)을 그대로 돌려주고 무엇이 틀렸는지 알려줍니다.
            # 사고 블록을 수정하거나 빼면 안 됩니다(이 모델은 블록이 대화에 묶여 있습니다).
            log.info("schema repair retry asset=%s request_id=%s", asset_id, result.request_id)
            messages = messages + [
                {"role": "assistant", "content": message.content},
                {"role": "user", "content": f"출력이 스키마와 맞지 않습니다: {summarize_validation_error(e)[:300]}\n같은 캡처를 다시 읽어 스키마에 맞는 JSON 만 출력하세요."},
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
    # 금액이 0 인 행은 잘린 조각(시각·잔액만 보이는 행)이라 거래로 볼 수 없어 버립니다.
    # 가맹점과 방향이 둘 다 없는 행도 같은 조각입니다: 실측에서 카카오뱅크 맨 위 잘린 행이 한 번은 금액 0,
    # 다른 실행에서는 잔액(-2,095,864)을 금액으로 들고 나왔습니다. 검토 큐에 올려도 지울 수밖에 없는 행이라 버립니다.
    header_date = effective_header_date(parsed.header_date, [r.date for r in parsed.rows])
    rows = [postprocess_row(r, i, asset_id, slot_month, parsed.displayed_month, header_date, owner_name)
            for i, r in enumerate(parsed.rows) if r.amount_krw != 0]
    kept = [t for t in rows if not is_fragment(t)]
    dropped = len(parsed.rows) - len(kept)
    if dropped:
        log.info("drop fragment rows asset=%s n=%d", asset_id, dropped)
    result.rows = kept
    return result


# ---------------------------------------------------------------------------
# 7. 후처리 (코드 담당)
# ---------------------------------------------------------------------------


def resolve_date(raw: str, slot_month: str) -> tuple[str, bool]:
    """
    모델이 준 날짜("10-23" 또는 "2026-10-23")를 슬롯 연·월로 확정합니다.
    반환: (YYYY-MM-DD 또는 "", 슬롯 월과 다른지 여부)

    - 연도가 없으면 슬롯의 연도를 붙입니다. 월이 슬롯과 다르면 그대로 두되 mismatch 를 True 로.
      (예: 10월 슬롯인데 "09-30" 이 보이면 9월 30일로 두고 F04 에서 "9월로 옮길까요?" 제안)
    - 연도 경계: 1월 슬롯의 12월 → 전년도, 12월 슬롯의 1월 → 다음 해.
    - 읽을 수 없는 값("2026-10" 처럼 일이 없음, "02-30", 숫자가 아님, 범위 밖)은 날짜를 지어내지 않고
      "" 를 돌려줍니다. 호출자가 date_missing 으로 F04 검토 큐에 올립니다.
      (예전에는 슬롯 1일로 채웠는데, 존재하지 않는 거래가 요일·급여일 탐지에 섞여 들어가서 없앴습니다)
    """
    slot_y, slot_m = (int(x) for x in slot_month.split("-"))
    try:
        parts = [int(p) for p in raw.split("-")]
    except ValueError:
        return "", False
    if len(parts) == 3:
        y, m, d = parts
    elif len(parts) == 2:
        m, d = parts
        y = slot_y
        if m == 12 and slot_m == 1:    # 1월 슬롯에 보이는 12월 → 전년도
            y = slot_y - 1
        elif m == 1 and slot_m == 12:  # 12월 슬롯에 보이는 1월 → 다음 해
            y = slot_y + 1
    elif len(parts) == 1:              # "DD": 일만 보이는 화면(일별 보기 헤더) → 슬롯 연·월
        (d,) = parts
        y, m = slot_y, slot_m
    else:
        return "", False
    if y < 2000:                       # "26-10-07" 처럼 두 자리 연도는 지어내지 않고 검토로
        return "", False
    try:
        resolved = date(y, m, d)
    except (ValueError, OverflowError):
        return "", False
    return resolved.isoformat(), (resolved.year, resolved.month) != (slot_y, slot_m)


def normalize_time(raw: str) -> str | None:
    """
    모델이 적은 시각을 HH:MM 으로 통일합니다. "9:07", "09:07:55", "오후 2:15" 같은 변형을 흡수하고,
    시각으로 읽을 수 없으면 None. (모델마다 초 단위를 붙이거나 빼는 편차가 있어 코드에서 고정)
    """
    if not raw:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", raw)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if "오후" in raw and h < 12:
        h += 12
    if "오전" in raw and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return f"{h:02d}:{mi:02d}"


# 프롬프트가 이미 넣지 말라고 하지만, 모델 순응에만 맡기지 않도록 코드에서 한 번 더 지웁니다.
# 순서가 중요합니다: 카드(4-4-4-4) → 전화(0xx-xxxx-xxxx) → 계좌(하이픈 묶음, 숫자 9자리 이상) → 긴 숫자열 → 잔액.
_RE_CARD = re.compile(r"(?<![\d*])(?:[\d*]{4}[ -]){3}[\d*]{4}(?![\d*])")
_RE_PHONE = re.compile(r"(?<!\d)0\d{1,2}-\d{3,4}-\d{4}(?!\d)")
_RE_ACCOUNT = re.compile(r"(?<![\d*])(?:[\d*]{1,7}-){1,3}[\d*]{2,}(?![\d*])")   # 그룹 첫 글자가 * 여도 잡음 (마스킹 계좌)
_RE_LONG_DIGITS = re.compile(r"(?<![\d,])\d{11,}(?![\d,])")                    # 하이픈 없는 계좌·카드. 금액은 쉼표가 있어 안 걸림
# 사람 이름 + 통장 설명 괄호: "이아림(모임통장)" → "개인". 실측에서 프롬프트 규칙만으로는 새어 나옴.
_RE_NAME_WITH_ACCOUNT_NOTE = re.compile(r"(?<![가-힣])[가-힣]{2,4}\s*\((?:모임통장|통장|계좌|생활비|용돈|회비)\)")
_RE_BALANCE = re.compile(r"잔액\s*[:：]?\s*[-+]?[\d,]+\s*원?")


def _account_if_enough_digits(m: "re.Match[str]") -> str:
    """하이픈 묶음 중 숫자·마스킹 글자가 9개 이상인 것만 계좌로 봅니다. 날짜(2026-10-30), 지점 표기(1-2호점)는 보존."""
    token = m.group(0)
    return "[계좌]" if sum(ch.isdigit() or ch == "*" for ch in token) >= 9 else token


def scrub_sensitive(text: str) -> str:
    """
    merchant / raw_text 에서 카드번호·전화번호·계좌번호·긴 숫자열·잔액 표기를 제거합니다. 거래 식별에 필요 없는 값들입니다.
    한계(알고 있는 것): 라벨 없이 두 번째 금액으로만 찍힌 잔액("4,500원 1,234,567원")은 금액과 구분할 수 없어 남습니다.
    이건 프롬프트("잔액은 어떤 필드에도 넣지 않음")에 맡기고, 평가 세트에서 raw_text 의 '원' 개수를 점검합니다.
    주문번호·ISBN 같은 긴 번호도 함께 지워지는데, 가맹점 식별에는 필요 없는 값이라 감수합니다.
    """
    if not text:
        return text
    text = _RE_NAME_WITH_ACCOUNT_NOTE.sub("개인", text)
    text = _RE_CARD.sub("[카드]", text)
    text = _RE_PHONE.sub("[전화]", text)
    text = _RE_ACCOUNT.sub(_account_if_enough_digits, text)
    text = _RE_LONG_DIGITS.sub("[번호]", text)
    text = _RE_BALANCE.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


# kind 와 direction 의 정합성. income 은 in, expense 는 out 이어야 합니다.
# refund / cancel 은 앱마다 표기가 갈립니다(카드사 앱은 "-4,500원 취소" 처럼 음수로, 은행 앱은 "+" 로) → 어느 방향이든 허용.
# transfer 도 양방향. 모순이면 kind 를 지우지 않고 kind_conflict 플래그만 붙입니다 (지우면 환불이 지출로 집계될 수 있음).
# F07 집계 규칙: refund / cancel 은 direction 과 무관하게 "지출 차감" 으로 다룹니다.
# self_transfer(본인 계좌 간 이동)도 양방향. F07 집계 규칙: self_transfer 는 수입·지출 모두에서 제외.
_KIND_REQUIRES: dict[str, str] = {"income": "in", "expense": "out"}


_SELF_TRANSFER_MARKERS = ("내 계좌 이체", "내계좌이체", "내 계좌로 이체", "내 계좌 간 이체")


def scrub_owner_name(text: str, owner_name: str) -> tuple[str, bool]:
    """캡처 주인의 이름이 결과 문자열에 남아 있으면 "본인" 으로 바꿉니다 (프롬프트가 어겨도 저장 데이터엔 이름 없음).

    "홍*동", "홍**" 처럼 가운데가 가려진 표기도 잡되, 그 경우는 (text, True) 로 알려 호출자가 confidence 를 낮춥니다.
    두 글자 이름은 가려진 표기를 매칭하지 않습니다 (성만 같아도 걸려 과잉 치환).
    """
    if not text or not owner_name:
        return text, False
    out = text.replace(owner_name, "본인")
    masked = False
    if len(owner_name) >= 3:
        pat = re.escape(owner_name[0]) + r"[*○●]{1,3}" + "(?:" + re.escape(owner_name[-1]) + ")?"
        new = re.sub(pat, "본인", out)
        masked = new != out
        out = new
    return out, masked


def infer_self_transfer(kind: str, merchant: str, raw_text: str) -> str:
    """앱이 "내 계좌 이체" 라고 명시한 행은 모델 판단과 무관하게 self_transfer 로 둡니다 (결정적 규칙)."""
    if kind in ("refund", "cancel"):
        return kind
    text = f"{merchant} {raw_text}"
    if any(m in text for m in _SELF_TRANSFER_MARKERS):
        return "self_transfer"
    if "(" not in text and "본인 →" in text and "→ 본인" in text:    # 양쪽이 본인 (괄호 설명 있는 모임통장 제외)
        return "self_transfer"
    return kind


def reconcile_kind(kind: str, direction: str) -> tuple[str, bool]:
    """kind 와 direction 이 모순인지 판단. kind 는 보존하고 (kind, 모순 여부) 를 돌려줍니다."""
    need = _KIND_REQUIRES.get(kind)
    conflict = bool(need) and direction != "unknown" and direction != need
    return kind, conflict


def summarize_validation_error(e: Exception) -> str:
    """pydantic 오류를 입력값 없이 요약합니다. 기본 str(e) 에는 input_value 로 가맹점명 원문이 들어갑니다."""
    if isinstance(e, ValidationError):
        items = e.errors(include_input=False, include_url=False)[:5]
        return "; ".join(f"{'.'.join(str(x) for x in it['loc'])}: {it['msg']}" for it in items)
    return type(e).__name__ + ": invalid JSON"


def parse_displayed_month(displayed_month: str) -> tuple[int | None, int | None]:
    """모델이 적은 표시 월("8월", "2026.10", "10월 내역", "2026년 10월", "2026-10") → (연도 또는 None, 월 또는 None)."""
    if not displayed_month:
        return None, None
    ym = re.search(r"(20\d{2})\D{0,3}(\d{1,2})", displayed_month)      # 2026.10 / 2026-10 / 2026년 10월
    if ym and 1 <= int(ym.group(2)) <= 12:
        return int(ym.group(1)), int(ym.group(2))
    y_only = re.search(r"(20\d{2})", displayed_month)
    m = re.search(r"(\d{1,2})\s*월", displayed_month) or re.fullmatch(r"\s*(\d{1,2})\s*", displayed_month)
    month = int(m.group(1)) if m and 1 <= int(m.group(1)) <= 12 else None
    return (int(y_only.group(1)) if y_only else None), month


def displayed_month_mismatch(displayed_month: str, slot_month: str) -> bool:
    """표시 월이 슬롯과 다른지. 연도가 보이면 연도까지, 아니면 월만 비교. 월을 못 읽으면 False."""
    slot_y, slot_m = (int(x) for x in slot_month.split("-"))
    y, m = parse_displayed_month(displayed_month)
    if m is None:
        return False
    if y is not None:
        return (y, m) != (slot_y, slot_m)
    return m != slot_m


def effective_header_date(header_date: str, row_dates: list[str]) -> str:
    """header_date 는 어느 행에도 날짜가 없는 일별 보기에서만 씁니다.

    실측: 날짜 헤더가 있는 토스 목록 화면에서 모델이 상단 달력의 선택일("07")을 header_date 에 넣는 일이
    실행에 따라 생겼고, 그러면 헤더가 잘려 날짜를 알 수 없는 맨 위 행들에 07 이 붙어 지어낸 날짜가 됐습니다.
    행 하나라도 날짜를 가진 화면이면 header_date 를 버립니다.
    """
    return "" if any(row_dates) else header_date


def is_fragment(tx: "TransactionCandidate") -> bool:
    """가맹점도 방향도 없는 행. 화면 가장자리에 걸려 금액(또는 잔액)만 보이는 조각입니다."""
    return "merchant_missing" in tx.flags and tx.direction == "unknown"


_RE_WON_AMOUNT = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+원|[-+]?\d+원")


def strip_other_amounts(text: str, amount_krw: int) -> str:
    """raw_text 에서 거래 금액이 아닌 '…원' 토큰을 지웁니다.

    은행 앱은 행마다 거래 후 잔액을 라벨 없이 두 번째 금액으로 찍습니다("개인 5,750원 14:15 -1,934,364원").
    잔액은 거래 식별에 필요 없고 자산 규모가 드러나는 값이라, 거래 금액과 다른 금액 토큰은 모두 지웁니다.
    """
    def keep_or_drop(m: re.Match[str]) -> str:
        digits = re.sub(r"[^\d]", "", m.group(0))
        return m.group(0) if digits and int(digits) == amount_krw else ""
    return re.sub(r"\s{2,}", " ", _RE_WON_AMOUNT.sub(keep_or_drop, text)).strip()


def _attach_displayed_month(raw_date: str, displayed_month: str) -> str:
    """일(DD)만 있는 날짜에 화면 표시 달(·연)을 붙입니다.

    일별 보기 화면은 헤더에 "5일" 만 있고 달은 화면 상단 "10월" 에 있습니다. 전에는 슬롯 달을 붙여
    10월 캡처를 11월 슬롯에 올리면 11-05 라는 화면에 없는 날짜가 만들어졌습니다 (불일치 플래그는 떴지만
    날짜 자체가 틀림). 화면에 달이 있으면 그 달을 붙이고, 슬롯과의 불일치 판정은 resolve_date 에 맡깁니다.
    """
    if not raw_date or "-" in raw_date:
        return raw_date
    y, m = parse_displayed_month(displayed_month)
    if m is None:
        return raw_date
    return f"{y}-{m:02d}-{raw_date}" if y is not None else f"{m:02d}-{raw_date}"


def postprocess_row(
    row: ExtractedRow, index: int, asset_id: str, slot_month: str, displayed_month: str, header_date: str = "",
    owner_name: str = "",
) -> TransactionCandidate:
    """모델 출력 한 행 → 검토 표가 쓰는 거래 후보. 숫자·날짜 확정은 전부 여기서."""
    flags: list[str] = []
    raw_date = _attach_displayed_month(row.date or header_date, displayed_month)   # 행에 날짜가 없으면 헤더 날짜(일별 보기)
    resolved_date, mismatch = resolve_date(raw_date, slot_month) if raw_date else ("", False)
    if not resolved_date:
        # 행에도 헤더에도 날짜가 없거나 읽을 수 없음 → 지어내지 않고 빈 값. F04 검토 큐에서 "필수값 누락"으로
        # 사용자가 입력해야 확정할 수 있음 (가짜 날짜를 넣으면 실제 거래와 섞여 보여서 더 나쁨)
        flags.append("date_missing")
    # 헤더 월 불일치를 행에 반영하는 규칙
    #  - 행 날짜가 없어 헤더에 기댄 행: 헤더 월 불일치를 그대로 반영
    #  - 행 날짜에 연도가 없고 헤더에 연도가 있는 행: 헤더 연도로 연도 불일치를 잡음
    #    (작년 10월 캡처를 올해 10월 슬롯에 올리는 경우. 캡처 행에는 연도가 없어 이 길로만 잡힘)
    #  - 행 날짜가 슬롯 월과 맞는데 헤더 월만 다른 경우(두 달 걸친 캡처): 그 행은 정상
    header_y, _ = parse_displayed_month(displayed_month)
    row_has_year = len(row.date.split("-")) == 3 if row.date else False
    slot_y = int(slot_month.split("-")[0])
    header_year_mismatch = resolved_date != "" and not row_has_year and header_y is not None and header_y != slot_y
    if mismatch or header_year_mismatch or (not row.date and displayed_month_mismatch(displayed_month, slot_month)):
        flags.append("month_mismatch")
    merchant, masked_m = scrub_owner_name(scrub_sensitive(row.merchant)[:100], owner_name)
    raw_text, masked_r = scrub_owner_name(strip_other_amounts(scrub_sensitive(row.raw_text), row.amount_krw), owner_name)
    confidence = row.confidence
    if (masked_m or masked_r) and confidence == "high":
        confidence = "medium"                   # 가려진 이름을 본인으로 본 것은 추정이라 한 단계 낮춤
    if not merchant:
        flags.append("merchant_missing")        # 필수값 누락 (F04 에서 그대로 두기 불가)
    kind, conflict = reconcile_kind(infer_self_transfer(row.kind, merchant, raw_text), row.direction)
    if conflict:
        flags.append("kind_conflict")           # 정보성. F04 게이트 대상은 아님
    if confidence == "low":
        flags.append("low_confidence")
    if row.direction == "unknown" and kind != "self_transfer":
        flags.append("unknown_direction")       # 합계에서 제외하고 사용자 확인 요청 (F04)
        # self_transfer 는 방향과 무관하게 집계 제외라 확인을 요구하지 않음 (부호 없는 "A → B" 행이 흔함)

    # id 는 (자산, 행 순서, 슬롯) 으로 결정됩니다. 같은 자산을 재추출하면 같은 id 가 나와서
    # 재시도 병합 시 중복 행이 생기지 않고, 달 추가 시 구조화 행 재사용에도 안전합니다.
    source_row_id = f"{asset_id}:row{index:03d}"   # 원문 추적용. 이미지 삭제 후에도 raw_text 로 남음
    row_id = "tx_" + hashlib.sha1(f"{source_row_id}|{slot_month}".encode()).hexdigest()[:10]

    return TransactionCandidate(
        id=row_id,
        asset_id=asset_id,
        source_row_id=source_row_id,
        date=resolved_date,
        time=normalize_time(row.time),
        merchant=merchant,
        amount_krw=abs(int(row.amount_krw)),
        direction=row.direction,
        kind=kind,
        confidence=confidence,
        raw_text=raw_text,
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
    owner_name: str | None = None,
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
            try:
                return await extract_one(client, asset_id, path, slot_month, owner_name)
            except Exception as e:  # noqa: BLE001  예상 못한 예외도 "이 장만 실패" 로 가두어 job 전체를 지킵니다
                # log.exception 은 트레이스백 끝에 str(e) 를 찍어 원문이 섞일 수 있어 타입명만 남깁니다
                log.error("unexpected error asset=%s type=%s", asset_id, type(e).__name__)
                return AssetExtraction(asset_id=asset_id, status="failed",
                                       error_code="INTERNAL", error_message=type(e).__name__)

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

    status = job_status(results)

    for r in results:  # 개인정보 없는 운영 로그만 남깁니다 (이미지·원문 제외)
        log.info(
            "asset=%s status=%s rows=%d code=%s request_id=%s %.1fs usage=%s",
            r.asset_id, r.status, len(r.rows), r.error_code, r.request_id, r.elapsed_sec, r.usage,
        )
    return ExtractionJobResult(job_id=job_id, slot_month=slot_month, status=status, assets=results)


def job_status(results: list[AssetExtraction]) -> JobStatus:
    """자산 상태들 → job 상태. 성공이 하나라도 있고 실패도 있으면 partial, 실패만 있으면 failed,
    실패 없이 전부 skipped 면 no_transactions (재시도할 것이 없으므로 failed 와 구분)."""
    ok = sum(1 for r in results if r.status == "succeeded")
    failed = sum(1 for r in results if r.status == "failed")
    if not results:
        return "failed"
    if ok == 0 and failed == 0:
        return "no_transactions"
    if ok == 0:
        return "failed"
    if failed > 0:
        return "partial"
    return "succeeded"           # skipped 가 섞여 있어도 성공 (그 장만 제외)


# 재시도해도 같은 결과가 나오는 실패. 사용자에게 "다른 캡처로 교체" 를 안내합니다.
NON_RETRYABLE_CODES = frozenset({"UNSUPPORTED_MEDIA", "BAD_REQUEST", "REFUSED", "OUTPUT_TRUNCATED"})
# 호출자(API 계층)가 지킬 재시도 상한. RATE_LIMITED 가 반복되면 이 횟수 뒤에는 "잠시 후 다시" 안내로 끝냅니다.
MAX_RETRY_ROUNDS = 2


def merge_retry(original: ExtractionJobResult, retry: ExtractionJobResult) -> ExtractionJobResult:
    """
    partial 재시도 결과를 원 job 에 병합합니다. 재시도한 자산만 새 결과로 교체하고, 나머지(성공·skipped)는
    그대로 둡니다. job_id 는 원본을 유지해 클라이언트가 같은 job 을 계속 폴링할 수 있습니다.
    불변식: 자산 id 집합이 늘지 않고, 성공했던 자산의 행은 바뀌지 않습니다 → 거래 후보 수가 중복으로 늘지 않습니다.
    """
    if retry.slot_month != original.slot_month:
        raise ValueError("retry slot_month differs from original job")   # 행 id 체계가 달라 섞이면 안 됨
    retried = {a.asset_id: a for a in retry.assets}
    unknown = set(retried) - {a.asset_id for a in original.assets}
    if unknown:
        raise ValueError(f"retry contains assets not in original job: {sorted(unknown)}")
    protected = {a.asset_id for a in original.assets if a.status == "succeeded" and a.asset_id in retried}
    if protected:
        # 성공한 자산은 재시도 대상이 아닙니다. 사용자가 F04 에서 고친 행이 있을 수 있어 덮어쓰지 않습니다.
        raise ValueError(f"retry would overwrite succeeded assets: {sorted(protected)}")
    merged = [retried.get(a.asset_id, a) for a in original.assets]
    return ExtractionJobResult(job_id=original.job_id, slot_month=original.slot_month,
                               status=job_status(merged), assets=merged)


def job_to_payload(job: ExtractionJobResult, file_of: dict[str, str] | None = None) -> dict[str, Any]:
    """job 결과 → 저장·API 응답용 dict. 모델·버전을 함께 기록해 나중에 재추출 결과와 비교할 수 있게 합니다."""
    file_of = file_of or {}
    return {
        "job_id": job.job_id, "slot_month": job.slot_month, "status": job.status,
        "model": MODEL, "extractor_version": EXTRACTOR_VERSION, "prompt_version": PROMPT_VERSION,
        "effort": EFFORT,
        "assets": [
            {**{k: v for k, v in a.__dict__.items() if k != "rows"},
             "file": file_of.get(a.asset_id, ""),            # 평가 스크립트가 정답 파일과 짝짓는 키
             "rows": [r.__dict__ for r in a.rows]}
            for a in job.assets
        ],
    }


def job_from_payload(payload: dict[str, Any]) -> ExtractionJobResult:
    """job_to_payload 의 역변환. 재시도 병합(merge_retry)을 위해 저장해 둔 JSON 을 되살립니다."""
    assets: list[AssetExtraction] = []
    for a in payload.get("assets", []):
        rows = [TransactionCandidate(**{k: r.get(k) for k in TransactionCandidate.__dataclass_fields__})
                for r in a.get("rows", [])]
        fields = {k: a.get(k) for k in AssetExtraction.__dataclass_fields__ if k != "rows" and k in a}
        assets.append(AssetExtraction(**fields, rows=rows))
    return ExtractionJobResult(job_id=payload["job_id"], slot_month=payload["slot_month"],
                               status=payload["status"], assets=assets)


def retryable_assets(job: ExtractionJobResult) -> list[str]:
    """partial/failed 일 때 다시 보낼 자산만 고릅니다. (성공·skipped 는 재사용, 일시 오류만 재시도)"""
    return [a.asset_id for a in job.assets if a.status == "failed" and a.error_code not in NON_RETRYABLE_CODES]


def replace_needed_assets(job: ExtractionJobResult) -> list[str]:
    """재시도로는 해결되지 않아 사용자가 다른 캡처로 바꿔야 하는 자산."""
    return [a.asset_id for a in job.assets if a.status == "failed" and a.error_code in NON_RETRYABLE_CODES]


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
    payload = job_to_payload(job, {aid: p.name for aid, p in assets})
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsaved → {args.out}")


if __name__ == "__main__":
    _main()
