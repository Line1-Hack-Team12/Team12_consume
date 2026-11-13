"""
F11·F15·F16 설명 생성: 백엔드가 계산한 패턴 수치 → 카드 문장·가설 질문·행동 제안·태그 추천·총평·리포트.

계약은 F03_INTERFACE.md 7절. 핵심 약속:
  - 숫자와 거래 ID 는 입력을 그대로 쓴다. 서비스가 출력 문장의 숫자를 전부 뽑아 입력 숫자 집합과 대조하고,
    evidence_transaction_ids 가 그 패턴의 증거 id 부분집합인지 확인한다. 어긋나면 1회 교정 재시도, 그래도 틀리면 NarrateInvalid.
  - 말투(coach/friend/grandma)는 표현만 바꾼다. 모델이 거부하면 중립 말투로 한 번 더 부르고 응답 tone 을 "neutral" 로 적는다.
  - recommended_tags 는 F13 허용값 중 3개. 모델이 틀리게 내면 서비스가 교정한다 (재시도 사유 아님).
  - 모델에 보내는 것은 수치·가맹점명·거래 id 뿐이다. 이름·계좌·잔액은 애초에 요청에 없다.

F03·F07 과 같은 호출 방식(JSON 스키마 출력, 시스템 프롬프트 캐시, 서버측 폴백). 글을 쓰는 일이라 effort 는 medium.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field, field_validator

import llm_common
from llm_common import OutputInvalid, parse_or_errors as _parse_or_errors

log = logging.getLogger("f11.narrate")

MODEL = os.environ.get("F11_MODEL", os.environ.get("F03_MODEL", "claude-sonnet-5-5"))
EFFORT = "medium"
MAX_TOKENS = 8000
PROMPT_VERSION = "n2"
REQUEST_TIMEOUT_SEC = 90.0

TONES = ("coach", "friend", "grandma")
WANTS = ("cards", "summary", "report")
PATTERN_TYPES = ("weekday", "late_night", "rain", "chain", "next_morning_chain", "payday")
TAGS = ("overtime", "rain", "stress", "habit", "no_specific_reason")
EVIDENCE_GRADES = ("strong", "normal")
MAX_PATTERNS = 10
MAX_EVIDENCE = 10
MAX_OVERVIEW = 3
MAX_ACTIONS = 3

WEEKDAY_KO = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
CATEGORY_KO = {"food": "식비", "delivery": "배달", "cafe": "카페", "shopping": "쇼핑", "transport": "교통",
               "living": "생활", "other": "기타"}
TYPE_KO = {"weekday": "특정 요일 반복", "late_night": "심야 반복", "rain": "비 오는 날 반복", "chain": "연쇄 결제",
           "next_morning_chain": "다음날 아침 후속 결제", "payday": "월급날 전후 반복"}
# 패턴 유형별 기본 태그 (모델이 틀리게 냈을 때 채우는 용도)
DEFAULT_TAGS = {"weekday": ["overtime", "stress", "habit"], "late_night": ["stress", "habit", "overtime"],
                "rain": ["rain", "habit", "no_specific_reason"], "chain": ["habit", "stress", "no_specific_reason"],
                "next_morning_chain": ["habit", "stress", "no_specific_reason"], "payday": ["stress", "habit", "no_specific_reason"]}


# ---------------------------------------------------------------- 요청 모델

class EvidenceTx(BaseModel):
    id: str
    date: str
    time: str | None = None
    merchant: str
    amount_krw: int
    category: str | None = None
    subcategory: str | None = None
    lag_minutes: int | None = None


class Observed(BaseModel):
    hit: int
    window: int
    unit: str = "주"


class Pattern(BaseModel):
    pattern_id: str
    type: str
    condition: dict[str, Any] = Field(default_factory=dict)
    observed: Observed
    sum_krw: int
    chained_sum_krw: int = 0
    evidence_grade: str = "normal"
    evidence_transactions: list[EvidenceTx] = Field(..., min_length=1, max_length=MAX_EVIDENCE)
    chain_transactions: list[EvidenceTx] = Field(default_factory=list, max_length=MAX_EVIDENCE)

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v not in PATTERN_TYPES:
            raise ValueError(f"type 은 {PATTERN_TYPES} 중 하나")
        return v

    @field_validator("evidence_grade")
    @classmethod
    def _grade(cls, v: str) -> str:
        return v if v in EVIDENCE_GRADES else "normal"

    @field_validator("evidence_transactions")
    @classmethod
    def _unique_ids(cls, v: list[EvidenceTx]) -> list[EvidenceTx]:
        ids = [t.id for t in v]
        if len(set(ids)) != len(ids):
            raise ValueError("evidence_transactions 의 id 가 중복")
        return v


class Period(BaseModel):
    start: str
    end: str
    slots: list[str] = Field(default_factory=list)


class CategoryShare(BaseModel):
    category: str
    sum_krw: int
    share: float | None = None


class MonthSum(BaseModel):
    month: str
    sum_krw: int


class Limits(BaseModel):
    days_without_upload: int = 0
    transactions_without_time: int = 0
    weather_skipped: bool = False


class Summary(BaseModel):
    total_spend_krw: int
    by_category: list[CategoryShare] = Field(default_factory=list)
    by_month: list[MonthSum] = Field(default_factory=list)
    limits: Limits = Field(default_factory=Limits)


class Subscription(BaseModel):
    merchant: str
    monthly_krw: int
    observed_months: int = 1
    price_increased: bool = False


class NarrateRequest(BaseModel):
    snapshot_id: str
    tone: Literal["coach", "friend", "grandma"]
    period: Period
    want: list[Literal["cards", "summary", "report"]] = Field(..., min_length=1)
    patterns: list[Pattern] = Field(default_factory=list, max_length=MAX_PATTERNS)
    summary: Summary | None = None
    subscriptions: list[Subscription] = Field(default_factory=list)

    @field_validator("patterns")
    @classmethod
    def _unique_pattern_ids(cls, v: list[Pattern]) -> list[Pattern]:
        ids = [p.pattern_id for p in v]
        if len(set(ids)) != len(ids):
            raise ValueError("pattern_id 중복")
        return v


class NarrateInvalid(OutputInvalid):
    """교정 재시도 후에도 출력이 계약을 어김. errors 에 사유 목록."""


# ---------------------------------------------------------------- 출력 스키마

CARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern_id": {"type": "string"},
        "title": {"type": "string", "description": "카드 제목. 20자 이내, 관찰 사실만"},
        "observation": {"type": "string", "description": "관찰 사실 1~2문장. 횟수·금액은 입력 숫자 그대로"},
        "hypothesis_question": {"type": "string", "description": "원인을 단정하지 않는 질문 한 문장"},
        "action": {"type": "string", "description": "다음 행동 제안 한 문장"},
        "recommended_tags": {"type": "array", "items": {"type": "string", "enum": list(TAGS)},
                             "description": "정확히 3개"},   # 구조화 출력은 minItems 2 이상을 거부해 개수는 서비스가 교정
        "evidence_transaction_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["pattern_id", "title", "observation", "hypothesis_question", "action", "recommended_tags",
                 "evidence_transaction_ids"],
    "additionalProperties": False,
}

NARRATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cards": {"type": "array", "items": CARD_SCHEMA},
        "summary_line": {"type": ["string", "null"], "description": "결과 홈 요약 한 줄"},
        "report": {
            "type": ["object", "null"],
            "properties": {
                "overview": {"type": "array", "items": {"type": "string"}, "description": "3문장 이내"},
                "observations": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"pattern_id": {"type": "string"}, "sentence": {"type": "string"}},
                    "required": ["pattern_id", "sentence"], "additionalProperties": False}},
                "actions": {"type": "array", "items": {"type": "string"}, "minItems": 1, "description": "1~3개"},
                "limits_sentence": {"type": "string"},
            },
            "required": ["overview", "observations", "actions", "limits_sentence"],
            "additionalProperties": False,
        },
    },
    "required": ["cards", "summary_line", "report"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """당신은 소비 기록 분석 서비스의 글쓰기 담당입니다. 분석은 이미 끝났고, 당신은 주어진 수치를 문장으로 옮기기만 합니다.
