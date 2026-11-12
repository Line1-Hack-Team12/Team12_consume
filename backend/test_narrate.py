# -*- coding: utf-8 -*-
"""narrate.py 테스트. 모델 호출은 가짜로 바꾼다. 핵심은 검증기(숫자·증거 id)와 재시도·말투 폴백."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

import narrate as nr
from narrate import NarrateInvalid, NarrateRequest, normalize_tags, numbers_in, validate_output


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


REQ = {
    "snapshot_id": "snap_1", "tone": "coach",
    "period": {"start": "2026-08-01", "end": "2026-10-31", "slots": ["2026-08", "2026-09", "2026-10"]},
    "want": ["cards", "summary", "report"],
    "patterns": [{
        "pattern_id": "pat_3", "type": "weekday",
        "condition": {"weekday": 3, "hour_from": 21, "hour_to": 24, "category": "delivery"},
        "observed": {"hit": 11, "window": 13, "unit": "주"},
        "sum_krw": 286000, "chained_sum_krw": 94000, "evidence_grade": "strong",
        "evidence_transactions": [
            {"id": "tx_a", "date": "2026-10-23", "time": "22:14", "merchant": "(주)우아한형제들", "amount_krw": 27000, "category": "delivery"},
            {"id": "tx_b", "date": "2026-10-16", "time": "21:50", "merchant": "(주)우아한형제들", "amount_krw": 24500, "category": "delivery"},
            {"id": "tx_c", "date": "2026-10-09", "time": "23:02", "merchant": "쿠팡이츠", "amount_krw": 19800, "category": "delivery"},
        ],
        "chain_transactions": [
            {"id": "tx_d", "date": "2026-10-24", "time": "10:40", "merchant": "CU 역삼점", "amount_krw": 6800, "category": "living", "subcategory": "편의점", "lag_minutes": 746},
        ],
    }],
    "summary": {"total_spend_krw": 1243500,
                "by_category": [{"category": "food", "sum_krw": 412000, "share": 0.33}],
                "by_month": [{"month": "2026-08", "sum_krw": 401000}],
                "limits": {"days_without_upload": 4, "transactions_without_time": 12, "weather_skipped": True}},
    "subscriptions": [{"merchant": "유튜브 프리미엄", "monthly_krw": 14900, "observed_months": 3}],
}

GOOD = {
    "cards": [{
        "pattern_id": "pat_3", "title": "목요일 밤 배달",
        "observation": "최근 13주 중 11주, 목요일 21시 이후에 배달 주문이 있었어요. 직접 비용 286,000원에 다음날 편의점 94,000원이 따라왔어요.",
        "hypothesis_question": "목요일 밤에 유독 주문이 몰리는 이유가 있을까요?",
        "action": "목요일 저녁에는 주문 전에 냉장고를 한 번 열어 보세요.",
        "recommended_tags": ["overtime", "stress", "habit"],
        "evidence_transaction_ids": ["tx_a", "tx_b", "tx_c"],
    }],
    "summary_line": "지난 3개월, 목요일 밤 배달이 가장 큰 반복 지출이었어요.",
    "report": {
        "overview": ["8월부터 10월까지 총 1,243,500원을 썼어요.", "가장 큰 반복은 목요일 밤 배달 286,000원이에요."],
        "observations": [{"pattern_id": "pat_3", "sentence": "목요일 밤 배달이 13주 중 11주 있었어요."}],
        "actions": ["목요일 저녁 주문 전에 냉장고를 확인해 보세요."],
        "limits_sentence": "업로드가 없는 날 4일과 시각이 없는 거래 12건은 시간대 분석에서 빠졌어요.",
    },
}


def _req(**over):
    d = copy.deepcopy(REQ)
    d.update(over)
    return NarrateRequest.model_validate(d)


def test_numbers_in_parses_commas_and_decimals():
    assert numbers_in("13주 중 11회, 286,000원, 0.33") == [13.0, 11.0, 286000.0, 0.33]
    assert numbers_in("숫자 없음") == []


def test_valid_output_passes_unchanged():
    fixed, errors = validate_output(_req(), GOOD)
    assert errors == []
    assert fixed["cards"][0]["evidence_transaction_ids"] == ["tx_a", "tx_b", "tx_c"]
    assert fixed["cards"][0]["recommended_tags"] == ["overtime", "stress", "habit"]
    assert fixed["summary_line"].startswith("지난 3개월")
    assert fixed["report"]["limits_sentence"].startswith("업로드")


def test_invented_number_is_an_error():
    bad = copy.deepcopy(GOOD)
    bad["cards"][0]["observation"] = "최근 13주 중 11주, 목요일 밤 배달에 약 29만 원을 썼어요."   # 29 는 입력에 없음
    _, errors = validate_output(_req(), bad)
    assert len(errors) == 1 and "observation" in errors[0] and "29" in errors[0]
    bad2 = copy.deepcopy(GOOD)
    bad2["report"]["limits_sentence"] = "업로드 없는 날 5일이 빠졌어요."                            # 4 → 5
    _, errors = validate_output(_req(), bad2)
    assert len(errors) == 1 and "limits_sentence" in errors[0]


def test_sum_of_direct_and_chained_is_allowed():
    ok = copy.deepcopy(GOOD)
    ok["cards"][0]["observation"] = "목요일 밤 배달과 다음날 편의점까지 합치면 380,000원이에요. 13주 중 11주 있었어요."
    _, errors = validate_output(_req(), ok)
    assert errors == []


def test_evidence_ids_must_be_subset_and_enough():
    bad = copy.deepcopy(GOOD)
    bad["cards"][0]["evidence_transaction_ids"] = ["tx_a", "tx_zzz"]
    _, errors = validate_output(_req(), bad)
    assert any("증거가 아닌 id" in e for e in errors) and any("3건 이상" in e for e in errors)
    # 후속 결제 id 는 증거가 아님
    bad["cards"][0]["evidence_transaction_ids"] = ["tx_a", "tx_b", "tx_d"]
    _, errors = validate_output(_req(), bad)
    assert len(errors) == 2 and "tx_d" in errors[0] and "3건 이상" in errors[1]   # tx_d 를 빼면 2건이라 둘 다 오류


def test_missing_or_unknown_pattern_card_is_an_error():
    bad = copy.deepcopy(GOOD)
    bad["cards"][0]["pattern_id"] = "pat_9"
    _, errors = validate_output(_req(), bad)
    assert any("요청에 없는 pattern_id" in e for e in errors) and any("카드가 없음" in e for e in errors)


def test_tags_are_corrected_not_rejected():
    assert normalize_tags(["overtime", "overtime", "custom", "rain"], "weekday") == ["overtime", "rain", "stress"]
    assert normalize_tags(None, "rain") == ["rain", "habit", "no_specific_reason"]
    assert normalize_tags(["a", "b", "c", "d"], "chain") == ["habit", "stress", "no_specific_reason"]
    bad = copy.deepcopy(GOOD)
    bad["cards"][0]["recommended_tags"] = ["stress", "custom"]
    fixed, errors = validate_output(_req(), bad)
    assert errors == [] and len(fixed["cards"][0]["recommended_tags"]) == 3


def test_want_limits_what_is_validated_and_returned():
    out = copy.deepcopy(GOOD)
    out["summary_line"] = "엉뚱한 999원"
    fixed, errors = validate_output(_req(want=["cards"]), out)
    assert errors == [] and fixed["summary_line"] is None and fixed["report"] is None and len(fixed["cards"]) == 1


def test_generate_retries_once_with_error_list_then_raises():
    bad = copy.deepcopy(GOOD)
    bad["cards"][0]["action"] = "매주 50,000원씩 아껴 보세요."    # 50,000 없음
    fake = FakeClient([_msg(bad), _msg(GOOD)])
    out = asyncio.run(nr.narrate(_req(), client=fake))
    assert len(fake.calls) == 2 and out["tone"] == "coach" and out["cards"][0]["action"].startswith("목요일")
    assert "50,000" in fake.calls[1]["messages"][-1]["content"]           # 오류 목록이 교정 메시지에 들어감
    fake2 = FakeClient([_msg(bad), _msg(bad)])
    with pytest.raises(NarrateInvalid) as ei:
        asyncio.run(nr.narrate(_req(), client=fake2))
    assert any("action" in e for e in ei.value.errors)


def test_refusal_falls_back_to_neutral_tone():
    fake = FakeClient([_msg({}, stop_reason="refusal"), _msg(GOOD)])
    out = asyncio.run(nr.narrate(_req(tone="grandma"), client=fake))
    assert out["tone"] == "neutral" and "말투: neutral" in fake.calls[1]["messages"][0]["content"]
    assert "말투: grandma" in fake.calls[0]["messages"][0]["content"]
    with pytest.raises(NarrateInvalid):
        asyncio.run(nr.narrate(_req(tone="grandma"), client=FakeClient([_msg({}, stop_reason="refusal")] * 2)))


def test_request_validation():
    with pytest.raises(Exception):
        _req(tone="pirate")
    with pytest.raises(Exception):
        _req(want=[])
    d = copy.deepcopy(REQ)
    d["patterns"][0]["evidence_transactions"].append(dict(d["patterns"][0]["evidence_transactions"][0]))   # id 중복
    with pytest.raises(Exception):
        NarrateRequest.model_validate(d)


def test_render_uses_korean_weekday_and_allowed_numbers():
    text = nr._render_request(_req(), "friend")
    assert "목요일" in text and "21시~24시" in text and "배달(delivery)" in text
    assert "13주 중 11회" in text and "286,000원" in text and "380,000원" in text
    assert "tx_a | 2026-10-23 22:14 | (주)우아한형제들 | 27,000원" in text
    assert "이 카드에서 쓸 수 있는 숫자" in text
