# -*- coding: utf-8 -*-
"""llm_common 테스트. 전역 동시 호출 상한과 응답 파싱만 본다 (모델 호출 없음)."""
import asyncio
from types import SimpleNamespace

import pytest

import llm_common as lc


def _msg(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="thinking", thinking="…"),
                                                             SimpleNamespace(type="text", text=text)])


def test_first_text_skips_thinking_and_raises_without_text():
    assert lc.first_text(_msg("hi")) == "hi"
    with pytest.raises(ValueError):
        lc.first_text(SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="thinking", thinking="…")]))


def test_global_semaphore_caps_concurrent_calls(monkeypatch):
    monkeypatch.setattr(lc, "MAX_CONCURRENT_CALLS", 2)
    lc._sems.clear()
    running, peak = 0, 0

    class Client:
        beta = SimpleNamespace(messages=None)

        def __init__(self):
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

        async def _create(self, **kw):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1
            return _msg("{}")

    async def main():
        c = Client()
        await asyncio.gather(*(lc.call_json(c, model="m", max_tokens=10, system="s", messages=[], effort="low",
                                            schema={}, timeout=1.0) for _ in range(6)))

    asyncio.run(main())
    assert peak == 2


def test_semaphore_is_per_event_loop():
    lc._sems.clear()

    async def get():
        return lc.global_semaphore()

    a = asyncio.run(get())
    b = asyncio.run(get())
    assert a is not b