출력은 JSON 스키마를 따릅니다.

절대 규칙 (서버가 기계적으로 검사합니다. 어기면 출력 전체가 버려집니다)
- 숫자는 사용자 메시지의 "쓸 수 있는 숫자" 목록에 있는 것만 씁니다. 반올림·어림("약 29만 원", "거의 매주")·새 계산을 하지 않습니다.
  금액은 "286,000원" 처럼 전체 자릿수와 쉼표로 적고 "만 원" 단위로 줄이지 않습니다. 횟수는 "13주 중 11회" 처럼 입력 그대로.
  비율(%)·배수·평균은 목록에 없으면 쓰지 않습니다. 날짜를 적을 때는 증거 거래에 있는 날짜만 씁니다.
  기간·월은 "8월부터 10월까지", "9월 621,500원" 처럼 사람이 읽는 표기로 적습니다 ("2026-08" 그대로 쓰지 않음).
- evidence_transaction_ids 는 그 패턴의 증거 거래 id 중에서만 고릅니다. 다른 패턴의 id, 지어낸 id 는 안 됩니다. 3건 이상(증거가 3건 미만이면 전부).
- title, observation 은 관찰 사실만 적습니다. "비 오는 날 배달 2건" 이지 "비 때문에 배달함" 이 아닙니다. 원인·감정 상태를 단정하지 않습니다.
- hypothesis_question 은 원인을 묻는 질문 한 문장. 답을 정해 두지 않습니다 ("스트레스 때문이죠?" 금지).
- action 은 다음에 해볼 행동 한 문장. 구체적이고 작게 ("목요일 저녁엔 주문 전에 냉장고를 한 번 열어 보세요").
- recommended_tags 는 overtime(야근), rain(비 와서), stress(스트레스), habit(습관), no_specific_reason(그냥) 중 이 패턴 조건에 어울리는 3개.
- "유의하다", "확률", "통계적으로" 같은 말은 쓰지 않습니다. 횟수를 그대로 보여주는 것이 이 서비스의 방식입니다.
- 사람 이름·계좌·잔액은 입력에 없고 출력에도 쓰지 않습니다. 가맹점명은 입력 표기 그대로.
- 패턴을 부를 때는 분류 이름("기타·여가 결제")보다 증거 거래의 가맹점이 말해 주는 것을 씁니다. 증거가 PC방들이면 "PC방",
  배달 앱들이면 "배달", 술집이면 "술자리" 처럼 사람이 쓰는 말로. 가맹점이 여러 종류면 그때 분류 이름을 씁니다.

