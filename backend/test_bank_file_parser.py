# -*- coding: utf-8 -*-
"""bank_file_parser 테스트. 실제 내보내기 파일 대신 같은 모양의 가짜 워크북을 메모리에서 만든다."""
import io
import json

import pytest
from openpyxl import Workbook

from bank_file_parser import (
    EncryptedFileError, UnsupportedFileError, parse_bank_file, parse_toss_xlsx, parsed_to_payload,
    _looks_like_person, _replace_person,
)

HEADER = ["거래 일시", "적요", "거래 유형", "거래 기관", "계좌번호", "거래 금액", "거래 후 잔액", "메모"]


def _toss_xlsx(rows, with_meta=True):
    wb = Workbook()
    ws = wb.active
    ws.title = "토스뱅크 거래내역"
    if with_meta:
        ws.append([None, "토스뱅크 거래내역"])
        ws.append([None, "성명", "홍길동"])
        ws.append([None, "계좌번호", "****-****-1234"])
        ws.append([None, "조회기간", "2026.10.01 - 2026.10.31"])
        ws.append([])
        ws.append([None, "※ 본 거래내역은 법적효력이 없는 참고용 문서입니다."])
        ws.append([])
    ws.append([None] + HEADER)
    for r in rows:
        ws.append([None] + list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


ROWS = [
    ("2026.10.27 21:10:05", "GS25 역삼점", "체크카드결제", "", "", -4500, 1234567, ""),
    ("2026.10.27 21:12:00", "GS25 역삼점", "체크카드결제", "", "", 4500, 1239067, ""),          # 취소
    ("2026.10.26 09:00:00", "카드 캐시백", "프로모션입금", "", "", 43, 1239110, ""),
    ("2026.10.25 12:00:00", "김철수", "입금", "", "110-123-456789", 50000, 1289110, "생활비"),  # 타인 → 개인
    ("2026.10.25 12:30:00", "홍길동", "오픈뱅킹", "KB국민은행", "3333-01-1234567", -200000, 1089110, ""),  # 본인 → self
    ("2026.10.24 08:00:00", "키워봐요 자동이체 저축(1234)", "내계좌간자동이체", "토스뱅크", "", -30000, 1059110, ""),
    ("2026.10.23 10:00:00", "LGU+12345678", "지로출금", "", "", -16000, 1043110, ""),
    ("2026.10.22 10:00:00", "이체 박*수", "오픈뱅킹", "", "", -10000, 1033110, ""),
    ("2026.10.21 10:00:00", "카카오페이", "펌뱅킹출금", "", "", -12000, 1021110, ""),
    ("2026.09.30 23:59:00", "통장 이자", "이자입금", "", "", 123, 1021233, ""),
    ("", "깨진 행", "체크카드결제", "", "", -100, 0, ""),
    ("2026.10.20 10:00:00", "금액 없음", "체크카드결제", "", "", None, 0, ""),
]


def test_parse_reads_only_needed_columns_and_never_balance_or_account():
    p = parse_toss_xlsx(_toss_xlsx(ROWS), "f1", owner_name="홍길동")
    assert len(p.rows) == 10 and p.skipped_rows == 2
    text = json.dumps(parsed_to_payload(p, "f1"), ensure_ascii=False)
    for forbidden in ["1234567", "1239067", "110-123-456789", "3333-01", "생활비", "홍길동", "김철수", "박*수", "12345678"]:
        assert forbidden not in text, forbidden
    assert p.period == ("2026-09-30", "2026-10-27")


def test_kind_and_direction_mapping():
    p = parse_toss_xlsx(_toss_xlsx(ROWS), "f1", owner_name="홍길동")
    by = {(r.amount_krw, r.date): r for r in p.rows}
    assert by[(4500, "2026-10-27")].kind in ("expense", "refund")          # 같은 키 두 건 → 아래서 따로 확인
    kinds = sorted((r.amount_krw, r.direction, r.kind) for r in p.rows if r.amount_krw == 4500)
    assert kinds == [(4500, "in", "refund"), (4500, "out", "expense")]
    assert by[(43, "2026-10-26")].kind == "income"
    assert by[(50000, "2026-10-25")].kind == "transfer" and by[(50000, "2026-10-25")].merchant == "개인"
    assert by[(200000, "2026-10-25")].kind == "self_transfer" and by[(200000, "2026-10-25")].merchant == "본인"
    assert by[(30000, "2026-10-24")].kind == "self_transfer"
    assert by[(16000, "2026-10-23")].kind == "expense" and by[(16000, "2026-10-23")].merchant == "LGU+[번호]"
    assert by[(10000, "2026-10-22")].merchant == "이체 개인"
    assert by[(12000, "2026-10-21")].merchant == "카카오페이" and by[(12000, "2026-10-21")].kind == "transfer"
    assert by[(123, "2026-09-30")].kind == "income" and by[(123, "2026-09-30")].time == "23:59"
    assert all(r.confidence == "high" and not r.flags for r in p.rows)


def test_person_heuristic():
    assert _looks_like_person("김철수") and _looks_like_person("이체 박*수") and _looks_like_person("홍**")
    assert not _looks_like_person("카카오페이") and not _looks_like_person("배민") and not _looks_like_person("토스증권")
    assert not _looks_like_person("GS25") and not _looks_like_person("ATM출금")
    assert _replace_person("홍길동", "홍길동") == "본인"
    assert _replace_person("홍*동", "홍길동") == "본인"
    assert _replace_person("김철수", "홍길동") == "개인"
    assert _replace_person("네이버파이낸셜", "홍길동") == "네이버파이낸셜"


def test_payload_groups_by_month_and_filters():
    p = parse_toss_xlsx(_toss_xlsx(ROWS), "f1", owner_name="홍길동")
    all_months = parsed_to_payload(p, "f1")
    assert [a["slot_month"] for a in all_months["assets"]] == ["2026-09", "2026-10"]
    assert all_months["status"] == "succeeded" and all_months["source_format"] == "toss_bank_xlsx"
    oct_only = parsed_to_payload(p, "f1", months=["2026-10", "2026-11"])
    assert [(a["asset_id"], a["status"], len(a["rows"])) for a in oct_only["assets"]] == \
        [("f1:2026-10", "succeeded", 9), ("f1:2026-11", "skipped", 0)]
    assert parsed_to_payload(p, "f1", months=["2025-01"])["status"] == "no_transactions"
    ids = [r["id"] for a in all_months["assets"] for r in a["rows"]]
    assert len(set(ids)) == len(ids)
    again = parse_toss_xlsx(_toss_xlsx(ROWS), "f1", owner_name="홍길동")
    assert [r.id for r in again.rows] == [r.id for r in p.rows]                       # 결정적 id


def test_header_found_without_meta_block():
    p = parse_toss_xlsx(_toss_xlsx(ROWS[:3], with_meta=False), "f1")
    assert len(p.rows) == 3


def test_errors():
    with pytest.raises(EncryptedFileError):
        parse_bank_file(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64, "a.xlsx", "f1")
    with pytest.raises(UnsupportedFileError):
        parse_bank_file(b"not a workbook", "a.xlsx", "f1")
    with pytest.raises(UnsupportedFileError):
        parse_bank_file(b"PK", "a.pdf", "f1")
    wb = Workbook()
    wb.active.append(["이상한", "머리글"])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(UnsupportedFileError):
        parse_bank_file(buf.getvalue(), "a.xlsx", "f1")


def test_encrypted_file_opens_with_password_and_rejects_wrong_one():
    from msoffcrypto.format.ooxml import OOXMLFile
    out = io.BytesIO()
    OOXMLFile(io.BytesIO(_toss_xlsx(ROWS))).encrypt("123456", out)
    enc = out.getvalue()
    assert len(parse_bank_file(enc, "a.xlsx", "f1", password="123456").rows) == 10
    with pytest.raises(EncryptedFileError) as e:
        parse_bank_file(enc, "a.xlsx", "f1", password="000000")
    assert "000000" not in str(e.value)                                           # 오류 메시지에 암호 없음
    with pytest.raises(EncryptedFileError):
        parse_bank_file(enc, "a.xlsx", "f1")
