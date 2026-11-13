"""
F14 알림 문구 생성: 카드 토글을 켜거나 태그를 바꿀 때 보여줄 알림 미리보기 (title + body).

계약은 F03_INTERFACE.md 8절. 요약:
  - 입력: tone, pattern(수치만), cause_tags(F13 허용값), scheduled_time
  - 출력: title ≤ 20자, body ≤ 80자, notice(고정 문장, 서비스 코드가 붙임)
  - 숫자는 pattern 의 수치만 (narrate 와 같은 검증). 길이 초과·숫자 불일치는 1회 교정, 그래도 틀리면 NotifyInvalid
  - 태그 맥락("야근하고 지친")은 문구 표현에만 쓰고, 당일 야근 여부를 판정하거나 단정하지 않는다
  - cause_tags 가 비어 있거나 not_applicable 이면 태그 맥락 없이 수치만으로 쓴다
  - 말투 거부 시 중립으로 한 번 더, 응답 tone="neutral"
"""
from __future__ import annotations

import logging
import os
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field, field_validator

import llm_common

from narrate import (TAGS, TYPE_KO, PATTERN_TYPES, Observed, NarrateInvalid, _fmt_num, _nums_from_date,
                     describe_condition, numbers_in)

log = logging.getLogger("f14.notify")

MODEL = os.environ.get("F14_MODEL", os.environ.get("F03_MODEL", "claude-sonnet-5-5"))
EFFORT = "low"
MAX_TOKENS = 1000
PROMPT_VERSION = "a1"
REQUEST_TIMEOUT_SEC = 60.0

TITLE_MAX = 20
BODY_MAX = 80
NOTICE = "과거 기록을 참고한 사전 안내이며 새 결제를 감지한 결과가 아닙니다."
TAG_KO = {"overtime": "야근", "rain": "비 오는 날", "stress": "스트레스", "habit": "습관", "no_specific_reason": "특별한 이유 없음"}
SPECIAL_TAGS = ("unknown", "not_applicable", "skipped")
# 조건에 없는 카테고리를 문구에 지어내는 것을 막는다 (주점 패턴을 "배달" 이라 부르는 식). 패턴의 카테고리 단어는 허용.
CATEGORY_WORDS = {"delivery": ("배달",), "cafe": ("카페", "커피"), "transport": ("택시", "버스", "지하철"),
                  "shopping": ("쇼핑",), "living": ("편의점", "구독"), "food": ("주점", "술집", "외식")}


class NotifyPattern(BaseModel):
    pattern_id: str
    type: str
    condition: dict[str, Any] = Field(default_factory=dict)
    observed: Observed
    sum_krw: int
    chained_sum_krw: int = 0

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v not in PATTERN_TYPES:
            raise ValueError(f"type 은 {PATTERN_TYPES} 중 하나")
        return v


