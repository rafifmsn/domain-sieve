"""Tests for out-of-band DeepSeek Uncensored enricher."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import httpx

from sieve.enrich import enrich_candidates_csv


def test_enrich_candidates_csv_truncates_and_logs(tmp_path: Path) -> None:
    scraped_root = tmp_path / "scraped"
    art_path = scraped_root / "2026-11-26/judol/aa/aabb.json"
    art_path.parent.mkdir(parents=True, exist_ok=True)
    art_path.write_text(
        json.dumps({"title": "Slot88 Gacor", "markdown": "Deposit QRIS 10rb"}),
        encoding="utf-8",
    )

    cand_csv = tmp_path / "candidates.csv"
    with cand_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "canonical_key",
                "submit_url",
                "category",
                "is_id_targeted",
                "confidence",
                "classifier_id",
                "source",
                "export_batch",
                "artifact_path",
            ]
        )
        w.writerow(
            [
                "slot88.xyz",
                "https://slot88.xyz",
                "judol",
                "1",
                "0.9500",
                "bge-m3-zeroshot-v2.0",
                "certstream",
                "2026-11-26/batch_0001.txt",
                "2026-11-26/judol/aa/aabb.json",
            ]
        )

    long_note = "Situs judi online slot88 menawarkan permainan slot gacor dengan pembayaran QRIS dan transfer bank lokal secara ilegal " * 2

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "note_id": long_note,
                                    "mechanism_tag": "slot_qris_depo",
                                }
                            )
                        }
                    }
                ]
            },
        )

    out_csv = tmp_path / "enriched_candidates.csv"
    audit_log = tmp_path / "enrichment_audit.jsonl"
    count = enrich_candidates_csv(
        candidates_csv=cand_csv,
        scraped_root=scraped_root,
        output_csv=out_csv,
        audit_log_path=audit_log,
        base_url="https://api.netra.ai/v1",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    assert count == 1
    rows = list(csv.DictReader(out_csv.open("r", encoding="utf-8")))
    assert len(rows) == 1
    assert len(rows[0]["note_140_id"]) <= 140
    assert rows[0]["mechanism_tag"] == "slot_qris_depo"
    assert audit_log.is_file()
