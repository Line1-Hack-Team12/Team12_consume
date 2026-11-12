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
    async def fake_extract_one(client, asset_id, path, slot_month, owner_name=None):
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



# ---- v0.8: 일별 보기 + 잘못된 슬롯 ----

def test_day_only_date_uses_displayed_month_not_slot():
    # 화면 "10월" + 헤더 "5일" 을 11월 슬롯에 올림: 날짜는 화면대로 10-05, 불일치 플래그
    tx = postprocess_row(_row(date=""), 0, "a", "2026-11", "10월", header_date="05")
    assert tx.date == "2026-10-05" and "month_mismatch" in tx.flags
    # 슬롯이 맞으면 플래그 없음
    tx = postprocess_row(_row(date=""), 0, "a", "2026-10", "10월", header_date="05")
    assert tx.date == "2026-10-05" and "month_mismatch" not in tx.flags
    # 화면에 연도까지 있으면 연도도 화면 기준 (작년 캡처 → 불일치)
    tx = postprocess_row(_row(date=""), 0, "a", "2026-10", "2025.10", header_date="05")
    assert tx.date == "2025-10-05" and "month_mismatch" in tx.flags
    # 화면에 달이 없으면 종전대로 슬롯 달
    tx = postprocess_row(_row(date=""), 0, "a", "2026-11", "", header_date="05")
    assert tx.date == "2026-11-05" and "month_mismatch" not in tx.flags
    # 행 날짜에 이미 달이 있으면 손대지 않음
    tx = postprocess_row(_row(date="10-07"), 0, "a", "2026-11", "11월")
    assert tx.date == "2026-10-07" and "month_mismatch" in tx.flags


def test_scrub_person_name_with_account_note():
    assert scrub_sensitive("내 토스뱅크 통장 → 이아림(모임통장)") == "내 토스뱅크 통장 → 개인"
    assert scrub_sensitive("김철수 (생활비)") == "개인"
    assert scrub_sensitive("카카오페이(모임통장)") == "카카오페이(모임통장)"     # 5자 이상 상호는 그대로
    assert scrub_sensitive("CU 운정애플점") == "CU 운정애플점"


# ---- v0.9: 조각 행 제거, raw_text 의 잔액 제거 ----

from f03_extraction import is_fragment, strip_other_amounts


def test_fragment_row_without_merchant_and_direction():
    tx = postprocess_row(_row(merchant="", direction="unknown", kind="unknown", date="", raw_text="21:15 -2,095,864원"),
                         0, "a", "2026-11", "")
    assert is_fragment(tx)
    assert not is_fragment(postprocess_row(_row(merchant="", direction="out", date="10-01"), 0, "a", "2026-10", ""))
    assert not is_fragment(postprocess_row(_row(direction="unknown", date="10-01"), 0, "a", "2026-10", ""))


def test_strip_other_amounts_keeps_only_transaction_amount():
    assert strip_other_amounts("개인 5,750원 14:15 -1,934,364원", 5750) == "개인 5,750원 14:15"
    assert strip_other_amounts("네이버페이충전 -100,000원 19:36 #자동이체 -2,134,364원", 100000) == "네이버페이충전 -100,000원 19:36 #자동이체"
    assert strip_other_amounts("-29,000 -26,331원 내 포인트 사용 | Apple Services", 26331) == "-29,000 -26,331원 내 포인트 사용 | Apple Services"
    assert strip_other_amounts("43원 카드 캐시백 → 내 토스뱅크 통장", 43) == "43원 카드 캐시백 → 내 토스뱅크 통장"
    assert strip_other_amounts("-14,500원 레벨업pc방 | 토스뱅크 체크카드 캐시백 43", 14500) == "-14,500원 레벨업pc방 | 토스뱅크 체크카드 캐시백 43"
    tx = postprocess_row(_row(amount_krw=5750, raw_text="개인 5,750원 14:15 -1,934,364원", date="11-02"), 0, "a", "2026-11", "")
    assert "1,934,364" not in tx.raw_text and "5,750원" in tx.raw_text


from f03_extraction import effective_header_date


def test_header_date_ignored_when_any_row_has_its_own_date():
    # 목록 화면: 헤더가 잘린 맨 위 행은 "" 유지, 달력 선택일은 무시
    assert effective_header_date("07", ["", "", "10-06", "10-05"]) == ""
    # 일별 보기: 모든 행이 비어 있으면 헤더 날짜 사용
    assert effective_header_date("07", ["", "", ""]) == "07"
    assert effective_header_date("", ["", ""]) == ""


# ---- v0.10: 캡처 주인 이름 → "본인", self_transfer ----

from f03_extraction import scrub_owner_name, infer_self_transfer


def test_scrub_owner_name_exact_and_masked():
    assert scrub_owner_name("홍길동 → 내 토스뱅크 통장", "홍길동") == ("본인 → 내 토스뱅크 통장", False)
    assert scrub_owner_name("홍*동 5,000원", "홍길동") == ("본인 5,000원", True)
    assert scrub_owner_name("홍** 5,000원", "홍길동") == ("본인 5,000원", True)
    assert scrub_owner_name("김*수", "홍길동") == ("김*수", False)             # 다른 사람
    assert scrub_owner_name("김*수", "김수") == ("김*수", False)               # 두 글자 이름은 가려진 표기 매칭 안 함
    assert scrub_owner_name("스타벅스", "") == ("스타벅스", False)
    assert scrub_owner_name("", "홍길동") == ("", False)


