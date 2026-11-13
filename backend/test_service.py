# -*- coding: utf-8 -*-
"""service.py 테스트. 모델 호출은 가짜로 바꿔 API 키 없이 돈다."""
import io
import json

import pytest
from fastapi.testclient import TestClient

import f03_extraction as f03
import service


def _asset(aid, status, code=None, nrows=0):
    rows = [f03.TransactionCandidate(id=f"tx_{aid}_{i}", asset_id=aid, source_row_id=f"{aid}:row{i:03d}",
                                     date="2026-10-01", time=None, merchant="m", amount_krw=100, direction="out",
                                     kind="expense", confidence="high", raw_text="r") for i in range(nrows)]
    return f03.AssetExtraction(asset_id=aid, status=status, rows=rows, error_code=code)


@pytest.fixture
def client(monkeypatch):
    calls = []

    async def fake_job(assets, slot_month, client=None, concurrency=5, owner_name=None):
        calls.append({"ids": [a for a, _ in assets], "slot": slot_month, "owner": owner_name,
                      "exists": [p.exists() for _, p in assets]})
        out = []
        for aid, _ in assets:
            if aid.startswith("fail"):
                out.append(_asset(aid, "failed", "RATE_LIMITED"))
            elif aid.startswith("skip"):
                out.append(_asset(aid, "skipped", "NOT_TRANSACTION_SCREEN"))
            else:
                out.append(_asset(aid, "succeeded", nrows=2))
        return f03.ExtractionJobResult(job_id="job_test", slot_month=slot_month,
                                       status=f03.job_status(out), assets=out)

    monkeypatch.setattr(f03, "run_extraction_job", fake_job)
    c = TestClient(service.app)
    c.calls = calls
    return c


def _png(name="a.png"):
    return (name, io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64), "image/png")


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["extractor_version"] == f03.EXTRACTOR_VERSION


def test_extract_happy_path_and_owner_name_passthrough(client):
    r = client.post("/extract",
                    data={"slot_month": "2026-10", "asset_ids": ["17", "18"], "owner_name": " 홍길동 "},
                    files=[("images", _png("x.png")), ("images", _png("y.jpg"))])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded" and [a["asset_id"] for a in body["assets"]] == ["17", "18"]
    assert len(body["assets"][0]["rows"]) == 2 and body["extractor_version"] == f03.EXTRACTOR_VERSION
    call = client.calls[0]
    assert call["owner"] == "홍길동" and call["slot"] == "2026-10" and all(call["exists"])
    assert "홍길동" not in r.text                                       # 응답에 이름 없음


def test_extract_validation_errors(client):
    bad_slot = client.post("/extract", data={"slot_month": "2026-13", "asset_ids": ["1"]}, files=[("images", _png())])
    assert bad_slot.status_code == 400
    mismatch = client.post("/extract", data={"slot_month": "2026-10", "asset_ids": ["1", "2"]}, files=[("images", _png())])
    assert mismatch.status_code == 400
    dup = client.post("/extract", data={"slot_month": "2026-10", "asset_ids": ["1", "1"]},
                      files=[("images", _png()), ("images", _png())])
    assert dup.status_code == 400
    wrong_type = client.post("/extract", data={"slot_month": "2026-10", "asset_ids": ["1"]},
                             files=[("images", ("a.gif", io.BytesIO(b"GIF89a"), "image/gif"))])
    assert wrong_type.status_code == 415
    assert client.calls == []                                           # 검증 실패 시 모델 호출 없음


def test_extract_partial_then_retry_merges(client):
    first = client.post("/extract", data={"slot_month": "2026-10", "asset_ids": ["ok1", "fail1", "skip1"]},
                        files=[("images", _png()), ("images", _png()), ("images", _png())]).json()
    assert first["status"] == "partial"
    by = {a["asset_id"]: a for a in first["assets"]}
    assert by["fail1"]["error_code"] == "RATE_LIMITED" and by["skip1"]["status"] == "skipped"

    # 성공한 장을 재시도하려 하면 거부
    r = client.post("/extract/retry", data={"original_job": json.dumps(first), "asset_ids": ["ok1"]},
                    files=[("images", _png())])
    assert r.status_code == 400 and "ok1" in r.text

    # 실패한 장만 재시도 → 병합. 이번엔 성공하도록 id 를 바꿔 보내는 대신 가짜 추출기가 'fail' 접두사로 실패를 내므로
    # 여전히 실패하는 경우를 먼저 확인
    r = client.post("/extract/retry", data={"original_job": json.dumps(first), "asset_ids": ["fail1"]},
                    files=[("images", _png())])
    assert r.status_code == 200, r.text
    merged = r.json()
    assert merged["job_id"] == "job_test" and merged["status"] == "partial"
    assert merged["retry"]["still_retryable"] == ["fail1"] and merged["retry"]["max_retry_rounds"] == 2
    assert sum(len(a["rows"]) for a in merged["assets"]) == 2            # 성공 자산 행 수 불변


def test_retry_rejects_broken_original(client):
    r = client.post("/extract/retry", data={"original_job": "{not json", "asset_ids": ["fail1"]},
                    files=[("images", _png())])
    assert r.status_code == 400


def test_job_payload_round_trip():
    job = f03.ExtractionJobResult("job_x", "2026-10", "partial",
                                  [_asset("a", "succeeded", nrows=3), _asset("b", "failed", "NETWORK_ERROR")])
    back = f03.job_from_payload(json.loads(json.dumps(f03.job_to_payload(job))))
    assert back.job_id == "job_x" and back.slot_month == "2026-10" and back.status == "partial"
    assert [a.asset_id for a in back.assets] == ["a", "b"] and len(back.all_rows) == 3
    assert back.assets[1].error_code == "NETWORK_ERROR"
    assert f03.retryable_assets(back) == ["b"]



