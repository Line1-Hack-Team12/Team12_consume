"""
모델 호출 공통. f03_extraction · classify · narrate · notify 가 같이 쓴다.

- 클라이언트 하나: 프로세스에 `anthropic.AsyncAnthropic` 을 하나만 만들어 연결 풀을 공유한다 (요청마다 만들지 않음).
  타임아웃은 호출마다 `timeout=` 으로 준다.
- 전역 동시 호출 상한: 모듈별 세마포어(추출 5·분류 3)는 "한 작업 안"의 상한이고, 요청이 여러 개 겹치면 그 합이
  얼마든지 커진다. 여기의 세마포어가 프로세스 전체를 `AI_MAX_CONCURRENT_CALLS`(기본 8) 로 묶는다.
- `first_text` · `parse_or_errors` · `generate_validated` · `with_neutral_fallback`: 응답에서 JSON 꺼내기, 1회 교정,
  거부 시 중립 말투 폴백. 네 모듈에 따로 있던 같은 코드를 한 곳으로.

테스트는 `client=` 로 가짜 클라이언트를 넣는다. 가짜도 전역 세마포어는 그대로 지난다.
"""
from __future__ import annotations

import asyncio
import logging
import os
import weakref
from typing import Any, Awaitable, Callable

import anthropic
from pydantic import BaseModel, ValidationError

log = logging.getLogger("f03.llm")

MAX_CONCURRENT_CALLS = max(1, int(os.environ.get("AI_MAX_CONCURRENT_CALLS", "8")))
BETAS = ["server-side-fallback-2026-07-01"]

_client: anthropic.AsyncAnthropic | None = None
# 세마포어는 이벤트 루프에 묶이므로 루프마다 하나 (테스트는 asyncio.run 을 여러 번 부른다)
_sems: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def get_client() -> anthropic.AsyncAnthropic:
    """프로세스 공용 클라이언트. 처음 부를 때 만든다 (import 시점에 키가 없어도 모듈은 올라감)."""
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


def global_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _sems.get(loop)
    if sem is None:
        sem = _sems[loop] = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
    return sem


async def call_json(
    client: Any,
    *,
    model: str,
    max_tokens: int,
    system: str,
    messages: list[dict[str, Any]],
    effort: str,
    schema: dict[str, Any],
    timeout: float,
) -> anthropic.types.Message:
    """
    JSON 스키마 출력으로 한 번 호출. 네 모듈이 같은 옵션을 쓴다:

    - fallbacks="default" : 안전 분류기가 거부하면 서버가 다른 모델로 한 번 더 시도 (Claude API 전용 베타.
                            Bedrock/Vertex 로 옮기면 betas·fallbacks 두 줄은 뺀다)
    - cache_control       : 시스템 프롬프트 캐시. 같은 프롬프트로 여러 번 부르므로 두 번째부터 입력 비용이 준다
    - output_config.format: 첫 text 블록이 스키마에 맞는 JSON
    """
    async with global_semaphore():
        return await client.beta.messages.create(
            model=model, max_tokens=max_tokens,
            betas=BETAS, fallbacks="default",
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            timeout=timeout,
        )


def first_text(message: Any) -> str:
    """응답의 첫 text 블록. (사고 블록이 앞에 올 수 있으니 type 으로 찾는다.) 없으면 ValueError."""
    for block in message.content:
        if block.type == "text":
            return block.text
    raise ValueError("no text block in response")


def parse_or_errors(message: Any, model_cls: type[BaseModel]) -> tuple[BaseModel | None, list[str]]:
    """응답에서 JSON 을 꺼낸다. text 블록이 없거나(max_tokens 로 사고만 하다 끝남) 스키마가 틀리면 오류 목록."""
    if message.stop_reason == "max_tokens":
        return None, ["출력이 길이 제한에 잘렸습니다. 더 짧게 쓰세요"]
    try:
        return model_cls.model_validate_json(first_text(message)), []
    except (ValueError, ValidationError) as e:
        return None, [f"출력이 JSON 스키마와 맞지 않습니다 ({type(e).__name__})"]


class Refused(Exception):
    """stop_reason == refusal. fallbacks="default" 가 이미 다른 모델로 한 번 시도한 뒤의 최종 결과."""


class OutputInvalid(Exception):
    """교정 재시도 후에도 출력이 계약을 어김. errors 에 사유 목록."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


async def generate_validated(
    call: Callable[[list[dict[str, Any]]], Awaitable[Any]],
    messages: list[dict[str, Any]],
    model_cls: type[BaseModel],
    validate: Callable[[dict[str, Any]], tuple[dict[str, Any], list[str]]],
    correction_intro: str,
    name: str,
    invalid_cls: type[OutputInvalid] = OutputInvalid,
) -> dict[str, Any]:
    """호출 → 검증 → 오류 목록을 돌려주며 1회 교정 → 그래도 틀리면 invalid_cls(errors). 거부는 Refused.

    교정 메시지에는 모델 응답(사고 블록 포함)을 그대로 돌려준다. 사고 블록을 고치거나 빼면 안 된다.
    """
    last: list[str] = []
    for attempt in range(2):
        msg = await call(messages)
        if msg.stop_reason == "refusal":
            raise Refused()
        parsed, errors = parse_or_errors(msg, model_cls)
        fixed: dict[str, Any] = {}
        if parsed is not None:
            fixed, errors = validate(parsed.model_dump())
        if not errors:
            log.info("%s ok attempt=%d", name, attempt + 1)
            return fixed
        last = errors
        log.warning("%s invalid attempt=%d errors=%d", name, attempt + 1, len(errors))   # 사유 개수만, 문장 없음
        if attempt == 0:
            messages = messages + [
                {"role": "assistant", "content": msg.content},
                {"role": "user", "content": correction_intro + "\n- " + "\n- ".join(errors)},
            ]
    raise invalid_cls(last)


async def with_neutral_fallback(
    gen: Callable[[str], Awaitable[dict[str, Any]]],
    tone: str,
    name: str,
    invalid_cls: type[OutputInvalid] = OutputInvalid,
) -> tuple[dict[str, Any], str]:
    """요청 말투로 생성. 거부되면 중립 말투로 한 번 더. 돌려주는 값: (출력, 실제 쓴 말투)."""
    try:
        return await gen(tone), tone
    except Refused:
        log.warning("%s refused tone=%s → neutral", name, tone)
        try:
            return await gen("neutral"), "neutral"
        except Refused:
            raise invalid_cls(["모델이 생성을 거부했습니다 (중립 말투로도)"])