def test_infer_self_transfer_rules():
    assert infer_self_transfer("transfer", "내 계좌 이체 | 토스뱅크 → KB국민", "") == "self_transfer"
    assert infer_self_transfer("transfer", "본인", "본인 → 내 토스뱅크 통장 본인 → 본인") == "self_transfer"
    assert infer_self_transfer("transfer", "개인", "내 토스뱅크 통장 → 개인") == "transfer"
    assert infer_self_transfer("transfer", "개인", "내 토스뱅크 통장 → 본인(모임통장)") == "transfer"   # 괄호 설명은 공동 자금
    assert infer_self_transfer("refund", "내 계좌 이체 취소", "") == "refund"


def test_pay_money_charge_is_self_transfer():
    assert infer_self_transfer("expense", "네이버페이충전", "-50,000원 네이버페이충전") == "self_transfer"
    assert infer_self_transfer("expense", "충전 | 토스뱅크 → 토스페이머니", "") == "self_transfer"
    assert infer_self_transfer("expense", "뼈옥 파주본점 결제 충전", "") == "self_transfer"          # 토스 "결제 충전"
    assert infer_self_transfer("income", "네이버페이충전 → 내 우리계좌", "") == "self_transfer"       # 페이 머니 → 본인 계좌
    assert infer_self_transfer("expense", "뼈옥 파주본점 | 토스페이머니", "") == "expense"            # 페이 머니로 결제한 행은 지출
    assert infer_self_transfer("expense", "티머니 충전", "") == "expense"                            # 교통카드 충전은 지출
    assert infer_self_transfer("expense", "스타벅스 카드 충전", "") == "expense"
    assert infer_self_transfer("transfer", "카카오페이 송금수취", "") == "transfer"
    assert infer_self_transfer("cancel", "네이버페이충전 취소", "") == "cancel"
    # 충전 표기 없이 본인 계좌 ↔ 페이 회사 이동
    assert infer_self_transfer("expense", "네이버파이낸셜", "-3,000원 내 우리계좌 → 네이버파이낸셜") == "self_transfer"
    assert infer_self_transfer("income", "카카오페이", "50,000원 카카오페이 → 내 토스뱅크 통장") == "self_transfer"
    assert infer_self_transfer("transfer", "개인", "내 우리계좌 → 개인") == "transfer"
    assert infer_self_transfer("expense", "네이버파이낸셜", "-3,000원 네이버파이낸셜") == "expense"             # 화살표 없는 결제 행
    assert reconcile_kind("self_transfer", "out") == ("self_transfer", False)
    assert reconcile_kind("self_transfer", "in") == ("self_transfer", False)


def test_postprocess_replaces_owner_name_and_lowers_confidence_for_masked():
    tx = postprocess_row(_row(merchant="홍길동", raw_text="홍길동 5,000원 → 내 토스뱅크 통장", date="10-01", kind="transfer"),
                         0, "a", "2026-10", "", owner_name="홍길동")
    assert tx.merchant == "본인" and "홍길동" not in tx.raw_text and tx.confidence == "high"
    tx = postprocess_row(_row(merchant="홍*동", raw_text="홍*동 5,000원", date="10-01", kind="transfer"),
                         0, "a", "2026-10", "", owner_name="홍길동")
    assert tx.merchant == "본인" and tx.confidence == "medium"
    # 이름을 안 넘기면 아무것도 바꾸지 않음
    tx = postprocess_row(_row(merchant="홍길동", date="10-01"), 0, "a", "2026-10", "")
    assert tx.merchant == "홍길동"


def test_self_transfer_with_unknown_direction_is_not_gated():
    tx = postprocess_row(_row(merchant="KB국민", raw_text="1,031,390원 내 계좌 이체 | 본인 → KB국민",
                             direction="unknown", kind="transfer", date="10-15"), 0, "a", "2026-10", "")
    assert tx.kind == "self_transfer" and "unknown_direction" not in tx.flags
    tx = postprocess_row(_row(direction="unknown", kind="transfer", date="10-15"), 0, "a", "2026-10", "")
    assert "unknown_direction" in tx.flags


def test_scrub_owner_name_respects_hangul_boundaries():
    assert scrub_owner_name("하나은행 ATM 출금", "하나") == ("하나은행 ATM 출금", False)       # 이름이 상호의 일부
    assert scrub_owner_name("서현역 스타벅스", "서현") == ("서현역 스타벅스", False)
    assert scrub_owner_name("하나 → 내 토스뱅크 통장", "하나") == ("본인 → 내 토스뱅크 통장", False)
    assert scrub_owner_name("이체 하나(생활비)", "하나") == ("이체 본인(생활비)", False)
    assert scrub_owner_name("홍길동은행", "홍길동") == ("홍길동은행", False)
    assert scrub_owner_name("김홍길동", "홍길동") == ("김홍길동", False)
    assert scrub_owner_name("홍*동은행", "홍길동") == ("홍*동은행", False)
    assert scrub_owner_name("가나다라", "가") == ("가나다라", False)                            # 한 글자 이름은 무시
    assert scrub_owner_name("홍길동님 입금", "홍길동") == ("본인 입금", False)                   # 존칭은 이름과 함께 치환
    assert scrub_owner_name("홍*동님 입금", "홍길동") == ("본인 입금", True)