말투 (표현만 바뀌고 사실·숫자·id 는 같아야 합니다)
- coach: 담백한 코치. 존댓말, 판단 없이 사실과 다음 행동. "~했어요", "~해 보세요".
- friend: 친한 친구. 반말, 가볍고 편한 말. "~했네", "~해 봐". 놀리되 비하하지 않음.
- grandma: 직설적인 할머니. 반말, 구수하고 거침없는 훈수. "~했구나", "~해라". 거친 입담은 소비 행동에만 향하고
  사람 자체를 깎아내리거나 혐오·모욕 표현은 쓰지 않습니다.
- neutral: 중립. 존댓말, 꾸밈 없는 서술.

생성 범위
- cards: 패턴마다 카드 하나, 입력 순서대로. 요청에 없는 패턴 카드를 만들지 않습니다.
- summary_line: 결과 홈 맨 위 한 줄. 가장 큰 반복 지출 하나를 짚습니다.
- report: overview 는 3문장 이내(기간·합계·가장 큰 관찰), observations 는 패턴마다 한 문장(카드 observation 과 같은 사실을 다른 문장으로),
  actions 는 1~3개, limits_sentence 는 한계 수치(업로드 없는 날, 시각 없는 거래, 날씨 생략)를 한 문장으로.
- want 에 없는 항목은 cards=[] / summary_line=null / report=null 로 둡니다."""


# ---------------------------------------------------------------- 렌더링 (모델에 보낼 사람이 읽는 표현)

def _won(n: int) -> str:
    return f"{n:,}원"


def describe_condition(ptype: str, cond: dict[str, Any]) -> str:
    parts: list[str] = []
    wd = cond.get("weekday")
    if isinstance(wd, int) and 0 <= wd <= 6:
        parts.append(WEEKDAY_KO[wd])
    if cond.get("hour_from") is not None and cond.get("hour_to") is not None:
        parts.append(f"{cond['hour_from']}시~{cond['hour_to']}시")
    if cond.get("weather") == "rain" or ptype == "rain":
        parts.append("비 오는 날")
    if ptype == "payday":
        parts.append("월급날 전후")
    cat = cond.get("category")
    if cat:
        parts.append(f"{CATEGORY_KO.get(cat, cat)}({cat})" + (f"·{cond['subcategory']}" if cond.get("subcategory") else ""))
    known = {"weekday", "hour_from", "hour_to", "weather", "category", "subcategory"}
    for k, v in cond.items():
        if k not in known and v is not None:
            parts.append(f"{k}={v}")
    return " ".join(parts) or TYPE_KO.get(ptype, ptype)


def _tx_line(t: EvidenceTx) -> str:
    s = f"{t.id} | {t.date}{' ' + t.time if t.time else ''} | {t.merchant} | {_won(t.amount_krw)}"
    if t.category:
        s += f" | {CATEGORY_KO.get(t.category, t.category)}" + (f"·{t.subcategory}" if t.subcategory else "")
    if t.lag_minutes is not None:
        s += f" | {t.lag_minutes}분 뒤"
    return s


def _nums_from_date(s: str | None, out: set[float]) -> None:
    for tok in re.findall(r"\d+", s or ""):
        out.add(float(int(tok)))


def allowed_numbers_global(req: NarrateRequest) -> set[float]:
    """카드 밖(요약·리포트)에서 쓸 수 있는 숫자. 기간·합계·카테고리·월별·한계·구독."""
    nums: set[float] = set()
    _nums_from_date(req.period.start, nums)
    _nums_from_date(req.period.end, nums)
    for s in req.period.slots:
        _nums_from_date(s, nums)
    nums.add(float(len(req.period.slots)))
    if req.summary:
        nums.add(float(req.summary.total_spend_krw))
        for c in req.summary.by_category:
            nums.add(float(c.sum_krw))
            if c.share is not None:
                nums.add(float(round(c.share * 100)))
        for m in req.summary.by_month:
            nums.add(float(m.sum_krw))
            _nums_from_date(m.month, nums)
        nums.update({float(req.summary.limits.days_without_upload), float(req.summary.limits.transactions_without_time)})
    for s in req.subscriptions:
        nums.update({float(s.monthly_krw), float(s.observed_months)})
    if req.subscriptions:
        nums.add(float(sum(s.monthly_krw for s in req.subscriptions)))
        nums.add(float(len(req.subscriptions)))
    return nums


def allowed_numbers_pattern(p: Pattern) -> set[float]:
    nums: set[float] = {float(p.observed.hit), float(p.observed.window), float(p.sum_krw), float(p.chained_sum_krw),
                        float(p.sum_krw + p.chained_sum_krw), float(len(p.evidence_transactions)),
                        float(len(p.chain_transactions))}
    for k in ("hour_from", "hour_to"):
        v = p.condition.get(k)
        if isinstance(v, (int, float)):
            nums.add(float(v))
    for t in p.evidence_transactions + p.chain_transactions:
        nums.add(float(t.amount_krw))
        _nums_from_date(t.date, nums)
        _nums_from_date(t.time, nums)
    return nums


# 쉼표는 뒤에 숫자 딱 3자리가 올 때만 천 단위 구분자 ("1,000원"). "8,9월" 은 8 과 9 두 숫자
_RE_NUM = re.compile(r"\d{1,3}(?:,\d{3})+(?!\d)(?:\.\d+)?|\d+(?:\.\d+)?")


def numbers_in(text: str) -> list[float]:
    out = []
    for m in _RE_NUM.findall(text or ""):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def _render_request(req: NarrateRequest, tone: str) -> str:
    L: list[str] = []
    L.append(f"말투: {tone}")
    L.append(f"생성할 것(want): {', '.join(req.want)}")
    L.append(f"기간: {req.period.start} ~ {req.period.end} (슬롯 {len(req.period.slots)}개: {', '.join(req.period.slots)})")
    L.append("")
    for i, p in enumerate(req.patterns, 1):
        L.append(f"[패턴 {i}] pattern_id={p.pattern_id} 유형={TYPE_KO.get(p.type, p.type)}({p.type}) 근거등급={p.evidence_grade}")
        L.append(f"  조건: {describe_condition(p.type, p.condition)}")
        L.append(f"  관찰: {p.observed.window}{p.observed.unit} 중 {p.observed.hit}회")
        L.append(f"  직접 비용 {_won(p.sum_krw)}, 후속 비용 {_won(p.chained_sum_krw)}, 합계 {_won(p.sum_krw + p.chained_sum_krw)}")
        L.append(f"  증거 거래 {len(p.evidence_transactions)}건 (evidence_transaction_ids 는 여기서만 고르세요):")
        for t in p.evidence_transactions:
            L.append("    " + _tx_line(t))
        if p.chain_transactions:
            L.append(f"  후속 결제 {len(p.chain_transactions)}건 (후속 비용의 근거, id 로 쓰지 않음):")
            for t in p.chain_transactions:
                L.append("    " + _tx_line(t))
        nums = sorted(allowed_numbers_pattern(p))
        L.append("  이 카드에서 쓸 수 있는 숫자: " + ", ".join(_fmt_num(n) for n in nums))
        L.append("")
    if req.summary:
        s = req.summary
        L.append(f"[요약] 총 지출 {_won(s.total_spend_krw)}")
        if s.by_category:
            L.append("  카테고리별: " + ", ".join(
                f"{CATEGORY_KO.get(c.category, c.category)} {_won(c.sum_krw)}" + (f"({round(c.share * 100)}%)" if c.share is not None else "")
                for c in s.by_category))
        if s.by_month:
            L.append("  월별: " + ", ".join(f"{m.month} {_won(m.sum_krw)}" for m in s.by_month))
        L.append(f"  한계: 업로드 없는 날 {s.limits.days_without_upload}일, 시각 없는 거래 {s.limits.transactions_without_time}건, "
                 f"날씨 분석 {'생략' if s.limits.weather_skipped else '포함'}")
    if req.subscriptions:
        L.append("[정기결제] " + ", ".join(
            f"{x.merchant} 월 {_won(x.monthly_krw)} ({x.observed_months}개월 관찰{', 가격 인상' if x.price_increased else ''})"
            for x in req.subscriptions) + f" / 월 합계 {_won(sum(x.monthly_krw for x in req.subscriptions))}")
    g = sorted(allowed_numbers_global(req))
    L.append("요약·리포트에서 쓸 수 있는 숫자: " + ", ".join(_fmt_num(n) for n in g) + " (각 카드의 숫자도 그 패턴을 언급할 때 사용 가능)")
    return "\n".join(L)


def _fmt_num(n: float) -> str:
    return f"{int(n):,}" if n == int(n) else f"{n}"


# ---------------------------------------------------------------- 검증

def normalize_tags(tags: Any, ptype: str) -> list[str]:
    out: list[str] = []
    for t in tags if isinstance(tags, list) else []:
        if t in TAGS and t not in out:
            out.append(t)
    for t in DEFAULT_TAGS.get(ptype, DEFAULT_TAGS["weekday"]) + list(TAGS):
        if len(out) >= 3:
            break
        if t not in out:
            out.append(t)
    return out[:3]


def _check_numbers(scope: str, text: str, allowed: set[float], errors: list[str]) -> None:
    bad = sorted({n for n in numbers_in(text) if n not in allowed})
    if bad:
        errors.append(f"{scope}: 입력에 없는 숫자 {', '.join(_fmt_num(b) for b in bad)} → 문장: {text[:80]}")


def validate_output(req: NarrateRequest, out: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """모델 출력을 계약에 맞춰 검사·교정. (교정된 출력, 재시도가 필요한 오류 목록) 을 돌려준다.

    교정만 하고 넘어가는 것: 태그, 카드 순서, 빈 문자열 trim, overview·actions 개수 초과(자름).
    오류로 잡는 것: 숫자 불일치, 증거 id 가 아님·부족, 없는 pattern_id, 빠진 카드, 빈 필수 문장.
    """
    errors: list[str] = []
    by_id = {p.pattern_id: p for p in req.patterns}
    glob = allowed_numbers_global(req)
    all_pattern_nums: set[float] = set().union(*(allowed_numbers_pattern(p) for p in req.patterns)) if req.patterns else set()
    fixed: dict[str, Any] = {"cards": [], "summary_line": None, "report": None}

    if "cards" in req.want:
        cards_in = {c.get("pattern_id"): c for c in (out.get("cards") or []) if isinstance(c, dict)}
        for pid in cards_in:
            if pid not in by_id:
                errors.append(f"cards: 요청에 없는 pattern_id {pid!r}")
        for p in req.patterns:
            c = cards_in.get(p.pattern_id)
            if c is None:
                errors.append(f"cards: pattern_id {p.pattern_id!r} 카드가 없음")
                continue
            allowed = allowed_numbers_pattern(p) | glob
            card = {"pattern_id": p.pattern_id}
            for f in ("title", "observation", "hypothesis_question", "action"):
                v = (c.get(f) or "").strip()
                if not v:
                    errors.append(f"cards[{p.pattern_id}].{f}: 비어 있음")
                card[f] = v
                _check_numbers(f"cards[{p.pattern_id}].{f}", v, allowed, errors)
            card["recommended_tags"] = normalize_tags(c.get("recommended_tags"), p.type)
            ev_ok = {t.id for t in p.evidence_transactions}
            ids = [i for i in (c.get("evidence_transaction_ids") or []) if isinstance(i, str)]
            unknown = [i for i in ids if i not in ev_ok]
            if unknown:
                errors.append(f"cards[{p.pattern_id}].evidence_transaction_ids: 증거가 아닌 id {unknown}")
            kept = list(dict.fromkeys(i for i in ids if i in ev_ok))
            need = min(3, len(ev_ok))
            if len(kept) < need:
                errors.append(f"cards[{p.pattern_id}].evidence_transaction_ids: {need}건 이상 필요 (지금 {len(kept)}건)")
            card["evidence_transaction_ids"] = kept
            fixed["cards"].append(card)

    if "summary" in req.want:
        line = (out.get("summary_line") or "").strip()
        if not line:
            errors.append("summary_line: 비어 있음")
        _check_numbers("summary_line", line, glob | all_pattern_nums, errors)
        fixed["summary_line"] = line

    if "report" in req.want:
        r = out.get("report") if isinstance(out.get("report"), dict) else {}
        overview = [s.strip() for s in (r.get("overview") or []) if isinstance(s, str) and s.strip()][:MAX_OVERVIEW]
        if not overview:
            errors.append("report.overview: 비어 있음")
        for i, s in enumerate(overview):
            _check_numbers(f"report.overview[{i}]", s, glob | all_pattern_nums, errors)
        obs_out = []
        for o in (r.get("observations") or []):
            if not isinstance(o, dict):
                continue
            pid, sent = o.get("pattern_id"), (o.get("sentence") or "").strip()
            if pid not in by_id:
                errors.append(f"report.observations: 요청에 없는 pattern_id {pid!r}")
                continue
            if not sent:
                errors.append(f"report.observations[{pid}]: 비어 있음")
            _check_numbers(f"report.observations[{pid}]", sent, allowed_numbers_pattern(by_id[pid]) | glob, errors)
            obs_out.append({"pattern_id": pid, "sentence": sent})
        actions = [s.strip() for s in (r.get("actions") or []) if isinstance(s, str) and s.strip()][:MAX_ACTIONS]
        if not actions:
            errors.append("report.actions: 비어 있음")
        for i, s in enumerate(actions):
            _check_numbers(f"report.actions[{i}]", s, glob | all_pattern_nums, errors)
        limits = (r.get("limits_sentence") or "").strip()
        if not limits:
            errors.append("report.limits_sentence: 비어 있음")
        _check_numbers("report.limits_sentence", limits, glob, errors)
        fixed["report"] = {"overview": overview, "observations": obs_out, "actions": actions, "limits_sentence": limits}

    return fixed, errors


# ---------------------------------------------------------------- 호출

class _ModelOut(BaseModel):
    cards: list[dict[str, Any]] = Field(default_factory=list)
    summary_line: str | None = None
    report: dict[str, Any] | None = None


async def _call(client: anthropic.AsyncAnthropic, messages: list[dict[str, Any]]) -> anthropic.types.Message:
    return await llm_common.call_json(
        client, model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT, messages=messages,
        effort=EFFORT, schema=NARRATE_SCHEMA, timeout=REQUEST_TIMEOUT_SEC,
    )


_CORRECTION = ("출력이 규칙을 어겼습니다. 아래 항목만 고쳐서 전체 JSON 을 다시 출력하세요. "
               "숫자는 '쓸 수 있는 숫자' 목록의 값만, 증거 id 는 그 패턴의 증거 거래 id 만 쓰세요.")


async def _generate(client: anthropic.AsyncAnthropic, req: NarrateRequest, tone: str) -> dict[str, Any]:
    """한 말투로 생성 + 검증. 검증 실패 시 오류 목록을 돌려주며 1회 교정. 그래도 실패면 NarrateInvalid. 거부는 llm_common.Refused."""
    return await llm_common.generate_validated(
        lambda messages: _call(client, messages),
        [{"role": "user", "content": _render_request(req, tone)}],
        _ModelOut, lambda out: validate_output(req, out), _CORRECTION, f"narrate tone={tone}", NarrateInvalid,
    )


async def narrate(req: NarrateRequest, client: anthropic.AsyncAnthropic | None = None) -> dict[str, Any]:
    """요청 말투로 생성. 모델이 거부하면 중립 말투로 한 번 더 시도하고 tone='neutral' 로 표시."""
    client = client or llm_common.get_client()
    out, tone = await llm_common.with_neutral_fallback(lambda t: _generate(client, req, t), req.tone, "narrate", NarrateInvalid)
    out.update({"model": MODEL, "prompt_version": PROMPT_VERSION, "tone": tone, "snapshot_id": req.snapshot_id})
    return out
