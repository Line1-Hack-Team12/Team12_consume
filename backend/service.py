"""
F03 추출기 HTTP 서비스. Spring 백엔드가 호출한다.

  실행:  uvicorn service:app --host 0.0.0.0 --port 8001
  문서:  http://localhost:8001/docs

엔드포인트
  GET  /health                     살아 있는지 + 모델·버전
  POST /extract                    캡처 N장 → job JSON (F03_INTERFACE.md 의 형식)
  POST /extract/retry              실패한 장만 다시 추출하고 원래 job JSON 에 병합해서 돌려줌
  POST /parse                      은행 앱 거래내역 파일(토스뱅크 xlsx) → 같은 모양의 job JSON (달마다 asset 하나)
  POST /classify                   가맹점명[] → 카테고리·하위 태그 (F07)
  POST /narrate                    패턴 수치 → 카드 문장·요약·리포트 (F11·F15·F16). 숫자·증거 id 검증 포함
  POST /notify-copy                패턴 수치 + 원인 태그 → 알림 제목·본문 (F14). 고정 안내 문장은 서비스가 붙임

설계
  - 동기 응답. 한 장 5~6초, 5장 동시 처리라 10장이면 15초 안팎. Spring 쪽 HTTP 타임아웃은 120초 이상으로.
  - 업로드 이미지는 요청 처리 동안만 임시 디렉터리에 두고 응답 후 지운다. 이 서비스는 아무것도 저장하지 않는다.
  - ANTHROPIC_API_KEY 는 이 프로세스의 .env 에만 있다. 요청에 키를 넣지 않는다.
  - 로그에 이미지·원문·이름을 남기지 않는다 (f03_extraction 의 로그 정책과 같음).
"""
from __future__ import annotations

import asyncio
import json
import logging

import anthropic
import re
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse

import f03_extraction as f03
from bank_file_parser import EncryptedFileError, UnsupportedFileError, parse_bank_file, parsed_to_payload
import classify as f07
import narrate as f11
import notify as f14

log = logging.getLogger("f03.service")

app = FastAPI(title="F03 추출 서비스", version=f03.EXTRACTOR_VERSION)

MAX_IMAGES_PER_REQUEST = 20
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_FILE_BYTES = 20 * 1024 * 1024
_RE_MONTHS = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_RE_SLOT = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "model": f03.MODEL, "extractor_version": f03.EXTRACTOR_VERSION,
            "prompt_version": f03.PROMPT_VERSION}


def _validate_common(slot_month: str, images: list[UploadFile], asset_ids: list[str]) -> None:
    if not _RE_SLOT.match(slot_month):
        raise HTTPException(400, "slot_month 는 YYYY-MM 형식이어야 합니다")
    if not images:
        raise HTTPException(400, "images 가 비어 있습니다")
    if len(images) > MAX_IMAGES_PER_REQUEST:
        raise HTTPException(400, f"한 번에 최대 {MAX_IMAGES_PER_REQUEST}장")
    if len(asset_ids) != len(images):
        raise HTTPException(400, "asset_ids 개수가 images 개수와 다릅니다")
    if len(set(asset_ids)) != len(asset_ids):
        raise HTTPException(400, "asset_ids 에 중복이 있습니다")
    if any(not a.strip() for a in asset_ids):
        raise HTTPException(400, "asset_ids 에 빈 값이 있습니다")


async def _save_uploads(tmp: Path, images: list[UploadFile], asset_ids: list[str]) -> list[tuple[str, Path]]:
    """업로드 파일을 임시 디렉터리에 저장. 파일명은 asset_id 기준으로 새로 만든다 (원본 파일명은 쓰지 않음)."""
    assets: list[tuple[str, Path]] = []
    for aid, up in zip(asset_ids, images):
        suffix = Path(up.filename or "").suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(415, f"지원하지 않는 형식: asset_id={aid}. jpg/png/webp 만 받습니다")
        data = await up.read()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(413, f"이미지가 너무 큽니다: asset_id={aid}")
        path = tmp / f"{len(assets):03d}{suffix}"
        path.write_bytes(data)
        assets.append((aid, path))
    return assets


