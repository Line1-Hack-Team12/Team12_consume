"""
F07 가맹점 분류: 가맹점명 → 상위 카테고리 7개 + 하위 태그.

계약은 F03_INTERFACE.md 6절. 요약:
  - 입력 items[] (merchant, kind, raw_text) 최대 100개 → 출력 items[] 같은 순서·개수
  - category 는 7개 enum, subcategory 는 상위별 허용값 또는 null, confidence 는 high/medium/low
  - c2: 기타(other) 하위에 "여가"(PC방·노래방·영화)와 "차량"(주유·주차) 추가. 상위 enum 은 그대로
  - 모델이 허용값 밖을 내면 여기서 other / null / low 로 바꾼다. 백엔드는 enum 검증을 다시 하지 않는다
  - 모델 호출이 아예 실패하면 전부 other / null / low 로 돌려준다 (분류 실패가 분석을 막지 않도록)

F03 과 같은 호출 방식(JSON 스키마 출력, effort low, 시스템 프롬프트 캐시, 서버측 폴백).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError

log = logging.getLogger("f07.classify")

MODEL = os.environ.get("F07_MODEL", os.environ.get("F03_MODEL", "claude-sonnet-5-5"))
EFFORT = "low"
MAX_TOKENS = 6000
BATCH_SIZE = 100
PROMPT_VERSION = "c2"

CATEGORIES = ("food", "delivery", "cafe", "shopping", "transport", "living", "other")
SUBCATEGORIES: dict[str, tuple[str, ...]] = {
    "food": ("외식", "주점"),
    "delivery": (),
    "cafe": (),
    "shopping": ("온라인", "오프라인"),
    "transport": ("택시", "대중교통"),
    "living": ("편의점", "구독", "주거·통신", "의료"),
    "other": ("이체", "여가", "차량", "미분류"),
}
CONFIDENCES = ("high", "medium", "low")

CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "요청 items 의 0부터 시작하는 번호"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "subcategory": {"type": ["string", "null"],
                                    "description": "상위별 허용값 중 하나. 해당 없으면 null. food: 외식|주점, shopping: 온라인|오프라인, transport: 택시|대중교통, living: 편의점|구독|주거·통신|의료, other: 이체|여가|차량|미분류"},
                    "confidence": {"type": "string", "enum": list(CONFIDENCES)},
                },
                "required": ["index", "category", "subcategory", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """당신은 한국 소비 내역의 가맹점명을 카테고리로 분류합니다. 출력은 JSON 스키마를 따릅니다.

상위 카테고리 (category)
- food: 식당, 외식, 술집. 하위 "외식" 또는 "주점"
- delivery: 배달 앱 결제 (배달의민족/(주)우아한형제들, 쿠팡이츠, 요기요 등). 하위 없음
- cafe: 카페, 커피, 디저트 전문점. 하위 없음
- shopping: 쇼핑. 하위 "온라인"(쿠팡, 네이버페이, 무신사 등) 또는 "오프라인"(백화점, 마트, 다이소 등)
- transport: 교통. 하위 "택시"(카카오T, 타다 등) 또는 "대중교통"(지하철, 버스, 티머니, 코레일 등)
- living: 생활. 하위 "편의점"(GS25, CU, 세븐일레븐 등), "구독"(유튜브, 넷플릭스, 멜론, 토스프라임 등 정기결제),
  "주거·통신"(통신요금, 관리비, 공과금, LGU+, KT, SKT), "의료"(병원, 약국)
- other: 위에 없음. 하위 "이체"(송금·이체·간편결제 충전), "여가"(PC방, 노래방, 영화관, 게임, 볼링·당구 등 오락·취미),
  "차량"(주유소, 주차장, 세차, 통행료), "미분류"(무엇인지 알 수 없음)

