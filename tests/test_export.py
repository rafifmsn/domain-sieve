"""Tests for batch export splitting, strict re-validation, and zero duplicate exports."""

from __future__ import annotations

import csv
from pathlib import Path

from sieve.export import export_batches
from sieve.store import SieveStore


def test_export_max_20_per_batch_and_zero_duplicate_exports(tmp_path: Path) -> None:
    db_path = tmp_path / "sieve.db"
    exports_root = tmp_path / "exports"

    with SieveStore(db_path) as store:
        urls = [(f"https://slot{i}.xyz", "certstream") for i in range(25)]
        urls.append(("https://legit-news.id", "seed"))
        store.enqueue_many(urls)

        for i in range(25):
            store.mark_classified(
                f"slot{i}.xyz",
                category="judol",
                is_id_targeted=(i % 2 == 0),
                confidence=0.90,
                classifier_id="bge-m3-zeroshot-v2.0",
                artifact_path=f"2026-11-26/judol/00/{i}.json",
            )
        store.mark_classified(
            "legit-news.id",
            category="benign",
            is_id_targeted=True,
            confidence=0.98,
            classifier_id="bge-m3-zeroshot-v2.0",
            artifact_path="2026-11-26/benign/00/legit.json",
        )

        # Inject a raw invalid >300 char row directly to verify strict re-validation at export
        with store.conn:
            store.conn.execute(
                """
                INSERT INTO urls (
                    canonical_key, canonical_hash, raw_url, source, status,
                    category, is_id_targeted, confidence, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "overlimit.xyz/" + "a" * 290,
                    "ff" * 32,
                    "https://overlimit.xyz/" + "a" * 290,
                    "seed",
                    "classified",
                    "judol",
                    1,
                    0.99,
                    "2026-11-26T10:00:00+07:00",
                ),
            )

        res1 = export_batches(
            store,
            exports_root=exports_root,
            min_confidence=0.70,
            run_date="2026-11-26",
        )
        assert res1["exported_count"] == 25
        batches = res1["batches"]
        assert len(batches) == 2

        b1_lines = Path(batches[0]).read_text(encoding="utf-8").splitlines()
        b2_lines = Path(batches[1]).read_text(encoding="utf-8").splitlines()
        assert len(b1_lines) == 20
        assert len(b2_lines) == 5
        assert all(len(u) <= 300 and u.startswith("https://") for u in b1_lines + b2_lines)

        csv_rows = list(csv.DictReader(Path(res1["candidates_csv"]).open("r", encoding="utf-8")))
        assert len(csv_rows) == 25
        # ID-targeted hits prioritized first
        assert csv_rows[0]["is_id_targeted"] == "1"

        # Consecutive export yields zero duplicates
        res2 = export_batches(
            store,
            exports_root=exports_root,
            min_confidence=0.70,
            run_date="2026-11-26",
        )
        assert res2["exported_count"] == 0
        assert res2["batches"] == []