@app.post("/extract")
async def extract(
    slot_month: str = Form(...),
    images: list[UploadFile] = File(...),
    asset_ids: list[str] = Form(...),
    owner_name: str = Form(""),
) -> JSONResponse:
    """캡처 N장을 추출해 job JSON 을 돌려준다. 응답 형식은 F03_INTERFACE.md."""
    _validate_common(slot_month, images, asset_ids)
    with tempfile.TemporaryDirectory(prefix="f03_") as d:
        assets = await _save_uploads(Path(d), images, asset_ids)
        job = await f03.run_extraction_job(assets, slot_month, owner_name=owner_name.strip())
    payload = f03.job_to_payload(job)
    log.info("extract slot=%s n=%d status=%s rows=%d", slot_month, len(assets), job.status, len(job.all_rows))
    return JSONResponse(payload)


@app.post("/extract/retry")
async def extract_retry(
    original_job: str = Form(..., description="이전 /extract 응답 JSON 문자열"),
    images: list[UploadFile] = File(...),
    asset_ids: list[str] = Form(...),
    owner_name: str = Form(""),
) -> JSONResponse:
    """실패한 장만 다시 추출해 원래 job 에 병합한다.

    - asset_ids 는 원래 job 에서 status=failed 이고 재시도 가능한 것만 (retryable_assets). 성공한 장을 보내면 400.
    - 응답은 병합된 job JSON (job_id 는 원래 것 유지). 또 실패한 장은 그대로 failed 로 남고 status 는 partial.
    """
    try:
        payload_in = json.loads(original_job)
        if not isinstance(payload_in, dict):
            raise TypeError("not an object")
        original = f03.job_from_payload(payload_in)
        if not isinstance(original.slot_month, str) or not isinstance(original.assets, list):
            raise TypeError("bad shape")
    except (ValueError, KeyError, TypeError, AttributeError) as e:       # JSON 아님, 배열, 필드 누락, 행이 dict 아님 등
        raise HTTPException(400, f"original_job 을 읽을 수 없습니다: {type(e).__name__}")
    _validate_common(original.slot_month, images, asset_ids)

    allowed = set(f03.retryable_assets(original))
    bad = [a for a in asset_ids if a not in allowed]
    if bad:
        raise HTTPException(400, f"재시도 대상이 아닌 asset_ids: {bad}. 재시도 가능: {sorted(allowed)}, "
                                 f"교체 필요: {f03.replace_needed_assets(original)}")

    with tempfile.TemporaryDirectory(prefix="f03_") as d:
        assets = await _save_uploads(Path(d), images, asset_ids)
        retry = await f03.run_extraction_job(assets, original.slot_month, owner_name=owner_name.strip())
    try:
        merged = f03.merge_retry(original, retry)
    except ValueError as e:
        raise HTTPException(409, str(e))
    payload = f03.job_to_payload(merged)
    payload["retry"] = {"retried": asset_ids,
                        "still_retryable": f03.retryable_assets(merged),
                        "replace_needed": f03.replace_needed_assets(merged),
                        "max_retry_rounds": f03.MAX_RETRY_ROUNDS}
    log.info("retry slot=%s n=%d status=%s", original.slot_month, len(assets), merged.status)
    return JSONResponse(payload)


@app.post("/parse")
async def parse_file(
    file: UploadFile = File(..., description="은행 앱이 내보낸 거래내역 파일 (지금은 토스뱅크 xlsx)"),
    asset_id: str = Form(..., description="백엔드의 UploadedFile id"),
    owner_name: str = Form(""),
    password: str = Form("", description="파일 열기 암호 (토스뱅크 발급 파일은 암호가 걸려 있음). 저장하지 않음"),
    months: list[str] = Form([], description="돌려줄 달 (YYYY-MM, 반복). 비우면 파일의 모든 달"),
) -> JSONResponse:
    """거래내역 파일을 읽어 F03 과 같은 모양의 JSON 을 돌려준다.

    - 잔액·상대 계좌번호·메모·성명 열은 읽지 않는다. 파일은 메모리에서만 처리하고 디스크에 쓰지 않는다.
    - 응답 assets 는 달마다 하나 (asset_id = "<asset_id>:<YYYY-MM>", slot_month 포함). 각 행은 F03 row 와 같은 필드.
    - 파일 파싱은 결정적이라 confidence 는 전부 high. 검토 큐에 올릴 플래그는 생기지 않는다 (merchant_missing 제외).
    """
    if not asset_id.strip():
        raise HTTPException(400, "asset_id 가 비어 있습니다")
    bad = [m for m in months if not _RE_MONTHS.match(m)]
    if bad:
        raise HTTPException(400, f"months 형식 오류(YYYY-MM): {bad}")
    data = await file.read()
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "파일이 너무 큽니다")
    try:
        # 암호 해제·xlsx 읽기는 동기 CPU 작업이라 스레드로 보낸다 (이벤트 루프를 잡으면 그동안 /health 도 멈춘다)
        parsed = await asyncio.to_thread(parse_bank_file, data, file.filename or "", asset_id.strip(), owner_name.strip(), password or None)
    except EncryptedFileError as e:
        raise HTTPException(422, str(e))
    except UnsupportedFileError as e:
        raise HTTPException(415, str(e))
    finally:
        del data
    payload = parsed_to_payload(parsed, asset_id.strip(), months or None)
    log.info("parse format=%s rows=%d months=%d skipped=%d", parsed.source_format, len(parsed.rows),
             len(payload["assets"]), parsed.skipped_rows)
    return JSONResponse(payload)


