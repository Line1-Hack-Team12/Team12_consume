# -*- coding: utf-8 -*-
"""notify.py 테스트. 모델 호출은 가짜로 바꾼다."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

import notify as nf
from notify import NOTICE, NotifyInvalid, NotifyRequest, effective_tags, validate_output


def _msg(payload, stop_reason="end_turn"):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


REQ = {
    "tone": "grandma",
    "pattern": {"pattern_id": "pat_3", "type": "weekday",
                "condition": {"weekday": 3, "hour_from": 21, "hour_to": 24, "category": "delivery"},
                "observed": {"hit": 11, "window": 13, "unit": "주"}, "sum_krw": 286000, "chained_sum_krw": 94000},
    "cause_tags": ["overtime", "stress"],
    "scheduled_time": "20:00",
}
GOOD = {"title": "목요일 밤이다", "body": "야근하고 지친 목요일, 13주 중 11주를 배달로 때웠지. 오늘은 냉장고부터 열어 봐라."}


def _req(**over):
    d = copy.deepcopy(REQ)
    d.update(over)
    return NotifyRequest.model_validate(d)


def test_valid_output_passes():
    fixed, errors = validate_output(_req(), GOOD)
    assert errors == [] and fixed == GOOD


def test_length_and_number_errors():
    bad = {"title": "목" * 21, "body": "13주 중 11주 배달. 대략 30만 원."}
    _, errors = validate_output(_req(), bad)
    assert any("title: 20자 초과" in e for e in errors) and any("body: 입력에 없는 숫자 30" in e for e in errors)
    _, errors = validate_output(_req(), {"title": "제목", "body": "x" * 81})
    assert errors == ["body: 80자 초과 (지금 81자)"]
    _, errors = validate_output(_req(), {"title": "", "body": ""})
    assert errors == ["title: 비어 있음", "body: 비어 있음"]


def test_allowed_numbers_include_sum_hours_and_scheduled_time():
    ok = {"title": "목요일 21시 전에", "body": "13주 중 11주, 합치면 380,000원이 나갔어요. 20시 알림이에요."}
    assert validate_output(_req(), ok)[1] == []


def test_effective_tags_rules():
    assert effective_tags(["overtime", "stress"]) == ["overtime", "stress"]
    assert effective_tags(["overtime", "not_applicable"]) == []
    assert effective_tags(["unknown"]) == []
    assert effective_tags(["custom", "habit"]) == ["habit"]
    with pytest.raises(Exception):
        _req(cause_tags=["bored"])
    text = nf._render_request(_req(cause_tags=[]), "coach")
    assert "(없음 → 수치만으로)" in text
    text = nf._render_request(_req(), "coach")
    assert "야근, 스트레스" in text and "목요일" in text and "13주 중 11회" in text


def test_notify_copy_adds_fixed_notice_and_retries_once():
    bad = {"title": "목요일 밤", "body": "약 29만 원을 배달에 썼지. 오늘은 참아라."}
    fake = FakeClient([_msg(bad), _msg(GOOD)])
    out = asyncio.run(nf.notify_copy(_req(), client=fake))
    assert out["notice"] == NOTICE and out["tone"] == "grandma" and out["title"] == "목요일 밤이다"
    assert len(fake.calls) == 2 and "29" in fake.calls[1]["messages"][-1]["content"]
    with pytest.raises(NotifyInvalid):
        asyncio.run(nf.notify_copy(_req(), client=FakeClient([_msg(bad), _msg(bad)])))


def test_refusal_falls_back_to_neutral():
    fake = FakeClient([_msg({}, stop_reason="refusal"), _msg(GOOD)])
    out = asyncio.run(nf.notify_copy(_req(), client=fake))
    assert out["tone"] == "neutral" and "말투: neutral" in fake.calls[1]["messages"][0]["content"]


def test_other_category_words_are_rejected():
    _, errors = validate_output(_req(), {"title": "목요일 밤 택시", "body": "13주 중 11주 배달이었지. 오늘은 참아라."})
    assert errors == ["title: 조건에 없는 카테고리 표현 ['택시'] (이 패턴은 delivery)"]
    food = copy.deepcopy(REQ); food["pattern"]["condition"] = {"category": "food", "subcategory": "주점", "hour_from": 22, "hour_to": 26}
    _, errors = validate_output(NotifyRequest.model_validate(food), {"title": "밤늦은 배달 또 생각나지?", "body": "13주 중 11회 술집에서 썼지."})
    assert len(errors) == 1 and "배달" in errors[0]
    nocat = copy.deepcopy(REQ); nocat["pattern"]["condition"] = {"weekday": 3}
    assert validate_output(NotifyRequest.model_validate(nocat), {"title": "배달 택시", "body": "13주 중 11주."})[1] == []
