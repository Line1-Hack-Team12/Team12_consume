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


# ---- v0.2: 헤더 날짜(일별 보기), 빈 날짜, 표시 월 비교 ----

from f03_extraction import displayed_month_mismatch


def _row(**kw):
    base = dict(date="", time="", merchant="x", amount_krw=1000,
                direction="out", kind="expense", confidence="high", raw_text="x")
    base.update(kw)
    return ExtractedRow(**base)


def test_empty_row_date_uses_header_date():
    # 토스 달력 일별 보기: 행에 날짜 없음, 헤더에 "21일"
    tx = postprocess_row(_row(), 0, "img_01", "2026-08", "8월", header_date="21")
    assert tx.date == "2026-08-21"
    assert "month_mismatch" not in tx.flags and "date_missing" not in tx.flags


def test_header_date_with_month():
    tx = postprocess_row(_row(), 0, "img_01", "2026-08", "", header_date="08-21")
    assert tx.date == "2026-08-21" and tx.flags == []


def test_no_date_anywhere_is_flagged_missing():
    tx = postprocess_row(_row(), 0, "img_04", "2026-10", "", header_date="")
    assert tx.date == ""                      # 지어내지 않음. F04 에서 사용자가 입력
    assert "date_missing" in tx.flags


def test_row_date_wins_over_header_date():
    tx = postprocess_row(_row(date="10-12"), 0, "a", "2026-10", "", header_date="01")
    assert tx.date == "2026-10-12"


def test_displayed_month_free_text_compare():
    assert displayed_month_mismatch("8월", "2026-08") is False
    assert displayed_month_mismatch("8월", "2026-10") is True
    assert displayed_month_mismatch("2026.10", "2026-10") is False
    assert displayed_month_mismatch("2025.10", "2026-10") is True
    assert displayed_month_mismatch("", "2026-10") is False
    assert displayed_month_mismatch("전체 내역", "2026-10") is False


def test_header_date_bad_shape_rejected():
    import pytest
    with pytest.raises(ValueError):
        ExtractionOutput(is_transaction_screen=True, app_guess="", displayed_month="",
                         header_date="21일", rows=[])


from f03_extraction import normalize_time


def test_normalize_time_variants():
    assert normalize_time("09:07:55") == "09:07"
    assert normalize_time("9:07") == "09:07"
    assert normalize_time("오후 2:15") == "14:15"
    assert normalize_time("오전 12:05") == "00:05"
    assert normalize_time("") is None
    assert normalize_time("자동이체") is None
    assert normalize_time("25:99") is None
