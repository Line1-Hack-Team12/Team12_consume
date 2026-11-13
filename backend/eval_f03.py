"""
F03 구조화 추출 채점기

추출 결과(extraction_result.json)를 사람이 만든 정답(truth/*.json)과 대조해
기능명세서 F03 수용 기준을 숫자로 찍습니다.

  - 금액 오독률  : 같은 거래로 짝지어졌지만 금액이 다른 비율 (목표 2% 이하)
  - 누락         : 정답에는 있는데 추출에 없는 건수 (목표 0)
  - 과잉         : 추출에는 있는데 정답에 없는 건수 (목표 0)
  - 날짜·시각·방향 일치율, 가맹점 유사도
  - 상태 일치    : 거래 화면이 아닌 이미지가 skipped 로 나왔는지
  - 소요 시간    : 자산별 elapsed_sec

사용 순서
  1) 샘플 캡처를 ./samples/ 에 넣고 추출 실행.
     모든 캡처가 같은 달이면 f03_extraction.py 를 그대로 쓰고,
       $ python f03_extraction.py ./samples --slot 2026-10 --out samples_result.json
     캡처마다 달이 다르면 samples/slots.json 에 {"파일명": "YYYY-MM"} 을 적고 run 을 씁니다
     (슬롯별로 job 을 나눠 돌린 뒤 한 결과 파일로 합칩니다. 실제 서비스도 슬롯 단위 job 입니다).
       $ python eval_f03.py run --samples ./samples --slots ./samples/slots.json --out samples_result.json
  2) 추출 결과로 정답 템플릿 생성 (예측값이 미리 채워져 있으니 틀린 곳만 고치면 됨)
       $ python eval_f03.py template --result samples_result.json --truth ./truth
  3) truth/<파일명>.json 을 열어 금액·날짜·가맹점·방향을 캡처와 대조해 고침
     - 거래 화면이 아닌 이미지는 "expect_skipped": true 로 두고 rows 를 비움
  4) 채점
       $ python eval_f03.py score --result samples_result.json --truth ./truth --report report_sonnet.json
     모델을 바꿔 돌린 결과도 같은 truth 로 채점하면 비교표가 됩니다.
       $ F03_MODEL=claude-opus-5-5 python f03_extraction.py ./samples --slot 2026-10 --out samples_result_opus.json
       $ python eval_f03.py score --result samples_result_opus.json --truth ./truth --report report_opus.json
       $ python eval_f03.py compare report_sonnet.json report_opus.json

정답 파일 형식 (truth/<캡처 파일명>.json)
  {
    "file": "toss_2026-10.png",
    "expect_skipped": false,
    "rows": [
      {"date": "2026-10-17", "time": "12:14", "merchant": "메가커피 강남", "amount_krw": 2000, "direction": "out"}
    ]
  }
  time 은 캡처에 없으면 "" 로 둡니다. merchant 는 대소문자·공백 차이를 무시하고 유사도로 비교합니다.

주의: samples/ 와 truth/ 에는 실제 금융 화면·가맹점명이 들어가므로 저장소에 올리지 않습니다 (.gitignore 참고).
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 1. 기준값 (FunctionalSpec F03 수용 기준)
# ---------------------------------------------------------------------------

TARGET_AMOUNT_ERROR_RATE = 0.02   # 금액 오독률 2% 이하
TARGET_MISSING = 0                # 누락 0건
MERCHANT_MATCH_THRESHOLD = 0.6    # 이 유사도 이상이면 같은 가맹점으로 봄 (금액이 달라도 "같은 거래, 금액 오독"으로 분류)


# ---------------------------------------------------------------------------
# 2. 입출력 자료형
# ---------------------------------------------------------------------------


@dataclass
class Row:
    date: str
    time: str
    merchant: str
    amount_krw: int
    direction: str
    flags: list[str] = field(default_factory=list)
    kind: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Row":
        return cls(
            date=str(d.get("date") or ""),
            time=str(d.get("time") or ""),
            merchant=str(d.get("merchant") or ""),
            amount_krw=int(d.get("amount_krw") or 0),
            direction=str(d.get("direction") or "unknown"),
            flags=list(d.get("flags") or []),
            kind=str(d.get("kind") or ""),
        )

    @property
    def direction_irrelevant(self) -> bool:
        # 본인 계좌 간 이동은 F07 에서 방향과 무관하게 집계 제외라 방향 오독을 세지 않는다.
        # 부호 없이 "A → B" 만 있는 행은 모델이 in/out/unknown 을 오가므로 지표에서 빼야 노이즈가 줄어든다.
        return self.kind == "self_transfer"

    @property
    def date_unknown(self) -> bool:
        """날짜를 알 수 없는 행. 추출기가 date_missing 으로 넘겼거나(예측), 캡처에 날짜가 없어 정답도 빈 값(정답)."""
        return self.date == "" or "date_missing" in self.flags


def _same_date(t: "Row", p: "Row") -> bool:
    """정답이 '모름'이면 예측도 '모름'이어야 맞고, 정답이 있으면 예측이 같아야 합니다.
    (정답은 모르는데 예측이 날짜를 적었다면 지어낸 것이므로 다른 날짜로 취급)"""
    if t.date_unknown:
        return p.date_unknown
    return t.date == p.date


@dataclass
class AssetScore:
    file: str
    status: str
    expect_skipped: bool
    truth_n: int = 0
    pred_n: int = 0
    matched: int = 0            # 같은 거래로 짝지어진 수 (금액 일치 + 금액 오독 포함)
    amount_wrong: int = 0       # 짝은 맞았는데 금액이 다른 수
    date_wrong: int = 0
    date_review: int = 0        # 날짜를 못 읽어 검토로 넘긴 수 (오류 아님, 참고용)
    time_wrong: int = 0
    direction_wrong: int = 0
    missing: int = 0            # 정답에만 있음
    extra: int = 0              # 추출에만 있음
    merchant_sim_sum: float = 0.0
    elapsed_sec: float = 0.0
    status_ok: bool = True
    details: list[str] = field(default_factory=list)   # 사람이 볼 불일치 목록


# ---------------------------------------------------------------------------
# 3. 비교 도우미
# ---------------------------------------------------------------------------


def _norm_merchant(s: str) -> str:
    """가맹점명 비교용 정규화: 소문자, 공백·특수문자 제거."""
    return re.sub(r"[\s\(\)\[\]\-_.,·'\"]+", "", s.lower())


def merchant_similarity(a: str, b: str) -> float:
    na, nb = _norm_merchant(a), _norm_merchant(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:          # "메가커피" vs "메가커피 강남점" 같은 부분 일치
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _pair_rows(truth: list[Row], pred: list[Row]) -> list[tuple[int | None, int | None, str]]:
    """
    정답 행과 예측 행을 짝짓습니다. 반환: (truth_idx, pred_idx, how)
      how = "exact"    날짜+금액+방향 일치 (가장 강한 짝)
            "merchant" 날짜 일치 + 가맹점 유사도 ≥ 임계값 (금액이 다르면 금액 오독)
            "missing"  정답만 있음 / "extra" 예측만 있음
    두 단계 탐욕 매칭. 샘플이 수십 행 수준이라 O(n²)로 충분합니다.
    """
    pairs: list[tuple[int | None, int | None, str]] = []
    used_t: set[int] = set()
    used_p: set[int] = set()

    # 1단계: 날짜+금액+방향 정확 일치. 후보가 여럿이면 가맹점 유사도가 높은 쪽
    for ti, t in enumerate(truth):
        best, best_sim = None, -1.0
        for pi, p in enumerate(pred):
            if pi in used_p:
                continue
            if _same_date(t, p) and t.amount_krw == p.amount_krw and (t.direction == p.direction or t.direction_irrelevant):
                sim = merchant_similarity(t.merchant, p.merchant)
                if sim > best_sim:
                    best, best_sim = pi, sim
        if best is not None:
            pairs.append((ti, best, "exact"))
            used_t.add(ti)
            used_p.add(best)

    # 2단계: 남은 것끼리 날짜 + 가맹점 유사도로 짝짓기 (금액 오독·방향 오독을 잡기 위함)
    for ti, t in enumerate(truth):
        if ti in used_t:
            continue
        best, best_sim = None, MERCHANT_MATCH_THRESHOLD
        for pi, p in enumerate(pred):
            if pi in used_p:
                continue
            if not _same_date(t, p):
                continue
            sim = merchant_similarity(t.merchant, p.merchant)
            if sim >= best_sim:
                best, best_sim = pi, sim
        if best is not None:
            pairs.append((ti, best, "merchant"))
            used_t.add(ti)
            used_p.add(best)

    for ti in range(len(truth)):
        if ti not in used_t:
            pairs.append((ti, None, "missing"))
    for pi in range(len(pred)):
        if pi not in used_p:
            pairs.append((None, pi, "extra"))
    return pairs


def _fmt(r: Row) -> str:
    return f"{r.date} {r.time or '--:--'} {r.merchant} {r.amount_krw:,} {r.direction}"


# ---------------------------------------------------------------------------
# 4. 자산 단위 채점
# ---------------------------------------------------------------------------


def score_asset(asset: dict[str, Any], truth: dict[str, Any] | None) -> AssetScore:
    file = asset.get("file") or asset.get("asset_id", "")
    status = asset.get("status", "")
    expect_skipped = bool(truth.get("expect_skipped", False)) if truth else False
    sc = AssetScore(file=file, status=status, expect_skipped=expect_skipped,
                    elapsed_sec=float(asset.get("elapsed_sec") or 0.0))

    if truth is None:
        sc.status_ok = False
        sc.details.append("정답 파일 없음 (template 로 생성 후 채우세요)")
        return sc

    # 상태 일치: 거래 화면이 아니면 skipped 여야 하고, 거래 화면이면 succeeded 여야 함
    expected_status = "skipped" if expect_skipped else "succeeded"
    sc.status_ok = status == expected_status
    if not sc.status_ok:
        sc.details.append(f"상태 불일치: 기대 {expected_status}, 실제 {status} ({asset.get('error_code') or ''})")

    truth_rows = [Row.from_dict(r) for r in truth.get("rows", [])]
    pred_rows = [Row.from_dict(r) for r in asset.get("rows", [])] if status == "succeeded" else []
    sc.truth_n, sc.pred_n = len(truth_rows), len(pred_rows)

    for ti, pi, how in _pair_rows(truth_rows, pred_rows):
        if how == "missing":
            sc.missing += 1
            sc.details.append(f"누락  : {_fmt(truth_rows[ti])}")
            continue
        if how == "extra":
            sc.extra += 1
            sc.details.append(f"과잉  : {_fmt(pred_rows[pi])}")
            continue
        t, p = truth_rows[ti], pred_rows[pi]
        sc.matched += 1
        sc.merchant_sim_sum += merchant_similarity(t.merchant, p.merchant)
        if t.amount_krw != p.amount_krw:
            sc.amount_wrong += 1
            sc.details.append(f"금액  : 정답 {t.amount_krw:,} / 추출 {p.amount_krw:,}  ({t.merchant})")
        if t.date_unknown and p.date_unknown:
            sc.date_review += 1          # 둘 다 "모름": 정상 (검토 큐로 넘어감)
        elif t.date != p.date:
            sc.date_wrong += 1
            sc.details.append(f"날짜  : 정답 {t.date} / 추출 {p.date}  ({t.merchant})")
        if (t.time or "") != (p.time or ""):
            sc.time_wrong += 1
            sc.details.append(f"시각  : 정답 {t.time or '-'} / 추출 {p.time or '-'}  ({t.merchant})")
        if t.direction != p.direction and not t.direction_irrelevant:
            sc.direction_wrong += 1
            sc.details.append(f"방향  : 정답 {t.direction} / 추출 {p.direction}  ({t.merchant})")
    return sc


# ---------------------------------------------------------------------------
# 5. 전체 집계와 출력
# ---------------------------------------------------------------------------


def _rate(num: int, den: int) -> float | None:
    return None if den == 0 else num / den


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def build_report(result: dict[str, Any], truth_dir: Path) -> dict[str, Any]:
    scores: list[AssetScore] = []
    for asset in result.get("assets", []):
        file = asset.get("file") or asset.get("asset_id", "")
        tpath = truth_dir / f"{Path(file).stem}.json"
        truth = json.loads(tpath.read_text(encoding="utf-8")) if tpath.exists() else None
        scores.append(score_asset(asset, truth))

    tot = Counter()
    sim_sum = 0.0
    for s in scores:
        for k in ("truth_n", "pred_n", "matched", "amount_wrong", "date_wrong", "date_review", "time_wrong",
                  "direction_wrong", "missing", "extra"):
            tot[k] += getattr(s, k)
        sim_sum += s.merchant_sim_sum
    status_ok = sum(1 for s in scores if s.status_ok)

    amount_err = _rate(tot["amount_wrong"], tot["matched"])
    summary = {
        "model": result.get("model"),
        "extractor_version": result.get("extractor_version"),
        "prompt_version": result.get("prompt_version"),
        "effort": result.get("effort"),
        "assets": len(scores),
        "status_ok": status_ok,
        "truth_rows": tot["truth_n"],
        "pred_rows": tot["pred_n"],
        "matched": tot["matched"],
        "amount_error_rate": amount_err,
        "missing": tot["missing"],
        "extra": tot["extra"],
        "date_match_rate": None if tot["matched"] == 0 else 1 - tot["date_wrong"] / tot["matched"],
        "date_review": tot["date_review"],
        "time_match_rate": None if tot["matched"] == 0 else 1 - tot["time_wrong"] / tot["matched"],
        "direction_match_rate": None if tot["matched"] == 0 else 1 - tot["direction_wrong"] / tot["matched"],
        "merchant_similarity_avg": None if tot["matched"] == 0 else sim_sum / tot["matched"],
        "elapsed_max_sec": max((s.elapsed_sec for s in scores), default=0.0),
        "elapsed_sum_sec": round(sum(s.elapsed_sec for s in scores), 2),
        "pass": {
            "amount_error_rate": amount_err is not None and amount_err <= TARGET_AMOUNT_ERROR_RATE,
            "missing": tot["missing"] <= TARGET_MISSING,
            "status": status_ok == len(scores),
        },
    }
    return {"summary": summary, "assets": [s.__dict__ for s in scores]}


def print_report(rep: dict[str, Any]) -> None:
    s = rep["summary"]
    print(f"\n모델 {s['model']}  extractor {s['extractor_version']}  prompt {s['prompt_version']}  effort {s['effort']}")
    print("-" * 78)
    print(f"{'파일':28} {'상태':9} {'정답':>4} {'추출':>4} {'짝':>4} {'금액X':>5} {'누락':>4} {'과잉':>4} {'초':>6}")
    for a in rep["assets"]:
        mark = "" if a["status_ok"] else " !"
        print(f"{a['file'][-28:]:28} {a['status']:9} {a['truth_n']:>4} {a['pred_n']:>4} {a['matched']:>4} "
              f"{a['amount_wrong']:>5} {a['missing']:>4} {a['extra']:>4} {a['elapsed_sec']:>6.1f}{mark}")
    print("-" * 78)
    p = s["pass"]
    ok = lambda b: "통과" if b else "실패"
    print(f"금액 오독률      {_pct(s['amount_error_rate']):>7}  (목표 ≤ {TARGET_AMOUNT_ERROR_RATE*100:.0f}%)   {ok(p['amount_error_rate'])}")
    print(f"누락             {s['missing']:>7}  (목표 0)      {ok(p['missing'])}")
    print(f"과잉             {s['extra']:>7}")
    print(f"상태 일치        {s['status_ok']:>4}/{s['assets']:<3}              {ok(p['status'])}")
    print(f"날짜 일치율      {_pct(s['date_match_rate']):>7}  (날짜 검토 넘김 {s['date_review']}건은 제외)")
    print(f"시각 일치율      {_pct(s['time_match_rate']):>7}")
    print(f"방향 일치율      {_pct(s['direction_match_rate']):>7}")
    print(f"가맹점 유사도    {_pct(s['merchant_similarity_avg']):>7}")
    print(f"소요 시간        최대 {s['elapsed_max_sec']:.1f}초 / 합계 {s['elapsed_sum_sec']:.1f}초")

    details = [(a["file"], d) for a in rep["assets"] for d in a["details"]]
    if details:
        print("\n불일치 목록")
        for f, d in details:
            print(f"  [{f}] {d}")


# ---------------------------------------------------------------------------
# 6. 서브커맨드
# ---------------------------------------------------------------------------


def cmd_template(args: argparse.Namespace) -> None:
    """추출 결과를 정답 템플릿으로 변환. 사람이 틀린 값만 고치면 되도록 예측값을 채워 둡니다."""
    result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    out = Path(args.truth)
    out.mkdir(parents=True, exist_ok=True)
    made = 0
    for asset in result.get("assets", []):
        file = asset.get("file") or asset.get("asset_id", "")
        tpath = out / f"{Path(file).stem}.json"
        if tpath.exists() and not args.force:
            print(f"skip (이미 있음) {tpath}")
            continue
        truth = {
            "file": file,
            "expect_skipped": asset.get("status") == "skipped",
            "_note": "예측값이 채워져 있습니다. 캡처와 대조해 틀린 곳을 고치고, 빠진 행은 추가, 없는 행은 삭제하세요.",
            "rows": [
                # 추출기가 날짜를 못 읽은 행은 정답 템플릿에도 "" 로 둡니다. 캡처에 날짜가 정말 없으면 그대로 두고,
                # 사람이 캡처에서 날짜를 읽을 수 있으면 채워 넣으세요 (그러면 추출기가 놓친 것으로 채점됩니다).
                {"date": "" if "date_missing" in (r.get("flags") or []) else r.get("date", ""),
                 "time": r.get("time") or "", "merchant": r.get("merchant", ""),
                 "amount_krw": r.get("amount_krw", 0), "direction": r.get("direction", "out")}
                for r in asset.get("rows", [])
            ],
        }
        tpath.write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
        made += 1
        print(f"wrote {tpath}  ({len(truth['rows'])} rows)")
    print(f"\n{made}개 템플릿 생성. 파일을 열어 캡처와 대조해 고친 뒤 score 를 실행하세요.")


def cmd_run(args: argparse.Namespace) -> None:
    """slots.json 기준으로 슬롯별 job 을 돌리고 결과를 한 파일로 합칩니다 (평가 전용 편의 기능)."""
    import asyncio
    import f03_extraction as f03

    samples = Path(args.samples)
    slots: dict[str, str] = json.loads(Path(args.slots).read_text(encoding="utf-8"))
    by_slot: dict[str, list[tuple[str, Path]]] = {}
    for name, slot in slots.items():
        path = samples / name
        if not path.exists():
            print(f"경고: {path} 없음, 건너뜀")
            continue
        by_slot.setdefault(slot, []).append((Path(name).stem, path))   # asset_id = 파일명 stem
    if not by_slot:
        raise SystemExit("slots.json 에 유효한 항목이 없습니다")

    async def run_all():
        jobs = []
        for slot, assets in sorted(by_slot.items()):
            print(f"slot {slot}: {len(assets)}장")
            jobs.append(await f03.run_extraction_job(assets, slot))
        return jobs

    jobs = asyncio.run(run_all())
    merged: dict[str, Any] | None = None
    for job, (slot, assets) in zip(jobs, sorted(by_slot.items())):
        payload = f03.job_to_payload(job, {aid: p.name for aid, p in assets})
        for a in payload["assets"]:
            a["slot_month"] = slot
        if merged is None:
            merged = {**payload, "slot_month": "mixed", "job_id": "eval", "status": "mixed", "assets": []}
        merged["assets"].extend(payload["assets"])

    for a in merged["assets"]:
        print(f"  {a['file'][:36]:36} {a['slot_month']} {a['status']:9} rows={len(a['rows']):3} "
              f"app={a.get('app_guess') or '-'} month={a.get('displayed_month') or '-'} {a['elapsed_sec']}s")
    Path(args.out).write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved → {args.out}")


def cmd_score(args: argparse.Namespace) -> None:
    result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    rep = build_report(result, Path(args.truth))
    print_report(rep)
    if args.report:
        Path(args.report).write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nsaved → {args.report}")
    p = rep["summary"]["pass"]
    sys.exit(0 if all(p.values()) else 1)


def cmd_compare(args: argparse.Namespace) -> None:
    """여러 report JSON 을 한 표로. 모델·프롬프트 버전 비교용."""
    reps = [json.loads(Path(p).read_text(encoding="utf-8"))["summary"] for p in args.reports]
    keys = [("model", "모델"), ("prompt_version", "프롬프트"), ("effort", "effort"),
            ("amount_error_rate", "금액 오독률"), ("missing", "누락"), ("extra", "과잉"),
            ("date_match_rate", "날짜"), ("time_match_rate", "시각"), ("direction_match_rate", "방향"),
            ("merchant_similarity_avg", "가맹점"), ("elapsed_max_sec", "최대 초"), ("elapsed_sum_sec", "합계 초")]
    rate_keys = {"amount_error_rate", "date_match_rate", "time_match_rate", "direction_match_rate", "merchant_similarity_avg"}
    print(f"{'항목':12}" + "".join(f"{Path(p).stem[:18]:>20}" for p in args.reports))
    for k, label in keys:
        vals = []
        for r in reps:
            v = r.get(k)
            vals.append(_pct(v) if k in rate_keys else ("-" if v is None else str(v)))
        print(f"{label:12}" + "".join(f"{v:>20}" for v in vals))


def main() -> None:
    ap = argparse.ArgumentParser(description="F03 추출 채점기")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="slots.json 기준으로 슬롯별 추출 후 결과 병합")
    r.add_argument("--samples", default="./samples")
    r.add_argument("--slots", default="./samples/slots.json")
    r.add_argument("--out", default="samples_result.json")
    r.set_defaults(fn=cmd_run)

    t = sub.add_parser("template", help="추출 결과에서 정답 템플릿 생성")
    t.add_argument("--result", required=True)
    t.add_argument("--truth", default="./truth")
    t.add_argument("--force", action="store_true", help="이미 있는 정답 파일도 덮어씀")
    t.set_defaults(fn=cmd_template)

    s = sub.add_parser("score", help="정답과 대조해 채점")
    s.add_argument("--result", required=True)
    s.add_argument("--truth", default="./truth")
    s.add_argument("--report", help="채점 결과 JSON 저장 경로 (compare 입력)")
    s.set_defaults(fn=cmd_score)

    c = sub.add_parser("compare", help="report JSON 여러 개를 표로 비교")
    c.add_argument("reports", nargs="+")
    c.set_defaults(fn=cmd_compare)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