class NotifyRequest(BaseModel):
    tone: Literal["coach", "friend", "grandma"]
    pattern: NotifyPattern
    cause_tags: list[str] = Field(default_factory=list, max_length=10)
    scheduled_time: str = Field("", description="HH:MM. 비우면 시각을 문구에 넣지 않음")

    @field_validator("cause_tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if t not in TAGS and t not in SPECIAL_TAGS and t != "custom"]
        if bad:
            raise ValueError(f"허용되지 않는 태그: {bad}")
        return v


class NotifyInvalid(NarrateInvalid):
    pass


NOTIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": f"알림 제목. {TITLE_MAX}자 이내"},
        "body": {"type": "string", "description": f"알림 본문. {BODY_MAX}자 이내, 한두 문장"},
    },
    "required": ["title", "body"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = f"""당신은 소비 기록 분석 서비스의 알림 문구 담당입니다. 사용자가 과거에 반복한 소비 패턴을 그 시간이 오기 전에 짧게 일깨우는 문구를 씁니다.
출력은 JSON 스키마를 따릅니다.

절대 규칙 (서버가 기계적으로 검사합니다)
- title 은 {TITLE_MAX}자 이내, body 는 {BODY_MAX}자 이내 (공백 포함). 넘기면 버려집니다.
- 숫자는 사용자 메시지의 "쓸 수 있는 숫자" 에 있는 것만. 반올림·어림·새 계산 금지. 금액은 "286,000원" 처럼 전체 자릿수.
- 이 알림은 과거 기록을 참고한 사전 안내입니다. "방금 결제했네", "지금 주문하려는 거지" 처럼 새 결제를 감지한 것처럼 쓰지 않습니다.
- 사용자가 고른 원인 태그(야근, 비 오는 날, 스트레스, 습관, 특별한 이유 없음)는 문구의 맥락으로만 씁니다 ("야근하고 지친 목요일").
  오늘 실제로 야근했는지, 비가 오는지는 모릅니다. 단정하지 말고 과거 패턴의 맥락으로만 언급합니다.
- 태그가 없으면 패턴 수치만으로 씁니다.
- 패턴 조건에 있는 카테고리만 말합니다. 주점 패턴을 "배달" 이라 부르거나, 없는 후속 결제(택시 등)를 지어내지 않습니다.
- 원인 단정("스트레스 때문에") 금지. 인격 모욕·혐오 금지. 사람 이름·계좌·잔액은 입력에 없고 쓰지 않습니다.
- body 는 "과거에 이랬다" + "오늘은 이렇게 해 보자" 의 구조. 행동 제안은 작고 구체적으로.
- 행동 제안에 새 숫자를 넣지 않습니다. "10분만 기다려" 대신 "잠깐 기다려", "한 잔" 처럼 말로 풉니다.

말투 (표현만 바뀌고 숫자는 같아야 합니다)
- coach: 담백한 코치. 존댓말. "~했어요", "~해 보세요".
- friend: 친한 친구. 반말, 가볍게. "~했잖아", "~해 봐".
- grandma: 직설적인 할머니. 반말, 구수한 훈수. "~했지", "~해라". 거침은 소비 행동에만.
- neutral: 중립. 존댓말, 꾸밈 없음."""


def allowed_numbers(req: NotifyRequest) -> set[float]:
    p = req.pattern
    nums = {float(p.observed.hit), float(p.observed.window), float(p.sum_krw), float(p.chained_sum_krw),
            float(p.sum_krw + p.chained_sum_krw)}
    for k in ("hour_from", "hour_to"):
        v = p.condition.get(k)
        if isinstance(v, (int, float)):
            nums.add(float(v))
    _nums_from_date(req.scheduled_time, nums)
    return nums


def effective_tags(tags: list[str]) -> list[str]:
    """문구에 쓸 태그. 특수 상태·custom 이 섞이면 허용값만 남기고, not_applicable 이 있으면 전부 버린다."""
    if "not_applicable" in tags:
        return []
    return [t for t in tags if t in TAGS]


def _render_request(req: NotifyRequest, tone: str) -> str:
    p = req.pattern
    tags = effective_tags(req.cause_tags)
    L = [f"말투: {tone}",
         f"패턴: {TYPE_KO.get(p.type, p.type)} / 조건: {describe_condition(p.type, p.condition)}",
         f"관찰: {p.observed.window}{p.observed.unit} 중 {p.observed.hit}회",
         f"직접 비용 {p.sum_krw:,}원, 후속 비용 {p.chained_sum_krw:,}원, 합계 {p.sum_krw + p.chained_sum_krw:,}원",
         f"사용자가 고른 원인 태그: {', '.join(TAG_KO[t] for t in tags) if tags else '(없음 → 수치만으로)'}",
         f"알림 예정 시각: {req.scheduled_time or '(지정 안 함)'}",
         "쓸 수 있는 숫자: " + ", ".join(_fmt_num(n) for n in sorted(allowed_numbers(req)))]
    return "\n".join(L)


def validate_output(req: NotifyRequest, out: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    title = (out.get("title") or "").strip()
    body = (out.get("body") or "").strip()
    if not title:
        errors.append("title: 비어 있음")
    elif len(title) > TITLE_MAX:
        errors.append(f"title: {TITLE_MAX}자 초과 (지금 {len(title)}자)")
    if not body:
        errors.append("body: 비어 있음")
    elif len(body) > BODY_MAX:
        errors.append(f"body: {BODY_MAX}자 초과 (지금 {len(body)}자)")
    allowed = allowed_numbers(req)
    cat = req.pattern.condition.get("category")
    for name, text in (("title", title), ("body", body)):
        bad = sorted({n for n in numbers_in(text) if n not in allowed})
        if bad:
            errors.append(f"{name}: 입력에 없는 숫자 {', '.join(_fmt_num(b) for b in bad)}")
        if cat in CATEGORY_WORDS:
            wrong = [w for c, words in CATEGORY_WORDS.items() if c != cat for w in words if w in text]
            if wrong:
                errors.append(f"{name}: 조건에 없는 카테고리 표현 {wrong} (이 패턴은 {cat})")
    return {"title": title, "body": body}, errors


class _ModelOut(BaseModel):
    title: str
    body: str


async def _call(client: anthropic.AsyncAnthropic, messages: list[dict[str, Any]]) -> anthropic.types.Message:
    return await llm_common.call_json(
        client, model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT, messages=messages,
        effort=EFFORT, schema=NOTIFY_SCHEMA, timeout=REQUEST_TIMEOUT_SEC,
    )


_CORRECTION = "출력이 규칙을 어겼습니다. 아래를 고쳐서 JSON 을 다시 출력하세요."


async def _generate(client: anthropic.AsyncAnthropic, req: NotifyRequest, tone: str) -> dict[str, Any]:
    """생성 + 검증 + 1회 교정. 실패면 NotifyInvalid, 거부는 llm_common.Refused. (narrate 와 같은 흐름, llm_common 에 있음)"""
    return await llm_common.generate_validated(
        lambda messages: _call(client, messages),
        [{"role": "user", "content": _render_request(req, tone)}],
        _ModelOut, lambda out: validate_output(req, out), _CORRECTION, f"notify tone={tone}", NotifyInvalid,
    )


async def notify_copy(req: NotifyRequest, client: anthropic.AsyncAnthropic | None = None) -> dict[str, Any]:
    client = client or llm_common.get_client()
    out, tone = await llm_common.with_neutral_fallback(lambda t: _generate(client, req, t), req.tone, "notify", NotifyInvalid)
    return {**out, "notice": NOTICE, "pattern_id": req.pattern.pattern_id, "tone": tone,
            "model": MODEL, "prompt_version": PROMPT_VERSION}
