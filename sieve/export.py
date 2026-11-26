"""Batch exporter producing 20-URL .txt submission files and candidates.csv."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path

from sieve.canon import validate_and_canonicalize
from sieve.config import WIB, Settings
from sieve.store import SieveStore, UrlRecord

MAX_LINKS_PER_BATCH = 20


def export_batches(
    store: SieveStore,
    *,
    exports_root: Path,
    min_confidence: float = 0.70,
    batch_size: int = MAX_LINKS_PER_BATCH,
    run_date: str | None = None,
) -> dict[str, object]:
    """Export unexported high-confidence hits into 20-URL .txt files + candidates.csv."""
    if batch_size < 1 or batch_size > MAX_LINKS_PER_BATCH:
        raise ValueError(f"batch_size must be between 1 and {MAX_LINKS_PER_BATCH}")

    date_part = run_date or datetime.now(WIB).strftime("%Y-%m-%d")
    out_dir = exports_root / date_part
    out_dir.mkdir(parents=True, exist_ok=True)

    candidates = store.get_export_candidates(min_confidence=min_confidence)
    valid_records: list[tuple[UrlRecord, str]] = []
    for rec in candidates:
        # Strict re-validation of 300-char / no-IP / no-port / scheme constraints
        canon = validate_and_canonicalize(rec.raw_url)
        if canon is None or canon.canonical_key != rec.canonical_key:
            continue
        valid_records.append((rec, canon.submit_url))

    if not valid_records:
        return {
            "export_dir": str(out_dir),
            "batches": [],
            "exported_count": 0,
            "candidates_csv": None,
        }

    existing_batches = sorted(out_dir.glob("batch_*.txt"))
    next_idx = len(existing_batches) + 1
    batch_files: list[str] = []
    exported_rows: list[tuple[UrlRecord, str, str]] = []

    for start in range(0, len(valid_records), batch_size):
        chunk = valid_records[start : start + batch_size]
        batch_name = f"batch_{next_idx:04d}.txt"
        next_idx += 1
        batch_path = out_dir / batch_name
        batch_rel = f"{date_part}/{batch_name}"

        batch_path.write_text(
            "\n".join(submit_url for _, submit_url in chunk) + "\n",
            encoding="utf-8",
        )
        store.mark_exported([rec.canonical_key for rec, _ in chunk], batch_rel)
        batch_files.append(str(batch_path))
        for rec, submit_url in chunk:
            exported_rows.append((rec, submit_url, batch_rel))

    csv_path = out_dir / "candidates.csv"
    write_header = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if write_header:
            writer.writerow(
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
        for rec, submit_url, batch_rel in exported_rows:
            writer.writerow(
                [
                    rec.canonical_key,
                    submit_url,
                    rec.category or "",
                    rec.is_id_targeted or 0,
                    f"{rec.confidence:.4f}" if rec.confidence is not None else "",
                    rec.classifier_id or "",
                    rec.source,
                    batch_rel,
                    rec.artifact_path or "",
                ]
            )

    return {
        "export_dir": str(out_dir),
        "batches": batch_files,
        "exported_count": len(exported_rows),
        "candidates_csv": str(csv_path),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Export 20-URL submission batches")
    parser.add_argument("--min-confidence", type=float, default=0.70)
    parser.add_argument("--batch-size", type=int, default=MAX_LINKS_PER_BATCH)
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    with SieveStore(settings.db_path) as store:
        result = export_batches(
            store,
            exports_root=settings.exports_dir,
            min_confidence=args.min_confidence,
            batch_size=args.batch_size,
        )
    print(f"Exported {result['exported_count']} URLs into {len(result['batches'])} batch file(s)")


if __name__ == "__main__":
    main()