규칙
- 가맹점명은 법인명으로 올 때가 많습니다 ("(주)우아한형제들" = 배달의민족). 아는 법인명은 서비스 기준으로 분류합니다.
- 지점명·숫자·결제 수단 표기는 무시하고 상호만 봅니다. raw_text 의 결제 수단은 참고만 합니다.
- 애매하면 category 는 가장 가까운 것으로 두고 confidence 를 medium, 정말 모르면 other/미분류/low.
- subcategory 는 그 category 의 허용값만 씁니다. delivery·cafe 는 항상 null.
- 요청 index 마다 정확히 하나씩, 빠짐없이 돌려줍니다."""


class ClassifyItem(BaseModel):
    merchant: str
    kind: str = "expense"
    raw_text: str | None = None


@dataclass
class Classification:
    merchant: str
    category: str
    subcategory: str | None
    confidence: str


class _ModelOut(BaseModel):
    items: list[dict[str, Any]]


def normalize(category: Any, subcategory: Any, confidence: Any) -> tuple[str, str | None, str]:
    """허용값 밖은 조용히 교정한다. 백엔드 쪽 enum 검증을 불필요하게 만드는 게 목적."""
    cat = category if category in CATEGORIES else "other"
    sub = subcategory if (isinstance(subcategory, str) and subcategory in SUBCATEGORIES[cat]) else None
    if cat == "other" and sub is None:
        sub = "미분류"
    conf = confidence if confidence in CONFIDENCES else "low"
    if cat != category:
        conf = "low"
    return cat, sub, conf


FALLBACK = ("other", "미분류", "low")


def _user_message(items: list[ClassifyItem]) -> str:
    lines = []
    for i, it in enumerate(items):
        extra = f" | 참고: {it.raw_text}" if it.raw_text else ""
        lines.append(f"{i}. {it.merchant} ({it.kind}){extra}")
    return "다음 가맹점을 분류하세요.\n" + "\n".join(lines)


async def _call(client: anthropic.AsyncAnthropic, messages: list[dict[str, Any]]) -> anthropic.types.Message:
    return await client.beta.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": CLASSIFY_SCHEMA}},
    )


def _first_text(message: anthropic.types.Message) -> str:
    for block in message.content:
        if block.type == "text":
            return block.text
    raise ValueError("no text block")


async def classify_batch(client: anthropic.AsyncAnthropic, items: list[ClassifyItem]) -> list[Classification]:
    """최대 BATCH_SIZE 개를 한 번에 분류. 어떤 경우에도 items 와 같은 길이·순서로 돌려준다."""
    if not items:
        return []
    assert len(items) <= BATCH_SIZE
    messages: list[dict[str, Any]] = [{"role": "user", "content": _user_message(items)}]
    parsed: _ModelOut | None = None
    for attempt in range(2):                                    # 스키마 실패 1회 교정
        try:
            msg = await _call(client, messages)
        except anthropic.APIError as e:                          # 429, 5xx, 네트워크 등 → 전부 fallback
            log.warning("classify api error type=%s", type(e).__name__)
            return [Classification(it.merchant, *FALLBACK) for it in items]
        if msg.stop_reason == "refusal":
            return [Classification(it.merchant, *FALLBACK) for it in items]
        try:
            text = _first_text(msg)                                   # 사고만 하다 max_tokens 에 끝나면 text 블록이 없음
            parsed = _ModelOut.model_validate_json(text)
            break
        except (ValueError, ValidationError):
            if attempt == 0:
                messages = messages + [{"role": "assistant", "content": msg.content},
                                       {"role": "user", "content": "출력이 스키마와 맞지 않습니다. 스키마에 맞는 JSON 만 다시 출력하세요."}]
    if parsed is None:
        return [Classification(it.merchant, *FALLBACK) for it in items]

    by_index: dict[int, dict[str, Any]] = {}
    for o in parsed.items:
        i = o.get("index")
        if isinstance(i, int) and 0 <= i < len(items) and i not in by_index:
            by_index[i] = o
    out: list[Classification] = []
    for i, it in enumerate(items):
        o = by_index.get(i)
        if o is None:
            out.append(Classification(it.merchant, *FALLBACK))
            continue
        cat, sub, conf = normalize(o.get("category"), o.get("subcategory"), o.get("confidence"))
        out.append(Classification(it.merchant, cat, sub, conf))
    return out


async def classify(items: list[ClassifyItem], client: anthropic.AsyncAnthropic | None = None,
                   concurrency: int = 3) -> list[Classification]:
    """BATCH_SIZE 단위로 나눠 동시 호출. 순서 보존."""
    own = client is None
    client = client or anthropic.AsyncAnthropic(timeout=60.0)
    sem = asyncio.Semaphore(concurrency)
    batches = [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]

    async def run(b: list[ClassifyItem]) -> list[Classification]:
        async with sem:
            return await classify_batch(client, b)

    try:
        results = await asyncio.gather(*(run(b) for b in batches))
    finally:
        if own:
            await client.close()
    return [c for r in results for c in r]


def to_payload(results: list[Classification]) -> dict[str, Any]:
    return {
        "items": [{"merchant": c.merchant, "category": c.category, "subcategory": c.subcategory,
                   "confidence": c.confidence} for c in results],
        "model": MODEL, "prompt_version": PROMPT_VERSION,
    }
