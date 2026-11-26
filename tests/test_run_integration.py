"""End-to-end async pipeline integration test with mocked CrawlPool and stub classifier."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from sieve.classify import ClassificationResult, ClassifyInput, detect_indonesian_targeting
from sieve.config import Settings
from sieve.crawler import CrawlOutcome
from sieve.export import export_batches
from sieve.metrics import RunMetricsTracker
from sieve.run import build_live_dashboard, run_pipeline
from sieve.store import SieveStore, UrlRecord


class _MockClassifier:
    classifier_id = "mock-nli"

    def classify_batch(self, items: Sequence[ClassifyInput]) -> list[ClassificationResult]:
        results: list[ClassificationResult] = []
        for item in items:
            text = f"{item.title} {item.markdown}".lower()
            cat = "judol" if "slot" in text or "gacor" in text else "benign"
            results.append(
                ClassificationResult(
                    category=cat,
                    is_id_targeted=detect_indonesian_targeting(item.url, text),
                    confidence=0.92,
                    classifier_id=self.classifier_id,
                    latency_ms=5.0,
                )
            )
        return results


def test_run_pipeline_end_to_end_and_export(tmp_path: Path, monkeypatch) -> None:
    seed_file = tmp_path / "seeds.txt"
    seed_file.write_text("https://slot1.xyz\nhttps://news-ok.id\n", encoding="utf-8")

    async def fake_prefilter(records: Sequence[UrlRecord], **_: object):
        return list(records), []

    class _MockPool:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> _MockPool:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def crawl_batch(self, records: Sequence[UrlRecord]) -> list[CrawlOutcome]:
            outcomes: list[CrawlOutcome] = []
            for rec in records:
                if "slot1" in rec.canonical_key:
                    outcomes.append(
                        CrawlOutcome(
                            canonical_key=rec.canonical_key,
                            canonical_hash=rec.canonical_hash,
                            raw_url=rec.raw_url,
                            source=rec.source,
                            status="crawled",
                            http_status=200,
                            headers={},
                            title="Slot1 Gacor",
                            markdown="Deposit QRIS slot gacor maxwin",
                            outbound_links=["https://mirror-slot2.vip/daftar"],
                            latency_ms=100.0,
                        )
                    )
                elif "mirror-slot2" in rec.canonical_key:
                    outcomes.append(
                        CrawlOutcome(
                            canonical_key=rec.canonical_key,
                            canonical_hash=rec.canonical_hash,
                            raw_url=rec.raw_url,
                            source=rec.source,
                            status="crawled",
                            http_status=200,
                            headers={},
                            title="Mirror Slot2",
                            markdown="Slot gacor bonus new member",
                            outbound_links=[],
                            latency_ms=110.0,
                        )
                    )
                else:
                    outcomes.append(
                        CrawlOutcome(
                            canonical_key=rec.canonical_key,
                            canonical_hash=rec.canonical_hash,
                            raw_url=rec.raw_url,
                            source=rec.source,
                            status="crawled",
                            http_status=200,
                            headers={},
                            title="Berita Terkini",
                            markdown="Laporan ekonomi kuartal ini",
                            outbound_links=[],
                            latency_ms=90.0,
                        )
                    )
            return outcomes

    monkeypatch.setattr("sieve.run.prefilter_live_targets", fake_prefilter)
    monkeypatch.setattr("sieve.run.CrawlPool", _MockPool)

    settings = Settings.from_env(data_dir=tmp_path / "data")
    counts = asyncio.run(
        run_pipeline(
            settings=settings,
            classifier=_MockClassifier(),
            seed_files=[seed_file],
            max_batches=2,
            batch_size=10,
        )
    )

    assert counts["crawled"] == 3  # 2 seeds + 1 ring-crawled mirror
    assert counts["classified"] == 2
    assert counts["benign"] == 1

    with SieveStore(settings.db_path) as store:
        exported = export_batches(
            store,
            exports_root=settings.exports_dir,
            min_confidence=0.70,
            run_date="2026-11-26",
        )
        assert exported["exported_count"] == 2

    run_dirs = list(settings.runs_dir.glob("*/*/metrics.jsonl"))
    assert len(run_dirs) == 1
    last_snap = json.loads(run_dirs[0].read_text(encoding="utf-8").splitlines()[-1])
    assert last_snap["counts"]["classified"] == 2


def test_rich_live_dashboard_renders(tmp_path: Path) -> None:
    from rich.console import Console

    settings = Settings.from_env(data_dir=tmp_path / "data")
    tracker = RunMetricsTracker(
        runs_dir=settings.runs_dir,
        run_id="run-ui-test",
        mode="ctlog",
        dns_mode="isp",
        classifier_id="bge-m3-zeroshot-v2.0",
    )
    tracker.inc("ct_scanned", 500)
    tracker.inc("discovered", 12)
    tracker.record_hit(
        canonical_key="slot88.xyz",
        category="judol",
        is_id_targeted=True,
        confidence=0.95,
    )
    dash = build_live_dashboard(tracker, settings=settings, queued_count=4)
    console = Console(record=True, width=100)
    console.print(dash)
    rendered = console.export_text()
    assert "domain-sieve" in rendered
    assert "slot88.xyz" in rendered
    assert "judol" in rendered


def test_run_pipeline_continuous_crawl_one_workers(tmp_path: Path, monkeypatch) -> None:
    seed_file = tmp_path / "seeds.txt"
    seed_file.write_text("https://slot1.xyz\nhttps://news-ok.id\n", encoding="utf-8")

    async def fake_prefilter(records: Sequence[UrlRecord], **_: object):
        return list(records), []

    class _StreamingMockPool:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> _StreamingMockPool:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def crawl_one(self, rec: UrlRecord) -> CrawlOutcome:
            is_slot = "slot1" in rec.canonical_key
            return CrawlOutcome(
                canonical_key=rec.canonical_key,
                canonical_hash=rec.canonical_hash,
                raw_url=rec.raw_url,
                source=rec.source,
                status="crawled",
                http_status=200,
                headers={},
                title="Slot1 Gacor" if is_slot else "Berita Terkini",
                markdown="Deposit QRIS slot gacor maxwin" if is_slot else "Berita ekonomi hari ini",
                outbound_links=[],
                latency_ms=40.0,
            )

    monkeypatch.setattr("sieve.run.prefilter_live_targets", fake_prefilter)
    monkeypatch.setattr("sieve.run.CrawlPool", _StreamingMockPool)

    settings = Settings.from_env(data_dir=tmp_path / "data")
    counts = asyncio.run(
        run_pipeline(
            settings=settings,
            classifier=_MockClassifier(),
            seed_files=[seed_file],
            batch_size=10,
        )
    )
    assert counts["crawled"] == 2
    assert counts["classified"] == 1
    assert counts["benign"] == 1
