"""
F03 추출기 HTTP 서비스. Spring 백엔드가 호출한다.

  실행:  uvicorn service:app --host 0.0.0.0 --port 8001
  문서:  http://localhost:8001/docs

엔드포인트
  GET  /health                     살아 있는지 + 모델·버전
  POST /extract                    캡처 N장 → job JSON (F03_INTERFACE.md 의 형식)
  POST /extract/retry              실패한 장만 다시 추출하고 원래 job JSON 에 병합해서 돌려줌

설계
  - 동기 응답. 한 장 5~6초, 5장 동시 처리라 10장이면 15초 안팎. Spring 쪽 HTTP 타임아웃은 120초 이상으로.
  - 업로드 이미지는 요청 처리 동안만 임시 디렉터리에 두고 응답 후 지운다. 이 서비스는 아무것도 저장하지 않는다.
  - ANTHROPIC_API_KEY 는 이 프로세스의 .env 에만 있다. 요청에 키를 넣지 않는다.
  - 로그에 이미지·원문·이름을 남기지 않는다 (f03_extraction 의 로그 정책과 같음).
"""
from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

import f03_extraction as f03

log = logging.getLogger("f03.service")

app = FastAPI(title="F03 추출 서비스", version=f03.EXTRACTOR_VERSION)

MAX_IMAGES_PER_REQUEST = 20
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
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
        original = f03.job_from_payload(json.loads(original_job))
    except (ValueError, KeyError, TypeError) as e:
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
