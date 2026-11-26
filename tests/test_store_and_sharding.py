"""Tests for SQLite WAL store O(1) dedup, resume, and sharded artifact storage."""

from __future__ import annotations

import json
from pathlib import Path

from sieve.metrics import RunMetricsTracker, write_sharded_artifact
from sieve.store import SieveStore


def test_store_o1_dedup_across_sources_and_rapid_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "sieve.db"
    with SieveStore(db_path) as store:
        inserted, skipped = store.enqueue_many(
            [
                ("https://www.SlotGacor88.xyz/?utm=1", "certstream"),
                ("http://slotgacor88.xyz/#top", "ring"),
                ("slotgacor88.xyz/", "dnstwist"),
                ("https://slotgacor88.xyz/Daftar", "ring"),
                ("https://slotgacor88.xyz/daftar", "ring"),
                ("http://127.0.0.1/bad", "seed"),
            ]
        )
        assert inserted == 3
        assert skipped == 3

    # Simulate immediate restart 2 seconds later: seen_keys preloaded from SQLite
    with SieveStore(db_path) as reopened:
        assert "slotgacor88.xyz" in reopened.seen_keys
        assert "slotgacor88.xyz/Daftar" in reopened.seen_keys
        assert "slotgacor88.xyz/daftar" in reopened.seen_keys
        inserted2, skipped2 = reopened.enqueue_many(
            [
                ("https://www.slotgacor88.xyz", "nrd"),
                ("https://freshpinjol.id/", "nrd"),
            ]
        )
        assert inserted2 == 1
        assert skipped2 == 1


def test_crash_resume_only_claims_queued_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "sieve.db"
    with SieveStore(db_path) as store:
        store.enqueue_many(
            [
                ("https://one.example.com", "seed"),
                ("https://two.example.com", "seed"),
                ("https://three.example.com", "seed"),
            ]
        )
        store.mark_dead("one.example.com")
        store.mark_classified(
            "two.example.com",
            category="judol",
            is_id_targeted=True,
            confidence=0.95,
            classifier_id="bge-m3-zeroshot-v2.0",
            artifact_path="2026-11-26/judol/ab/abcd.json",
        )

    with SieveStore(db_path) as resumed:
        queued = resumed.claim_queued_batch(limit=10)
        assert [rec.canonical_key for rec in queued] == ["three.example.com"]


def test_sharded_artifact_layout_and_run_metrics(tmp_path: Path) -> None:
    scraped_root = tmp_path / "scraped"
    runs_dir = tmp_path / "runs"
    db_path = tmp_path / "sieve.db"

    canonical_hash = "ab" + "0" * 62
    relpath = write_sharded_artifact(
        scraped_root,
        canonical_key="slot77.xyz",
        canonical_hash=canonical_hash,
        raw_url="https://slot77.xyz",
        source="certstream",
        http_status=200,
        headers={"content-type": "text/html"},
        title="Slot77 Gacor",
        markdown="# Slot77\nDaftar sekarang",
        category="judol",
        is_id_targeted=True,
        confidence=0.94,
        classifier_id="bge-m3-zeroshot-v2.0",
        latency_ms=32.5,
        run_date="2026-11-26",
    )

    assert relpath == Path("2026-11-26/judol/ab") / f"{canonical_hash}.json"
    saved = json.loads((scraped_root / relpath).read_text(encoding="utf-8"))
    assert saved["canonical_key"] == "slot77.xyz"
    assert saved["is_id_targeted"] is True

    with SieveStore(db_path) as store:
        tracker = RunMetricsTracker(
            runs_dir=runs_dir,
            run_id="run-test-1",
            mode="batch",
            dns_mode="isp",
            classifier_id="bge-m3-zeroshot-v2.0",
            store=store,
            run_date="2026-11-26",
        )
        tracker.inc("discovered", 5)
        tracker.inc("dns_checked", 4)
        tracker.inc("dns_passed", 2)
        tracker.record_crawl_latency(420.0)
        tracker.record_classify_latency(25.0)
        tracker.log_error("crawler", "bad.example.com", "timeout")
        snap = tracker.snapshot(ended=True)

        assert snap["counts"]["errors"] == 1
        assert snap["dns_pass_rate"] == 0.5
        assert tracker.metrics_path.is_file()
        assert tracker.errors_path.is_file()
