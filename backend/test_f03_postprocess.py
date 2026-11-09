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


def test_resolve_date_unreadable_returns_empty():
    # 불가능하거나 비정형인 날짜는 지어내지 않고 "" (호출자가 date_missing 처리)
    assert resolve_date("02-30", "2026-02") == ("", False)       # 없는 날
    assert resolve_date("2026-10", "2026-10") == ("", False)     # 일이 없음 (전에는 1일로 지어냈음)
    assert resolve_date("13-01", "2026-10") == ("", False)       # 없는 달
    assert resolve_date("abc", "2026-10") == ("", False)
    assert resolve_date("99999999999999999999-01-01", "2026-10") == ("", False)   # OverflowError 도 삼킴


def test_resolve_date_year_boundary_forward():
    # 12월 슬롯에 보이는 1월 날짜 → 다음 해
    assert resolve_date("01-02", "2026-12") == ("2027-01-02", True)


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


def test_displayed_month_mismatch_only_when_row_has_no_date():
    # 행 날짜가 슬롯과 맞으면 헤더 월이 달라도 그 행은 정상 (두 달이 걸친 캡처)
    row = ExtractedRow(date="10-07", time="21:10", merchant="배민", amount_krw=23500,
                       direction="out", kind="expense", confidence="high", raw_text="x")
    assert "month_mismatch" not in postprocess_row(row, 0, "img_02", "2026-10", "2026-09").flags
    # 행 날짜가 없어 헤더에 기댄 경우에는 헤더 월 불일치를 반영
    row2 = ExtractedRow(date="", time="", merchant="배민", amount_krw=1000,
                        direction="out", kind="expense", confidence="high", raw_text="x")
    assert "month_mismatch" in postprocess_row(row2, 0, "img_02", "2026-10", "9월", header_date="07").flags


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



# ---- v0.6: 리뷰 결함 대응 ----

import asyncio
import re
import pytest
from pydantic import ValidationError

import f03_extraction as f03
from f03_extraction import (
    AssetExtraction, ExtractionJobResult, TransactionCandidate,
    job_status, merge_retry, retryable_assets, replace_needed_assets,
    scrub_sensitive, reconcile_kind, summarize_validation_error,
)


def test_unreadable_row_date_becomes_date_missing_not_fake_day():
    tx = postprocess_row(_row(date="2026-10"), 0, "a", "2026-10", "")
    assert tx.date == "" and "date_missing" in tx.flags and "month_mismatch" not in tx.flags


def test_row_id_is_deterministic():
    a = postprocess_row(_row(date="10-07"), 3, "img_01", "2026-10", "")
    b = postprocess_row(_row(date="10-07", merchant="다른 이름"), 3, "img_01", "2026-10", "")
    c = postprocess_row(_row(date="10-07"), 3, "img_01", "2026-11", "")
    assert a.id == b.id            # 같은 자산·행·슬롯 → 같은 id (재시도 병합 시 중복 방지)
    assert a.id != c.id            # 슬롯이 다르면 다른 id


def test_merchant_missing_flag_and_kind_conflict():
    tx = postprocess_row(_row(merchant="  ", kind="income", direction="out", date="10-01"), 0, "a", "2026-10", "")
    assert "merchant_missing" in tx.flags
    assert tx.kind == "income" and "kind_conflict" in tx.flags      # kind 는 보존, 플래그만
    assert reconcile_kind("transfer", "out") == ("transfer", False)
    assert reconcile_kind("refund", "in") == ("refund", False)
    assert reconcile_kind("refund", "out") == ("refund", False)      # 카드사 앱 "-4,500원 취소" 표기
    assert reconcile_kind("cancel", "out") == ("cancel", False)
    assert reconcile_kind("expense", "unknown") == ("expense", False)
    assert reconcile_kind("expense", "in") == ("expense", True)


