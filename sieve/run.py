"""Async bounded-queue pipeline orchestrator with clean SIGINT shutdown and resume."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import uuid
from pathlib import Path

from sieve.classify import Classifier, ClassifyInput, build_classifier
from sieve.config import ILLEGAL_CATEGORIES, Settings
from sieve.crawler import CrawlOutcome, CrawlPool, prefilter_live_targets
from sieve.discover import (
    consume_certstream,
    extract_ring_candidates,
    fetch_crtsh_domains,
    generate_dnstwist_candidates,
    ingest_nrd_file,
)
from sieve.metrics import RunMetricsTracker, write_sharded_artifact
from sieve.store import SieveStore, UrlRecord

logger = logging.getLogger(__name__)


async def run_pipeline(
    *,
    settings: Settings,
    classifier: Classifier,
    seed_files: list[Path] | None = None,
    nrd_files: list[Path] | None = None,
    crtsh_keywords: list[str] | None = None,
    enable_certstream: bool = False,
    enable_dnstwist: bool = False,
    max_batches: int | None = None,
    batch_size: int = 32,
    duration_seconds: float | None = None,
) -> dict[str, int]:
    """Execute Discovery -> Pre-Filter -> Crawl4AI -> Micro-batched Classifier -> Sharded Store."""
    run_id = f"run-{uuid.uuid4().hex[:10]}"
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    with SieveStore(settings.db_path) as store:
        tracker = RunMetricsTracker(
            runs_dir=settings.runs_dir,
            run_id=run_id,
            mode="stream" if enable_certstream else "batch",
            dns_mode=settings.dns_mode,
            classifier_id=classifier.classifier_id,
            store=store,
        )

        # Backward historical seeds & NRD ingestion before opening browser pool
        for seed_path in seed_files or []:
            if seed_path.is_file():
                ins, skip = ingest_nrd_file(
                    seed_path,
                    store,
                    cursor_name=f"seed:{seed_path.name}",
                    source="seed",
                )
                tracker.inc("discovered", ins)
                tracker.inc("deduped", skip)

        for nrd_path in nrd_files or []:
            if nrd_path.is_file():
                ins, skip = ingest_nrd_file(
                    nrd_path,
                    store,
                    cursor_name=f"nrd:{nrd_path.name}",
                    source="nrd",
                )
                tracker.inc("discovered", ins)
                tracker.inc("deduped", skip)

        for kw in crtsh_keywords or []:
            try:
                domains = await fetch_crtsh_domains(kw, limit=200)
                ins, skip = store.enqueue_many((d, "crtsh") for d in domains)
                tracker.inc("discovered", ins)
                tracker.inc("deduped", skip)
            except Exception as exc:
                tracker.log_error("discover_crtsh", kw, str(exc))

        certstream_task: asyncio.Task[int] | None = None
        if enable_certstream:
            certstream_task = asyncio.create_task(
                consume_certstream(settings.certstream_url, store, stop_event=stop_event)
            )

        async def _timeout_watcher() -> None:
            if duration_seconds is not None and duration_seconds > 0:
                await asyncio.sleep(duration_seconds)
                stop_event.set()

        timer_task = asyncio.create_task(_timeout_watcher()) if duration_seconds else None

        classify_queue: asyncio.Queue[CrawlOutcome | None] = asyncio.Queue(
            maxsize=max(batch_size * 4, 64)
        )
        in_flight: set[str] = set()
        hop_depths: dict[str, int] = {}

        async def _classifier_worker() -> None:
            pending: list[CrawlOutcome] = []
            while True:
                item = await classify_queue.get()
                if item is None:
                    classify_queue.task_done()
                    break
                pending.append(item)
                while len(pending) < settings.nli_batch_size and not classify_queue.empty():
                    nxt = classify_queue.get_nowait()
                    if nxt is None:
                        classify_queue.task_done()
                        await classify_queue.put(None)
                        break
                    pending.append(nxt)

                if pending:
                    await _score_and_persist(pending)
                    for _ in pending:
                        classify_queue.task_done()
                    pending.clear()

            if pending:
                await _score_and_persist(pending)
                for _ in pending:
                    classify_queue.task_done()

        async def _score_and_persist(outcomes: list[CrawlOutcome]) -> None:
            inputs = [
                ClassifyInput(url=o.raw_url, markdown=o.markdown, title=o.title) for o in outcomes
            ]
            try:
                verdicts = await asyncio.to_thread(classifier.classify_batch, inputs)
            except Exception as exc:
                for o in outcomes:
                    tracker.log_error("classify", o.canonical_key, str(exc))
                    store.mark_error(o.canonical_key, http_status=o.http_status)
                return

            ring_buffer: list[tuple[str, str]] = []
            dnstwist_buffer: list[tuple[str, str]] = []

            for outcome, verdict in zip(outcomes, verdicts, strict=True):
                tracker.record_classify_latency(verdict.latency_ms)
                relpath = write_sharded_artifact(
                    settings.scraped_dir,
                    canonical_key=outcome.canonical_key,
                    canonical_hash=outcome.canonical_hash,
                    raw_url=outcome.raw_url,
                    source=outcome.source,
                    http_status=outcome.http_status,
                    headers=outcome.headers,
                    title=outcome.title,
                    markdown=outcome.markdown,
                    category=verdict.category,
                    is_id_targeted=verdict.is_id_targeted,
                    confidence=verdict.confidence,
                    classifier_id=verdict.classifier_id,
                    latency_ms=verdict.latency_ms,
                )
                store.mark_classified(
                    outcome.canonical_key,
                    category=verdict.category,
                    is_id_targeted=verdict.is_id_targeted,
                    confidence=verdict.confidence,
                    classifier_id=verdict.classifier_id,
                    artifact_path=str(relpath),
                    http_status=outcome.http_status,
                )
                if verdict.category == "benign":
                    tracker.inc("benign")
                else:
                    tracker.inc("classified")

                if verdict.category in ILLEGAL_CATEGORIES:
                    src_host = outcome.canonical_key.split("/", 1)[0]
                    depth = hop_depths.get(outcome.canonical_key, 0)
                    ring_urls = extract_ring_candidates(
                        source_category=verdict.category,
                        source_host=src_host,
                        outbound_links=outcome.outbound_links,
                        hop_depth=depth,
                        max_hop_depth=2,
                    )
                    for rurl in ring_urls:
                        from sieve.canon import validate_and_canonicalize

                        rcanon = validate_and_canonicalize(rurl)
                        if rcanon is not None:
                            hop_depths.setdefault(rcanon.canonical_key, depth + 1)
                        ring_buffer.append((rurl, "ring"))
                    if enable_dnstwist and verdict.confidence >= 0.75:
                        for durl in generate_dnstwist_candidates(src_host, max_variants=20):
                            dnstwist_buffer.append((durl, "dnstwist"))

            if ring_buffer:
                ins, skip = store.enqueue_many(ring_buffer)
                tracker.inc("discovered", ins)
                tracker.inc("deduped", skip)
            if dnstwist_buffer:
                ins, skip = store.enqueue_many(dnstwist_buffer)
                tracker.inc("discovered", ins)
                tracker.inc("deduped", skip)

        clf_task = asyncio.create_task(_classifier_worker())

        try:
            async with CrawlPool(
                doh_enabled=settings.doh_enabled,
                concurrency=settings.crawl_concurrency,
                memory_threshold_pct=settings.memory_threshold_pct,
            ) as pool:
                processed_batches = 0
                while not stop_event.is_set():
                    if max_batches is not None and processed_batches >= max_batches:
                        break
                    queued: list[UrlRecord] = store.claim_queued_batch(
                        limit=batch_size,
                        exclude_keys=in_flight,
                    )
                    if not queued:
                        if enable_certstream and not stop_event.is_set():
                            await asyncio.sleep(1.0)
                            continue
                        await classify_queue.join()
                        queued = store.claim_queued_batch(
                            limit=batch_size,
                            exclude_keys=in_flight,
                        )
                        if not queued:
                            break

                    processed_batches += 1
                    for rec in queued:
                        in_flight.add(rec.canonical_key)

                    live, dead = await prefilter_live_targets(
                        queued,
                        doh_enabled=settings.doh_enabled,
                    )
                    tracker.inc("dns_checked", len(queued))
                    tracker.inc("dns_passed", len(live))
                    tracker.inc("dead", len(dead))

                    for dead_key, reason in dead:
                        store.mark_dead(dead_key)
                        in_flight.discard(dead_key)
                        logger.debug("Pre-filter dropped %s (%s)", dead_key, reason)

                    if live:
                        outcomes = await pool.crawl_batch(live)
                        for outcome in outcomes:
                            in_flight.discard(outcome.canonical_key)
                            if outcome.status == "crawled":
                                tracker.inc("crawled")
                                tracker.record_crawl_latency(outcome.latency_ms)
                                store.mark_crawled(
                                    outcome.canonical_key,
                                    http_status=outcome.http_status,
                                )
                                await classify_queue.put(outcome)
                            elif outcome.status == "isp_blocked":
                                tracker.inc("dead")
                                store.mark_dead(
                                    outcome.canonical_key,
                                    http_status=outcome.http_status,
                                )
                            else:
                                store.mark_error(
                                    outcome.canonical_key,
                                    http_status=outcome.http_status,
                                )
                                tracker.log_error(
                                    "crawler",
                                    outcome.canonical_key,
                                    outcome.error_message,
                                )

                    tracker.snapshot(ended=False)
        finally:
            stop_event.set()
            if timer_task is not None:
                timer_task.cancel()
            if certstream_task is not None:
                certstream_task.cancel()
                await asyncio.gather(certstream_task, return_exceptions=True)
            await classify_queue.put(None)
            await clf_task
            final_snap = tracker.snapshot(ended=True)
            logger.info("Run %s complete: %s", run_id, final_snap["counts"])

    return dict(tracker.counts)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run domain-sieve pipeline")
    parser.add_argument("--classifier", choices=["nli", "jev"], default=None)
    parser.add_argument("--doh", action="store_true", help="Enable Cloudflare DoH 1.1.1.1")
    parser.add_argument("--certstream", action="store_true", help="Consume CertStream WebSocket")
    parser.add_argument("--dnstwist", action="store_true", help="Generate dnstwist variants on hits")
    parser.add_argument("--seed-file", type=Path, action="append", default=[])
    parser.add_argument("--nrd-file", type=Path, action="append", default=[])
    parser.add_argument("--crtsh", action="append", default=[], help="Historical crt.sh keyword")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--max-batches", type=int, default=None)
    parser.add_argument("--duration-seconds", type=float, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env(doh_enabled=args.doh, classifier_backend=args.classifier)
    classifier = build_classifier(
        settings.classifier_backend,
        nli_model_id=settings.nli_model_id,
        nli_batch_size=settings.nli_batch_size,
        typesafe_api_key=settings.typesafe_api_key,
        typesafe_base_url=settings.typesafe_base_url,
    )

    counts = asyncio.run(
        run_pipeline(
            settings=settings,
            classifier=classifier,
            seed_files=args.seed_file,
            nrd_files=args.nrd_file,
            crtsh_keywords=args.crtsh,
            enable_certstream=args.certstream,
            enable_dnstwist=args.dnstwist,
            max_batches=args.max_batches,
            batch_size=args.batch_size,
            duration_seconds=args.duration_seconds,
        )
    )
    print(f"Pipeline finished: {counts}")


if __name__ == "__main__":
    main()
