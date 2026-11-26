"""SQLite WAL persistence layer with O(1) in-memory duplicate suppression."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sieve.canon import validate_and_canonicalize
from sieve.config import WIB

VALID_SOURCES = ("certstream", "ring", "dnstwist", "nrd", "crtsh", "seed")
VALID_STATUSES = ("queued", "dead", "crawled", "classified", "benign", "error", "exported")


def now_wib_iso() -> str:
    """Return current WIB (UTC+7) timestamp in ISO-8601 format."""
    return datetime.now(WIB).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class UrlRecord:
    """Row representation from the urls table."""

    canonical_key: str
    canonical_hash: str
    raw_url: str
    source: str
    status: str
    http_status: int | None
    category: str | None
    is_id_targeted: int | None
    confidence: float | None
    classifier_id: str | None
    artifact_path: str | None
    export_batch: str | None
    discovered_at: str
    crawled_at: str | None
    classified_at: str | None


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS urls (
    canonical_key   TEXT PRIMARY KEY,
    canonical_hash  TEXT NOT NULL,
    raw_url         TEXT NOT NULL,
    source          TEXT NOT NULL,
    status          TEXT NOT NULL,
    http_status     INTEGER,
    category        TEXT,
    is_id_targeted  INTEGER,
    confidence      REAL,
    classifier_id   TEXT,
    artifact_path   TEXT,
    export_batch    TEXT,
    discovered_at   TEXT NOT NULL,
    crawled_at      TEXT,
    classified_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_urls_status_discovered
    ON urls (status, discovered_at);

CREATE INDEX IF NOT EXISTS idx_urls_export_priority
    ON urls (status, is_id_targeted DESC, confidence DESC);

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    mode            TEXT NOT NULL,
    dns_mode        TEXT NOT NULL,
    classifier_id   TEXT NOT NULL,
    counts_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_runs (
    eval_id         TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    dataset_path    TEXT NOT NULL,
    classifier_id   TEXT NOT NULL,
    metrics_json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_cursors (
    source_name     TEXT PRIMARY KEY,
    cursor_value    TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


class SieveStore:
    """SQLite WAL state store with preloaded O(1) seen_keys set."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.execute("PRAGMA busy_timeout=5000;")
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()
        self.seen_keys: set[str] = self._load_seen_keys()

    def _load_seen_keys(self) -> set[str]:
        rows = self.conn.execute("SELECT canonical_key FROM urls").fetchall()
        return {row["canonical_key"] for row in rows}

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> SieveStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> UrlRecord:
        return UrlRecord(
            canonical_key=row["canonical_key"],
            canonical_hash=row["canonical_hash"],
            raw_url=row["raw_url"],
            source=row["source"],
            status=row["status"],
            http_status=row["http_status"],
            category=row["category"],
            is_id_targeted=row["is_id_targeted"],
            confidence=row["confidence"],
            classifier_id=row["classifier_id"],
            artifact_path=row["artifact_path"],
            export_batch=row["export_batch"],
            discovered_at=row["discovered_at"],
            crawled_at=row["crawled_at"],
            classified_at=row["classified_at"],
        )

    def enqueue_many(self, candidates: Iterable[tuple[str, str]]) -> tuple[int, int]:
        """Validate, canonicalize, and insert new URLs with O(1) dedup.

        Args:
            candidates: Iterable of (raw_url, source) tuples.

        Returns:
            Tuple of (inserted_count, deduped_or_invalid_count).
        """
        now = now_wib_iso()
        rows_to_insert: list[tuple[str, str, str, str, str, str]] = []
        pending_keys: set[str] = set()
        skipped = 0

        for raw_url, source in candidates:
            canon = validate_and_canonicalize(raw_url)
            if canon is None:
                skipped += 1
                continue
            if canon.canonical_key in self.seen_keys or canon.canonical_key in pending_keys:
                skipped += 1
                continue
            pending_keys.add(canon.canonical_key)
            rows_to_insert.append(
                (
                    canon.canonical_key,
                    canon.canonical_hash,
                    canon.fetch_url,
                    source,
                    "queued",
                    now,
                )
            )

        if rows_to_insert:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT OR IGNORE INTO urls (
                        canonical_key, canonical_hash, raw_url, source, status, discovered_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    rows_to_insert,
                )
            self.seen_keys.update(pending_keys)

        return len(rows_to_insert), skipped

    def claim_queued_batch(
        self,
        limit: int = 100,
        exclude_keys: set[str] | None = None,
    ) -> list[UrlRecord]:
        """Fetch a batch of queued URLs without altering DB status before processing."""
        if limit <= 0:
            return []
        fetch_limit = limit + (len(exclude_keys) if exclude_keys else 0)
        rows = self.conn.execute(
            """
            SELECT * FROM urls
            WHERE status = 'queued'
            ORDER BY
                CASE WHEN source IN ('seed', 'ring', 'dnstwist') THEN 0 ELSE 1 END,
                discovered_at DESC
            LIMIT ?
            """,
            (fetch_limit,),
        ).fetchall()
        results: list[UrlRecord] = []
        for row in rows:
            if exclude_keys and row["canonical_key"] in exclude_keys:
                continue
            results.append(self._row_to_record(row))
            if len(results) >= limit:
                break
        return results

    def mark_dead(self, canonical_key: str, http_status: int | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE urls SET status = 'dead', http_status = COALESCE(?, http_status) WHERE canonical_key = ?",
                (http_status, canonical_key),
            )

    def mark_crawled(self, canonical_key: str, http_status: int | None = None) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE urls
                SET status = 'crawled', http_status = ?, crawled_at = ?
                WHERE canonical_key = ?
                """,
                (http_status, now_wib_iso(), canonical_key),
            )

    def mark_classified(
        self,
        canonical_key: str,
        *,
        category: str,
        is_id_targeted: bool,
        confidence: float,
        classifier_id: str,
        artifact_path: str,
        http_status: int | None = None,
    ) -> None:
        status = "benign" if category == "benign" else "classified"
        now = now_wib_iso()
        with self.conn:
            self.conn.execute(
                """
                UPDATE urls
                SET status = ?,
                    http_status = COALESCE(?, http_status),
                    category = ?,
                    is_id_targeted = ?,
                    confidence = ?,
                    classifier_id = ?,
                    artifact_path = ?,
                    crawled_at = COALESCE(crawled_at, ?),
                    classified_at = ?
                WHERE canonical_key = ?
                """,
                (
                    status,
                    http_status,
                    category,
                    1 if is_id_targeted else 0,
                    confidence,
                    classifier_id,
                    artifact_path,
                    now,
                    now,
                    canonical_key,
                ),
            )

    def mark_error(
        self,
        canonical_key: str,
        *,
        http_status: int | None = None,
        artifact_path: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE urls
                SET status = 'error',
                    http_status = COALESCE(?, http_status),
                    artifact_path = COALESCE(?, artifact_path)
                WHERE canonical_key = ?
                """,
                (http_status, artifact_path, canonical_key),
            )

    def get_export_candidates(
        self,
        *,
        min_confidence: float = 0.70,
        limit: int | None = None,
    ) -> list[UrlRecord]:
        sql = """
            SELECT * FROM urls
            WHERE status = 'classified'
              AND category IS NOT NULL
              AND category != 'benign'
              AND confidence >= ?
            ORDER BY is_id_targeted DESC, confidence DESC, discovered_at ASC
        """
        params: list[Any] = [min_confidence]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    def mark_exported(self, canonical_keys: Iterable[str], export_batch: str) -> int:
        keys = list(canonical_keys)
        if not keys:
            return 0
        with self.conn:
            cur = self.conn.executemany(
                """
                UPDATE urls
                SET status = 'exported', export_batch = ?
                WHERE canonical_key = ? AND status = 'classified'
                """,
                [(export_batch, key) for key in keys],
            )
            return cur.rowcount

    def get_url(self, canonical_key: str) -> UrlRecord | None:
        row = self.conn.execute(
            "SELECT * FROM urls WHERE canonical_key = ?",
            (canonical_key,),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def count_queued(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS cnt FROM urls WHERE status = 'queued'").fetchone()
        return int(row["cnt"]) if row is not None else 0

    def count_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM urls GROUP BY status"
        ).fetchall()
        return {row["status"]: int(row["cnt"]) for row in rows}

    def record_run_start(
        self,
        *,
        run_id: str,
        mode: str,
        dns_mode: str,
        classifier_id: str,
        counts: dict[str, int],
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO runs (
                    run_id, started_at, mode, dns_mode, classifier_id, counts_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, now_wib_iso(), mode, dns_mode, classifier_id, json.dumps(counts)),
            )

    def record_run_update(
        self,
        *,
        run_id: str,
        counts: dict[str, int],
        ended: bool = False,
    ) -> None:
        with self.conn:
            if ended:
                self.conn.execute(
                    "UPDATE runs SET ended_at = ?, counts_json = ? WHERE run_id = ?",
                    (now_wib_iso(), json.dumps(counts), run_id),
                )
            else:
                self.conn.execute(
                    "UPDATE runs SET counts_json = ? WHERE run_id = ?",
                    (json.dumps(counts), run_id),
                )

    def record_eval_run(
        self,
        *,
        eval_id: str,
        dataset_path: str,
        classifier_id: str,
        metrics: dict[str, Any],
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO eval_runs (
                    eval_id, created_at, dataset_path, classifier_id, metrics_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (eval_id, now_wib_iso(), dataset_path, classifier_id, json.dumps(metrics)),
            )

    def get_cursor(self, source_name: str) -> str | None:
        row = self.conn.execute(
            "SELECT cursor_value FROM source_cursors WHERE source_name = ?",
            (source_name,),
        ).fetchone()
        return str(row["cursor_value"]) if row is not None else None

    def set_cursor(self, source_name: str, cursor_value: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO source_cursors (source_name, cursor_value, updated_at)
                VALUES (?, ?, ?)
                """,
                (source_name, cursor_value, now_wib_iso()),
            )
