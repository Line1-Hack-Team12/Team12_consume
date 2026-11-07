"""
F03 후처리(코드 담당 부분)의 단위 테스트. API 호출 없이 돌아갑니다.

  $ pip install pytest
  $ pytest backend/test_f03_postprocess.py -q
"""

from f03_extraction import (
    EXTRACTION_SCHEMA,
    ExtractedRow,
    ExtractionOutput,
    postprocess_row,
    resolve_date,
)


def test_resolve_date_adds_slot_year():
    assert resolve_date("10-23", "2026-10") == ("2026-10-23", False)


def test_resolve_date_flags_other_month():
    d, mismatch = resolve_date("09-30", "2026-10")
    assert d == "2026-09-30" and mismatch is True


def test_resolve_date_year_boundary():
    # 1월 슬롯에 12월 날짜가 보이면 전년도로 보정
    assert resolve_date("12-31", "2026-01") == ("2025-12-31", True)


def test_resolve_date_keeps_full_date():
    assert resolve_date("2026-10-05", "2026-10") == ("2026-10-05", False)


def test_resolve_date_impossible_day_is_flagged():
    d, mismatch = resolve_date("02-30", "2026-02")
    assert d == "2026-02-01" and mismatch is True


def test_postprocess_row_flags_and_ids():
    row = ExtractedRow(
        date="10-07", time="", merchant="  GS25 역삼점  ", amount_krw=8900,
        direction="unknown", kind="expense", confidence="low", raw_text="10.07 GS25 역삼점 8,900원",
    )
    tx = postprocess_row(row, 3, "img_01", "2026-10", "2026-10")
    assert tx.date == "2026-10-07"
    assert tx.time is None
    assert tx.merchant == "GS25 역삼점"
    assert tx.source_row_id == "img_01:row003"
    assert set(tx.flags) == {"low_confidence", "unknown_direction"}


def test_displayed_month_mismatch_sets_flag():
    row = ExtractedRow(date="10-07", time="21:10", merchant="배민", amount_krw=23500,
                       direction="out", kind="expense", confidence="high", raw_text="x")
    tx = postprocess_row(row, 0, "img_02", "2026-10", "2026-09")
    assert "month_mismatch" in tx.flags


def test_schema_and_model_agree_on_required_fields():
    # JSON 스키마(API용)와 pydantic 모델(검증용)이 같은 필드를 요구하는지 확인
    schema_fields = set(EXTRACTION_SCHEMA["properties"]["rows"]["items"]["required"])
    model_fields = set(ExtractedRow.model_fields)
    assert schema_fields == model_fields
    assert set(EXTRACTION_SCHEMA["required"]) == set(ExtractionOutput.model_fields)


def test_bad_date_shape_rejected():
    import pytest
    with pytest.raises(ValueError):
        ExtractedRow(date="10월 23일", time="", merchant="x", amount_krw=1,
                     direction="out", kind="expense", confidence="high", raw_text="x")
