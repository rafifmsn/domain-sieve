"""Sharded artifact storage and structured JSONL/SQLite metrics tracking."""

from __future__ import annotations

import json
import os
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from sieve.config import WIB
from sieve.store import SieveStore, now_wib_iso


def build_artifact_relpath(
    *,
    category: str,
    canonical_hash: str,
    run_date: str | None = None,
) -> Path:
    """Compute sharded relative path: <YYYY-MM-DD>/<category>/<hash[:2]>/<canonical_hash>.json."""
    date_part = run_date or datetime.now(WIB).strftime("%Y-%m-%d")
    safe_category = category.strip().lower() or "error"
    prefix = canonical_hash[:2]
    return Path(date_part) / safe_category / prefix / f"{canonical_hash}.json"


def write_sharded_artifact(
    scraped_root: Path,
    *,
    canonical_key: str,
    canonical_hash: str,
    raw_url: str,
    source: str,
    http_status: int | None,
    headers: dict[str, str],
    title: str,
    markdown: str,
    category: str,
    is_id_targeted: bool,
    confidence: float,
    classifier_id: str,
    latency_ms: float,
    run_date: str | None = None,
) -> Path:
    """Write a scraped+classified artifact JSON into its sharded bucket and return relpath."""
    relpath = build_artifact_relpath(
        category=category,
        canonical_hash=canonical_hash,
        run_date=run_date,
    )
    full_path = scraped_root / relpath
    full_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "canonical_key": canonical_key,
        "canonical_hash": canonical_hash,
        "raw_url": raw_url,
        "source": source,
        "http_status": http_status,
        "headers": headers,
        "title": title,
        "markdown": markdown,
        "category": category,
        "is_id_targeted": is_id_targeted,
        "confidence": round(confidence, 6),
        "classifier_id": classifier_id,
        "latency_ms": round(latency_ms, 2),
        "recorded_at": now_wib_iso(),
    }
    full_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return relpath


class RunMetricsTracker:
    """Tracks per-stage counters, latencies, memory usage, and JSONL/SQLite logs."""

    def __init__(
        self,
        *,
        runs_dir: Path,
        run_id: str,
        mode: str,
        dns_mode: str,
        classifier_id: str,
        store: SieveStore | None = None,
        run_date: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.mode = mode
        self.dns_mode = dns_mode
        self.classifier_id = classifier_id
        self.store = store
        self._start_mono = time.monotonic()
        date_part = run_date or datetime.now(WIB).strftime("%Y-%m-%d")
        self.run_dir = runs_dir / date_part / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.metrics_path = self.run_dir / "metrics.jsonl"
        self.errors_path = self.run_dir / "errors.jsonl"

        self.counts: dict[str, int] = {
            "discovered": 0,
            "deduped": 0,
            "dns_checked": 0,
            "dns_passed": 0,
            "dead": 0,
            "crawled": 0,
            "classified": 0,
            "benign": 0,
            "exported": 0,
            "errors": 0,
        }
        self._crawl_latencies_ms: list[float] = []
        self._classify_latencies_ms: list[float] = []
        self._process = psutil.Process(os.getpid())

        if self.store is not None:
            self.store.record_run_start(
                run_id=self.run_id,
                mode=self.mode,
                dns_mode=self.dns_mode,
                classifier_id=self.classifier_id,
                counts=self.counts,
            )

    def inc(self, key: str, amount: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + amount

    def record_crawl_latency(self, latency_ms: float) -> None:
        self._crawl_latencies_ms.append(latency_ms)

    def record_classify_latency(self, latency_ms: float) -> None:
        self._classify_latencies_ms.append(latency_ms)

    def log_error(self, stage: str, target: str, message: str) -> None:
        self.inc("errors")
        entry = {
            "timestamp": now_wib_iso(),
            "run_id": self.run_id,
            "stage": stage,
            "target": target,
            "message": message,
        }
        with self.errors_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def snapshot(self, *, ended: bool = False) -> dict[str, Any]:
        elapsed_s = max(time.monotonic() - self._start_mono, 1e-6)
        dns_checked = self.counts["dns_checked"]
        dns_pass_rate = (self.counts["dns_passed"] / dns_checked) if dns_checked > 0 else 0.0
        total_classified = self.counts["classified"] + self.counts["benign"]
        rss_mb = self._process.memory_info().rss / (1024 * 1024)

        snap = {
            "timestamp": now_wib_iso(),
            "run_id": self.run_id,
            "elapsed_s": round(elapsed_s, 2),
            "counts": dict(self.counts),
            "dns_pass_rate": round(dns_pass_rate, 4),
            "crawl_p50_ms": round(statistics.median(self._crawl_latencies_ms), 2)
            if self._crawl_latencies_ms
            else 0.0,
            "classify_p50_ms": round(statistics.median(self._classify_latencies_ms), 2)
            if self._classify_latencies_ms
            else 0.0,
            "classify_throughput_per_s": round(total_classified / elapsed_s, 3),
            "rss_mb": round(rss_mb, 2),
            "system_ram_pct": round(psutil.virtual_memory().percent, 2),
        }
        with self.metrics_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(snap, ensure_ascii=False) + "\n")

        if self.store is not None:
            self.store.record_run_update(run_id=self.run_id, counts=self.counts, ended=ended)

        return snap