def test_scrub_sensitive_removes_account_card_balance():
    # 계좌 (은행별 흔한 형식 + 마스킹)
    for acct in ["3333-01-1234567", "110-123-456789", "1002-123-123456", "352-1234-5678-13",
                 "110-***-456789", "110-123-****89"]:
        out = scrub_sensitive(f"이체 {acct} 메모")
        assert acct not in out and "[계좌]" in out, (acct, out)
        assert not re.search(r"\d{2,}", out.replace("[계좌]", "")), (acct, out)   # 끝자리 잔존 없음
    # 카드
    assert scrub_sensitive("결제 1234-5678-9012-3456") == "결제 [카드]"
    assert scrub_sensitive("카드 1234-****-****-5678") == "카드 [카드]"
    assert scrub_sensitive("카드 1234567812345678") == "카드 [번호]"
    # 전화번호는 별도 라벨
    assert scrub_sensitive("CU 02-123-4567") == "CU [전화]"
    assert scrub_sensitive("배민 010-1234-5678") == "배민 [전화]"
    # 잔액 (콜론 포함)
    assert scrub_sensitive("스타벅스 -4,500원 잔액 1,234,567원") == "스타벅스 -4,500원"
    assert scrub_sensitive("출금 -4,500원 잔액: 1,234원") == "출금 -4,500원"
    # 보존되어야 하는 것
    for keep in ["GS25 역삼점", "10.30 09:07", "2026-10-30 09:07:55", "1-2호점", "12-3번출구점",
                 "현대카드(1234)", "-1,934,364원", "스타벅스 -4,500원"]:
        assert scrub_sensitive(keep) == keep, keep
    # 앞 글자 잔존 없이 통째로
    assert scrub_sensitive("스타벅스 1-1234-5678") == "스타벅스 [계좌]"


def test_validation_error_summary_has_no_input_value():
    try:
        ExtractedRow(date="10월 23일", time="", merchant="스타벅스 강남역점", amount_krw=1,
                     direction="out", kind="expense", confidence="high", raw_text="x")
    except ValidationError as e:
        msg = summarize_validation_error(e)
        assert "10월 23일" not in msg and "스타벅스" not in msg
        assert "date" in msg
    else:
        pytest.fail("expected ValidationError")


def _asset(aid, status, code=None, nrows=0):
    rows = [TransactionCandidate(id=f"tx_{aid}_{i}", asset_id=aid, source_row_id=f"{aid}:row{i:03d}", date="2026-10-01",
                                 time=None, merchant="m", amount_krw=100, direction="out", kind="expense",
                                 confidence="high", raw_text="r") for i in range(nrows)]
    return AssetExtraction(asset_id=aid, status=status, rows=rows, error_code=code)


def test_job_status_all_skipped_is_no_transactions():
    assert job_status([_asset("a", "skipped", "NOT_TRANSACTION_SCREEN"), _asset("b", "skipped", "NOT_TRANSACTION_SCREEN")]) == "no_transactions"
    assert job_status([]) == "failed"
    assert job_status([_asset("a", "failed", "RATE_LIMITED")]) == "failed"
    assert job_status([_asset("a", "succeeded", nrows=2), _asset("b", "failed", "RATE_LIMITED")]) == "partial"
    assert job_status([_asset("a", "succeeded", nrows=2), _asset("b", "skipped", "NOT_TRANSACTION_SCREEN")]) == "succeeded"


def test_retryable_excludes_permanent_failures():
    job = ExtractionJobResult("j", "2026-10", "failed", [
        _asset("a", "failed", "RATE_LIMITED"), _asset("b", "failed", "REFUSED"),
        _asset("c", "failed", "OUTPUT_TRUNCATED"), _asset("d", "failed", "NETWORK_ERROR"),
        _asset("e", "skipped", "NOT_TRANSACTION_SCREEN"),
    ])
    assert retryable_assets(job) == ["a", "d"]
    assert replace_needed_assets(job) == ["b", "c"]


def test_merge_retry_keeps_row_count_invariant():
    original = ExtractionJobResult("job_1", "2026-10", "partial", [
        _asset("a", "succeeded", nrows=5), _asset("b", "failed", "RATE_LIMITED"), _asset("c", "succeeded", nrows=3),
    ])
    retry = ExtractionJobResult("job_2", "2026-10", "succeeded", [_asset("b", "succeeded", nrows=4)])
    merged = merge_retry(original, retry)
    assert merged.job_id == "job_1" and merged.status == "succeeded"
    assert [a.asset_id for a in merged.assets] == ["a", "b", "c"]
    assert len(merged.all_rows) == 5 + 4 + 3
    assert len({r.source_row_id for r in merged.all_rows}) == 12       # 중복 없음
    # 이미 성공한 자산을 다시 보내면 거부 (F04 에서 고친 행을 덮어쓰지 않기 위해) → 행 수가 늘어날 길이 없음
    with pytest.raises(ValueError):
        merge_retry(merged, ExtractionJobResult("job_3", "2026-10", "succeeded", [_asset("b", "succeeded", nrows=4)]))
    with pytest.raises(ValueError):
        merge_retry(original, ExtractionJobResult("j", "2026-10", "succeeded", [_asset("zzz", "succeeded")]))


