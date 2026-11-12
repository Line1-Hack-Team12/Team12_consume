"""
은행 앱이 내보낸 거래내역 파일 → F03 과 같은 거래 후보 JSON.

지원 형식
  - 토스뱅크 거래내역 (xlsx). 앱: 토스뱅크 계좌 → 관리 → 문서 → 거래내역서 → 엑셀.
    발급 파일은 열기 암호가 걸려 있다 (OOXML 암호화). 호출자가 암호를 주면 메모리에서 풀고 저장하지 않는다.

원칙 (F03 과 같음)
  - 읽는 열만 읽는다: 거래 일시, 적요, 거래 유형, 거래 기관, 거래 금액.
    읽지 않는 것: 거래 후 잔액, 계좌번호(상대 계좌), 메모, 머리글 영역의 성명·계좌번호·조회기간.
  - 사람 이름은 저장하지 않는다: 이체류 거래의 적요가 사람 이름이면 "개인", 캡처 주인이면 "본인".
  - 잔액·긴 번호는 F03 의 scrub_sensitive 로 한 번 더 거른다.
  - 파일 파싱은 결정적이라 confidence 는 전부 "high", 날짜·금액 플래그는 생기지 않는다.
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from openpyxl import load_workbook

from f03_extraction import (
    TransactionCandidate, infer_self_transfer, scrub_owner_name, scrub_sensitive,
)

PARSER_VERSION = "bank-0.1"

# ---------------------------------------------------------------------------
# 토스뱅크 xlsx 형식 정의
# ---------------------------------------------------------------------------

TOSS_REQUIRED_HEADERS = ("거래 일시", "적요", "거래 유형", "거래 금액")
TOSS_READ_COLUMNS = ("거래 일시", "적요", "거래 유형", "거래 기관", "거래 금액")   # 이 외의 열은 읽지 않음

# 거래 유형 → kind. 방향은 금액 부호로 정한다.
_TOSS_KIND_BY_TYPE: dict[str, str] = {
    "체크카드결제": "expense",          # 양수면 refund (취소·환불)
    "프로모션입금": "income",
    "이자입금": "income",
    "내계좌간자동이체": "self_transfer",
    "내계좌간이체": "self_transfer",
    "자동이체": "expense",              # 공과금·구독 자동 납부
    "CMS출금": "expense",
    "지로출금": "expense",
    "입금": "transfer",
    "출금": "transfer",
    "오픈뱅킹": "transfer",
    "펌뱅킹출금": "transfer",
}
# 이 유형들은 적요가 상대방(사람 또는 기관)이라 이름 치환 대상
_TOSS_COUNTERPARTY_TYPES = {"입금", "출금", "오픈뱅킹", "펌뱅킹출금", "자동이체", "CMS출금"}

# 상호로 보는 단서. 이게 들어 있으면 사람 이름으로 보지 않는다.
_BUSINESS_HINTS = ("페이", "뱅크", "증권", "카드", "보험", "통신", "주식회사", "(주)", "㈜", "회사", "캐피탈",
                   "저축", "은행", "금융", "파이낸셜", "머니", "마켓", "몰", "컴퍼니", "서비스", "ATM", "협", "공단",
                   "학교", "대학", "병원", "약국", "센터", "스토어", "샵", "숍", "점", "텔레콤", "모바일")
_RE_FULL_NAME = re.compile(r"^[가-힣]{3}$")                      # 한글 3자 단독 → 사람 이름으로 간주
_RE_MASKED_NAME = re.compile(r"(?<![가-힣])[가-힣]{1,2}[*○●]{1,3}[가-힣]?(?![가-힣])")   # 홍*동, 홍**, 김*


_RE_LONG_NUMBER = re.compile(r"\d{7,}")


def _strip_long_numbers(text: str) -> str:
    """지로·자동이체 적요의 고객번호("LGU+12345678", "KT0101234567")를 지운다. 전화번호일 수 있고 가맹점 식별에 불필요."""
    return re.sub(r"\s{2,}", " ", _RE_LONG_NUMBER.sub("[번호]", text)).strip()


def _looks_like_person(text: str) -> bool:
    t = text.strip()
    if any(h in t for h in _BUSINESS_HINTS):
        return False
    return bool(_RE_FULL_NAME.match(t)) or bool(_RE_MASKED_NAME.search(t))


def _replace_person(text: str, owner_name: str) -> str:
    """상대방 표기가 사람이면 "개인"(본인이면 "본인")으로. 상호면 그대로."""
    scrubbed, _ = scrub_owner_name(text, owner_name)
    if scrubbed != text:                      # 주인 이름(또는 가려진 주인 이름)이 들어 있었음
        return scrubbed
    if not _looks_like_person(text):
        return text
    if _RE_FULL_NAME.match(text.strip()):
        return "개인"
    return _RE_MASKED_NAME.sub("개인", text)


# ---------------------------------------------------------------------------
# 결과
# ---------------------------------------------------------------------------


@dataclass
class ParsedFile:
    source_format: str
    period: tuple[str, str] | None            # (YYYY-MM-DD, YYYY-MM-DD) 데이터 행 기준 (머리글의 조회기간은 읽지 않음)
    rows: list[TransactionCandidate]
    skipped_rows: int                          # 날짜·금액을 못 읽어 버린 행 수

    def by_month(self) -> dict[str, list[TransactionCandidate]]:
        out: dict[str, list[TransactionCandidate]] = {}
        for r in self.rows:
            out.setdefault(r.date[:7], []).append(r)
        return dict(sorted(out.items()))


class UnsupportedFileError(ValueError):
    pass


class EncryptedFileError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 읽기
# ---------------------------------------------------------------------------


def _open_workbook(data: bytes, password: str | None):
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":       # OLE 컨테이너: 암호화된 OOXML (또는 구형 xls)
        if not password:
            raise EncryptedFileError("파일에 열기 암호가 걸려 있습니다. 발급 시 안내된 비밀번호가 필요합니다.")
        import msoffcrypto                                     # 암호 있을 때만 import
        out = io.BytesIO()
        try:
            f = msoffcrypto.OfficeFile(io.BytesIO(data))
            f.load_key(password=password)
            f.decrypt(out)
        except Exception as e:                                 # 잘못된 암호, 암호화 아닌 구형 xls 등. 메시지에 암호를 넣지 않음
            raise EncryptedFileError(f"암호로 파일을 열 수 없습니다 ({type(e).__name__})")
        data = out.getvalue()
    if data[:2] != b"PK":
        raise UnsupportedFileError("xlsx 형식이 아닙니다")
    try:
        return load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:                                     # 깨진 zip, xlsx 가 아닌 zip 등
        raise UnsupportedFileError(f"xlsx 파일을 열 수 없습니다 ({type(e).__name__})")


def _find_header(rows: list[tuple[Any, ...]], required: tuple[str, ...], scan: int = 40) -> tuple[int, dict[str, int]]:
    """머리글 행을 찾아 (행 번호, 열이름→열번호) 를 돌려준다. 머리글 위쪽(성명·계좌번호 등)은 읽지 않는다."""
    for i, r in enumerate(rows[:scan]):
        cells = {str(v).strip(): j for j, v in enumerate(r) if isinstance(v, str)}
        if all(h in cells for h in required):
            return i, cells
    raise UnsupportedFileError("토스뱅크 거래내역 머리글(거래 일시·적요·거래 유형·거래 금액)을 찾지 못했습니다")


def _parse_datetime(v: Any) -> tuple[str, str | None] | None:
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d"), v.strftime("%H:%M")
    s = str(v or "").strip()
    m = re.match(r"^(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", s)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        datetime(y, mo, d)
    except ValueError:
        return None
    time = f"{int(m.group(4)):02d}:{m.group(5)}" if m.group(4) else None
    return f"{y:04d}-{mo:02d}-{d:02d}", time


def _parse_amount(v: Any) -> int | None:
    if isinstance(v, (int, float)):
        return int(round(v))
    s = re.sub(r"[,\s원]", "", str(v or ""))
    if not re.match(r"^[-+]?\d+$", s):
        return None
    return int(s)


def parse_toss_xlsx(data: bytes, asset_id: str, owner_name: str = "", password: str | None = None) -> ParsedFile:
    wb = _open_workbook(data, password)
    ws = wb[wb.sheetnames[0]]
    all_rows = list(ws.iter_rows(values_only=True))
    hi, col = _find_header(all_rows, TOSS_REQUIRED_HEADERS)
    c_dt, c_desc, c_type, c_amt = col["거래 일시"], col["적요"], col["거래 유형"], col["거래 금액"]
    c_org = col.get("거래 기관")

    rows: list[TransactionCandidate] = []
    skipped = 0
    for idx, r in enumerate(all_rows[hi + 1:]):
        dt = _parse_datetime(r[c_dt] if c_dt < len(r) else None)
        amt = _parse_amount(r[c_amt] if c_amt < len(r) else None)
        if dt is None or amt is None or amt == 0:
            skipped += 1
            continue
        date, time = dt
        tx_type = str(r[c_type] or "").strip()
        org = str(r[c_org] or "").strip() if c_org is not None and c_org < len(r) else ""
        desc = str(r[c_desc] or "").strip()

        if tx_type in _TOSS_COUNTERPARTY_TYPES:
            desc = _replace_person(desc, owner_name)
        else:
            desc, _ = scrub_owner_name(desc, owner_name)
        merchant = _strip_long_numbers(scrub_sensitive(desc))[:100]

        direction = "out" if amt < 0 else "in"
        kind = _TOSS_KIND_BY_TYPE.get(tx_type, "expense" if direction == "out" else "income")
        if kind == "expense" and direction == "in":
            kind = "refund"                                   # 체크카드결제 양수 = 취소·환불
        if kind == "transfer" and merchant == "본인":
            kind = "self_transfer"                             # 오픈뱅킹 등으로 본인 다른 계좌에 보낸 것
        raw_text = _strip_long_numbers(scrub_sensitive(" | ".join(x for x in (desc, tx_type, org) if x)))
        kind = infer_self_transfer(kind, merchant, raw_text)

        flags: list[str] = []
        if not merchant:
            flags.append("merchant_missing")
        slot_month = date[:7]
        source_row_id = f"{asset_id}:row{idx:05d}"
        rows.append(TransactionCandidate(
            id="tx_" + hashlib.sha1(f"{source_row_id}|{slot_month}".encode()).hexdigest()[:10],
            asset_id=asset_id, source_row_id=source_row_id,
            date=date, time=time, merchant=merchant, amount_krw=abs(amt),
            direction=direction, kind=kind, confidence="high", raw_text=raw_text, flags=flags,
        ))

    period = (min(r.date for r in rows), max(r.date for r in rows)) if rows else None
    return ParsedFile(source_format="toss_bank_xlsx", period=period, rows=rows, skipped_rows=skipped)


# ---------------------------------------------------------------------------
# 형식 판별 + 결과 직렬화
# ---------------------------------------------------------------------------


def parse_bank_file(data: bytes, filename: str, asset_id: str, owner_name: str = "",
                    password: str | None = None) -> ParsedFile:
    """파일 형식을 보고 맞는 파서로 보낸다. 지금은 토스뱅크 xlsx 만."""
    suffix = Path(filename or "").suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return parse_toss_xlsx(data, asset_id, owner_name, password)
    if suffix == ".xls":
        raise UnsupportedFileError("구형 xls 는 지원하지 않습니다. 은행 앱에서 xlsx 로 다시 내려받아 주세요")
    raise UnsupportedFileError(f"지원하지 않는 파일 형식: {suffix or '(없음)'}")


def parsed_to_payload(parsed: ParsedFile, asset_id: str, months: list[str] | None = None) -> dict[str, Any]:
    """F03 job JSON 과 같은 모양. 달마다 asset 하나 (asset_id = "<파일 asset_id>:<YYYY-MM>").

    months 를 주면 그 달들만 돌려준다 (세션의 슬롯 달). 안 주면 파일에 있는 모든 달.
    """
    by_month = parsed.by_month()
    wanted = list(dict.fromkeys(months)) if months else list(by_month)     # 같은 달이 두 번 오면 한 번만
    assets = []
    for m in wanted:
        rows = by_month.get(m, [])
        assets.append({
            "asset_id": f"{asset_id}:{m}", "slot_month": m,
            "status": "succeeded" if rows else "skipped",
            "error_code": None if rows else "NO_ROWS_IN_MONTH",
            "rows": [r.__dict__ for r in rows],
        })
    return {
        "job_id": "parse_" + hashlib.sha1(f"{asset_id}|{parsed.period}".encode()).hexdigest()[:8],
        "status": "succeeded" if any(a["rows"] for a in assets) else "no_transactions",
        "source": "file", "source_format": parsed.source_format, "parser_version": PARSER_VERSION,
        "period": parsed.period, "skipped_rows": parsed.skipped_rows,
        "assets": assets,
    }
