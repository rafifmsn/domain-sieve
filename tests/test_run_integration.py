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
from sieve.run import run_pipeline
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
