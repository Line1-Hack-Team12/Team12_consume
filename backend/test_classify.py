# -*- coding: utf-8 -*-
"""classify.py 테스트. 모델 호출은 가짜로 바꾼다."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import classify as cl
from classify import ClassifyItem, classify_batch, normalize, to_payload


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
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_normalize_corrects_out_of_enum_values():
    assert normalize("living", "편의점", "high") == ("living", "편의점", "high")
    assert normalize("living", "택시", "high") == ("living", None, "high")          # 다른 상위의 하위 → null
    assert normalize("delivery", "온라인", "medium") == ("delivery", None, "medium")
    assert normalize("groceries", "편의점", "high") == ("other", "미분류", "low")  # 없는 상위 → other/low
    assert normalize("other", None, "high") == ("other", "미분류", "high")
    assert normalize("food", "외식", "sure") == ("food", "외식", "low")
    assert normalize("other", "여가", "high") == ("other", "여가", "high")        # c2 하위 태그
    assert normalize("other", "차량", "medium") == ("other", "차량", "medium")
    assert normalize("transport", "차량", "high") == ("transport", None, "high")  # 차량은 other 의 하위


def test_batch_keeps_order_and_count_even_if_model_reorders_or_drops():
    items = [ClassifyItem(merchant="GS25 역삼점"), ClassifyItem(merchant="(주)우아한형제들"), ClassifyItem(merchant="???")]
    fake = FakeClient([_msg({"items": [
        {"index": 1, "category": "delivery", "subcategory": None, "confidence": "high"},
        {"index": 0, "category": "living", "subcategory": "편의점", "confidence": "high"},
        {"index": 0, "category": "food", "subcategory": None, "confidence": "high"},     # 중복 index 는 무시
        {"index": 9, "category": "cafe", "subcategory": None, "confidence": "high"},     # 범위 밖 무시
    ]})])
    out = asyncio.run(classify_batch(fake, items))
    assert [(c.merchant, c.category, c.subcategory, c.confidence) for c in out] == [
        ("GS25 역삼점", "living", "편의점", "high"),
        ("(주)우아한형제들", "delivery", None, "high"),
        ("???", "other", "미분류", "low"),                                              # 빠진 index 는 fallback
    ]
    assert "0. GS25 역삼점" in fake.calls[0]["messages"][0]["content"]


def test_schema_failure_retries_once_then_falls_back():
    items = [ClassifyItem(merchant="A")]
    fake = FakeClient([_msg("not json"), _msg({"items": [{"index": 0, "category": "cafe", "subcategory": None, "confidence": "medium"}]})])
    out = asyncio.run(classify_batch(fake, items))
    assert out[0].category == "cafe" and len(fake.calls) == 2
    fake2 = FakeClient([_msg("not json"), _msg("still not json")])
    out2 = asyncio.run(classify_batch(fake2, items))
    assert (out2[0].category, out2[0].subcategory, out2[0].confidence) == ("other", "미분류", "low")


def test_api_error_and_refusal_fall_back_without_raising():
    import anthropic
    items = [ClassifyItem(merchant="A"), ClassifyItem(merchant="B")]
    err = anthropic.APIConnectionError(request=SimpleNamespace())
    out = asyncio.run(classify_batch(FakeClient([err]), items))
    assert [c.category for c in out] == ["other", "other"]
    out = asyncio.run(classify_batch(FakeClient([_msg({"items": []}, stop_reason="refusal")]), items))
    assert [c.confidence for c in out] == ["low", "low"]


def test_classify_splits_batches_and_preserves_order(monkeypatch):
    seen = []

    async def fake_batch(client, items):
        seen.append(len(items))
        return [cl.Classification(it.merchant, "cafe", None, "high") for it in items]

    monkeypatch.setattr(cl, "classify_batch", fake_batch)
    items = [ClassifyItem(merchant=f"m{i}") for i in range(230)]
    out = asyncio.run(cl.classify(items, client=object()))
    assert seen == [100, 100, 30] and [c.merchant for c in out] == [f"m{i}" for i in range(230)]
    payload = to_payload(out)
    assert len(payload["items"]) == 230 and payload["prompt_version"] == cl.PROMPT_VERSION