class ClassifyRequest(BaseModel):
    items: list[f07.ClassifyItem] = Field(..., max_length=1000)


@app.post("/classify")
async def classify_merchants(req: ClassifyRequest) -> JSONResponse:
    """가맹점명을 카테고리로 분류한다 (F03_INTERFACE.md 6절). 응답 순서·개수는 요청과 같다.

    - 100개씩 나눠 동시 호출. 모델 오류·거부·스키마 실패는 해당 묶음을 other/미분류/low 로 돌려주고 500 을 내지 않는다.
    - 허용값 밖 출력은 서비스가 교정하므로 백엔드는 enum 검증이 필요 없다.
    """
    if not req.items:
        return JSONResponse(f07.to_payload([]))
    results = await f07.classify(req.items)
    log.info("classify n=%d low=%d", len(results), sum(c.confidence == "low" for c in results))
    return JSONResponse(f07.to_payload(results))


@app.post("/narrate")
async def narrate_patterns(req: f11.NarrateRequest) -> JSONResponse:
    """패턴 수치로 카드 문장·가설 질문·행동 제안·태그 추천·요약·리포트를 만든다 (F03_INTERFACE.md 7절).

    - 문장 속 숫자는 요청 숫자 집합과 대조하고, evidence_transaction_ids 는 그 패턴의 증거 id 부분집합인지 확인한다.
      어긋나면 1회 교정 재시도, 그래도 틀리면 422 {"error_code": "ANALYSIS_OUTPUT_INVALID", "errors": [...]}.
    - 모델이 말투를 거부하면 중립 말투로 다시 만들고 응답 tone 이 "neutral" 이 된다.
    - 모델 호출 자체가 실패하면 502 ANALYSIS_FAILED. 백엔드는 (snapshot_id, tone) 으로 결과를 캐시한다.
    """
    try:
        out = await f11.narrate(req)
    except f11.NarrateInvalid as e:
        log.warning("narrate invalid snapshot=%s errors=%d", req.snapshot_id, len(e.errors))
        raise HTTPException(422, {"error_code": "ANALYSIS_OUTPUT_INVALID", "errors": e.errors})
    except anthropic.APIError as e:
        log.warning("narrate api error type=%s", type(e).__name__)
        raise HTTPException(502, {"error_code": "ANALYSIS_FAILED", "errors": [type(e).__name__]})
    log.info("narrate snapshot=%s tone=%s cards=%d", req.snapshot_id, out["tone"], len(out["cards"]))
    return JSONResponse(out)


@app.post("/notify-copy")
async def notify_copy(req: f14.NotifyRequest) -> JSONResponse:
    """알림 미리보기 문구 (F03_INTERFACE.md 8절). title ≤ 20자, body ≤ 80자, notice 는 항상 같은 문장.

    - 숫자는 pattern 수치만. 길이 초과·숫자 불일치는 1회 교정, 그래도 틀리면 422 ANALYSIS_OUTPUT_INVALID.
    - cause_tags 가 비어 있거나 not_applicable 이면 태그 맥락 없이 수치만으로 쓴다.
    """
    try:
        out = await f14.notify_copy(req)
    except f14.NotifyInvalid as e:
        raise HTTPException(422, {"error_code": "ANALYSIS_OUTPUT_INVALID", "errors": e.errors})
    except anthropic.APIError as e:
        log.warning("notify api error type=%s", type(e).__name__)
        raise HTTPException(502, {"error_code": "ANALYSIS_FAILED", "errors": [type(e).__name__]})
    log.info("notify pattern=%s tone=%s", req.pattern.pattern_id, out["tone"])
    return JSONResponse(out)