# ---- /parse ----

from test_bank_file_parser import _toss_xlsx, ROWS


def test_parse_endpoint_returns_month_assets_without_sensitive_values():
    c = TestClient(service.app)
    r = c.post("/parse", data={"asset_id": "42", "owner_name": "홍길동", "months": ["2026-10"]},
               files=[("file", ("토스뱅크_거래내역.xlsx", io.BytesIO(_toss_xlsx(ROWS)),
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded" and body["source"] == "file"
    assert [(a["asset_id"], a["slot_month"], len(a["rows"])) for a in body["assets"]] == [("42:2026-10", "2026-10", 9)]
    for forbidden in ["홍길동", "김철수", "1234567", "110-123", "생활비"]:
        assert forbidden not in r.text, forbidden
    row = body["assets"][0]["rows"][0]
    assert set(row) >= {"id", "date", "time", "merchant", "amount_krw", "direction", "kind", "confidence", "raw_text", "flags"}


def test_parse_endpoint_errors():
    c = TestClient(service.app)
    ole_magic = bytes.fromhex("d0cf11e0a1b11ae1") + bytes(64)
    enc = c.post("/parse", data={"asset_id": "1"},
                 files=[("file", ("a.xlsx", io.BytesIO(ole_magic), "application/octet-stream"))])
    assert enc.status_code == 422                                  # 암호 필요
    pdf = c.post("/parse", data={"asset_id": "1"}, files=[("file", ("a.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf"))])
    assert pdf.status_code == 415
    bad_month = c.post("/parse", data={"asset_id": "1", "months": ["2026-13"]},
                       files=[("file", ("a.xlsx", io.BytesIO(_toss_xlsx(ROWS)), "application/octet-stream"))])
    assert bad_month.status_code == 400



# ---- /classify ----

def test_classify_endpoint_keeps_order(monkeypatch):
    import classify as f07

    async def fake_classify(items, client=None, concurrency=3):
        return [f07.Classification(it.merchant, "living", "편의점", "high") for it in items]

    monkeypatch.setattr(f07, "classify", fake_classify)
    c = TestClient(service.app)
    r = c.post("/classify", json={"items": [{"merchant": "GS25 역삼점", "kind": "expense"}, {"merchant": "CU", "kind": "expense", "raw_text": "x"}]})
    assert r.status_code == 200 and [i["merchant"] for i in r.json()["items"]] == ["GS25 역삼점", "CU"]
    assert r.json()["items"][0]["subcategory"] == "편의점"
    assert c.post("/classify", json={"items": []}).json()["items"] == []
    assert c.post("/classify", json={"items": [{"kind": "expense"}]}).status_code == 422


# ---- /narrate ----

def test_narrate_endpoint_maps_errors(monkeypatch):
    import narrate as f11
    from test_narrate import REQ, GOOD

    async def ok(req, client=None):
        return {**GOOD, "model": "m", "prompt_version": "n1", "tone": req.tone, "snapshot_id": req.snapshot_id}

    monkeypatch.setattr(f11, "narrate", ok)
    c = TestClient(service.app)
    r = c.post("/narrate", json=REQ)
    assert r.status_code == 200 and r.json()["tone"] == "coach" and r.json()["cards"][0]["pattern_id"] == "pat_3"

    async def invalid(req, client=None):
        raise f11.NarrateInvalid(["cards[pat_3].action: 입력에 없는 숫자 50,000"])

    monkeypatch.setattr(f11, "narrate", invalid)
    r = c.post("/narrate", json=REQ)
    assert r.status_code == 422 and r.json()["detail"]["error_code"] == "ANALYSIS_OUTPUT_INVALID"
    assert "50,000" in r.json()["detail"]["errors"][0]

    assert c.post("/narrate", json={**REQ, "tone": "pirate"}).status_code == 422
    assert c.post("/narrate", json={**REQ, "want": []}).status_code == 422


# ---- /notify-copy ----

def test_notify_copy_endpoint(monkeypatch):
    import notify as f14
    from test_notify import REQ, GOOD

    async def ok(req, client=None):
        return {**GOOD, "notice": f14.NOTICE, "pattern_id": req.pattern.pattern_id, "tone": req.tone, "model": "m", "prompt_version": "a1"}

    monkeypatch.setattr(f14, "notify_copy", ok)
    c = TestClient(service.app)
    r = c.post("/notify-copy", json=REQ)
    assert r.status_code == 200 and r.json()["notice"] == f14.NOTICE and r.json()["title"] == "목요일 밤이다"

    async def invalid(req, client=None):
        raise f14.NotifyInvalid(["body: 80자 초과 (지금 91자)"])

    monkeypatch.setattr(f14, "notify_copy", invalid)
    assert c.post("/notify-copy", json=REQ).status_code == 422
    assert c.post("/notify-copy", json={**REQ, "cause_tags": ["bored"]}).status_code == 422


# ---- cold review 반영: 잘못된 original_job 모양은 400 ----

def test_retry_rejects_wrong_shaped_original(client):
    for bad in ["[]", "123", '{"job_id": "j", "slot_month": null, "status": "partial", "assets": []}',
                '{"job_id": "j", "slot_month": "2026-10", "status": "partial", "assets": [{"asset_id": "a", "rows": [1]}]}']:
        r = client.post("/extract/retry", data={"original_job": bad, "asset_ids": ["x"]},
                        files=[("images", ("x.png", b"\x89PNG", "image/png"))])
        assert r.status_code == 400, (bad, r.status_code, r.text)