def test_unexpected_exception_in_one_asset_does_not_kill_job(monkeypatch, tmp_path):
    async def fake_extract_one(client, asset_id, path, slot_month):
        if asset_id == "bad":
            raise RuntimeError("boom")
        return _asset(asset_id, "succeeded", nrows=2)
    monkeypatch.setattr(f03, "extract_one", fake_extract_one)
    assets = [("ok1", tmp_path / "1.png"), ("bad", tmp_path / "2.png"), ("ok2", tmp_path / "3.png")]
    job = asyncio.run(f03.run_extraction_job(assets, "2026-10", client=object()))   # client 를 넘기면 close 안 함
    assert job.status == "partial"
    by = {a.asset_id: a for a in job.assets}
    assert by["bad"].status == "failed" and by["bad"].error_code == "INTERNAL"
    assert by["bad"].error_message == "RuntimeError"                 # 메시지 본문(원문 가능성)은 저장하지 않음
    assert len(job.all_rows) == 4


# ---- v0.7: 재리뷰 지적 ----

from f03_extraction import parse_displayed_month


def test_header_year_mismatch_is_caught_even_when_row_month_matches():
    # 작년 10월 캡처를 올해 10월 슬롯에 올림: 행 "10-07" 은 연도가 없어 슬롯 월과 맞아 보이지만 헤더에 2025 가 있음
    row = _row(date="10-07")
    for header in ["2025.10", "2025년 10월", "2025-10", "2025.10 이용내역"]:
        assert "month_mismatch" in postprocess_row(row, 0, "a", "2026-10", header).flags, header
    # 헤더 연도가 슬롯과 같으면 정상
    assert "month_mismatch" not in postprocess_row(row, 0, "a", "2026-10", "2026.10").flags
    # 행 날짜에 연도가 있으면 행 연도가 우선 (헤더가 틀렸어도 행 기준)
    assert "month_mismatch" not in postprocess_row(_row(date="2026-10-07"), 0, "a", "2026-10", "2025.10").flags
    # 두 달 걸친 캡처: 행 날짜가 슬롯과 맞고 헤더 월만 다르면 정상
    assert "month_mismatch" not in postprocess_row(row, 0, "a", "2026-10", "9월").flags


def test_parse_displayed_month():
    assert parse_displayed_month("8월") == (None, 8)
    assert parse_displayed_month("2026.10") == (2026, 10)
    assert parse_displayed_month("2025년 10월") == (2025, 10)
    assert parse_displayed_month("2026 이용내역") == (2026, None)
    assert parse_displayed_month("전체 내역") == (None, None)
    assert parse_displayed_month("") == (None, None)


def test_two_digit_year_is_not_invented():
    assert resolve_date("26-10-07", "2026-10") == ("", False)


def test_merge_retry_rejects_slot_change_and_overwriting_success():
    original = ExtractionJobResult("job_1", "2026-10", "partial", [
        _asset("a", "succeeded", nrows=5), _asset("b", "failed", "RATE_LIMITED"),
    ])
    with pytest.raises(ValueError):
        merge_retry(original, ExtractionJobResult("j", "2026-11", "succeeded", [_asset("b", "succeeded", nrows=1)]))
    with pytest.raises(ValueError):
        merge_retry(original, ExtractionJobResult("j", "2026-10", "succeeded", [_asset("a", "succeeded", nrows=1)]))
    # 실패 자산이 재시도에서도 실패하면 partial 유지 + 여전히 재시도 대상
    merged = merge_retry(original, ExtractionJobResult("j", "2026-10", "failed", [_asset("b", "failed", "NETWORK_ERROR")]))
    assert merged.status == "partial" and retryable_assets(merged) == ["b"]
