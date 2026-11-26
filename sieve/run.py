"""Async bounded-queue pipeline orchestrator with rich live dashboard and resume."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
import uuid
from contextlib import nullcontext
from pathlib import Path

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from sieve.classify import Classifier, ClassifyInput, build_classifier
from sieve.config import ILLEGAL_CATEGORIES, Settings
from sieve.crawler import CrawlOutcome, CrawlPool, prefilter_live_targets
from sieve.discover import (
    consume_certstream,
    extract_ring_candidates,
    fetch_crtsh_domains,
    generate_dnstwist_candidates,
    ingest_nrd_file,
    poll_ct_logs,
)
from sieve.metrics import RunMetricsTracker, write_sharded_artifact
from sieve.store import SieveStore, UrlRecord

logger = logging.getLogger(__name__)


def build_live_dashboard(
    tracker: RunMetricsTracker,
    *,
    settings: Settings,
    queued_count: int,
) -> Group:
    """Render a compact interactive Rich dashboard for pipeline progress and liveness."""
    snap = {
        "elapsed_s": max(time.monotonic() - tracker._start_mono, 1e-6),
        "idle_s": max(time.monotonic() - tracker.last_activity_mono, 0.0),
    }
    elapsed_m, elapsed_sec = divmod(int(snap["elapsed_s"]), 60)
    elapsed_h, elapsed_m = divmod(elapsed_m, 60)
    elapsed_str = f"{elapsed_h:02d}:{elapsed_m:02d}:{elapsed_sec:02d}"

    counts = tracker.counts
    dns_checked = counts.get("dns_checked", 0)
    dns_pass_pct = (100.0 * counts.get("dns_passed", 0) / dns_checked) if dns_checked else 0.0
    total_done = counts.get("classified", 0) + counts.get("benign", 0)
    pages_per_min = (total_done / snap["elapsed_s"]) * 60.0
    rss_mb = tracker._process.memory_info().rss / (1024 * 1024)

    header = Text()
    header.append("domain-sieve", style="bold cyan")
    header.append(f"  run={tracker.run_id}  mode={tracker.mode}  dns={settings.dns_mode}  ")
    header.append(f"clf={tracker.classifier_id}\n", style="dim")
    header.append(f"stage: ", style="dim")
    header.append(tracker.current_stage, style="bold green")
    header.append(f"   elapsed: {elapsed_str}   last_signal: {snap['idle_s']:.1f}s ago")

    funnel = Table(title="Pipeline Funnel", expand=True, show_edge=True)
    funnel.add_column("Stage", style="cyan")
    funnel.add_column("Count", justify="right", style="bold")
    funnel.add_column("Metric", style="magenta")
    funnel.add_column("Value", justify="right")

    import statistics

    import psutil

    crawl_p50 = (
        f"{statistics.median(tracker._crawl_latencies_ms):.1f} ms"
        if tracker._crawl_latencies_ms
        else "-"
    )
    clf_p50 = (
        f"{statistics.median(tracker._classify_latencies_ms):.1f} ms"
        if tracker._classify_latencies_ms
        else "-"
    )
    sys_ram = f"{psutil.virtual_memory().percent:.1f}%"

    funnel.add_row(
        "CT Scanned",
        f"{counts.get('ct_scanned', 0):,}",
        "DNS Pass Rate",
        f"{dns_pass_pct:.1f}%",
    )
    funnel.add_row(
        "Discovered / Queued",
        f"{counts.get('discovered', 0):,} / {queued_count:,}",
        "Crawl p50",
        crawl_p50,
    )
    funnel.add_row(
        "Deduped (O(1))",
        f"{counts.get('deduped', 0):,}",
        "Classify p50",
        clf_p50,
    )
    funnel.add_row(
        "DNS Dead / Crawled",
        f"{counts.get('dead', 0):,} / {counts.get('crawled', 0):,}",
        "Throughput",
        f"{pages_per_min:.1f} pages/min",
    )
    funnel.add_row(
        "Classified / Benign",
        f"{counts.get('classified', 0):,} / {counts.get('benign', 0):,}",
        "RSS / Sys RAM",
        f"{rss_mb:.0f} MB / {sys_ram}",
    )
    funnel.add_row(
        "Errors",
        f"{counts.get('errors', 0):,}",
        "Categories",
        ", ".join(f"{k}:{v}" for k, v in sorted(tracker.category_counts.items())) or "-",
    )

    hits_table = Table(title="Recent Classified Hits", expand=True)
    hits_table.add_column("Time", width=9)
    hits_table.add_column("Category", width=14, style="bold yellow")
    hits_table.add_column("ID", width=4, justify="center")
    hits_table.add_column("Conf", width=6, justify="right")
    hits_table.add_column("Canonical Key", overflow="ellipsis")

    if tracker.recent_hits:
        for hit in tracker.recent_hits:
            hits_table.add_row(
                str(hit["time"]),
                str(hit["category"]),
                "Y" if hit["is_id"] else "-",
                f"{hit['confidence']:.2f}",
                str(hit["key"]),
            )
    else:
        hits_table.add_row("-", "waiting", "-", "-", "no illegal hits classified yet")

    parts: list[Any] = [Panel(header, border_style="cyan"), funnel, hits_table]
    if tracker.last_error:
        parts.append(Panel(Text(tracker.last_error, style="red"), title="Last Error"))
    return Group(*parts)


async def run_pipeline(
    *,
    settings: Settings,
    classifier: Classifier,
    seed_files: list[Path] | None = None,
    nrd_files: list[Path] | None = None,
    crtsh_keywords: list[str] | None = None,
    enable_certstream: bool = False,
    enable_ct_logs: bool | None = None,
    enable_dnstwist: bool = False,
    max_batches: int | None = None,
    batch_size: int = 64,
    duration_seconds: float | None = None,
    live_ui: bool = False,
) -> dict[str, int]:
    """Execute Discovery -> Pre-Filter -> Crawl4AI -> Micro-batched Classifier -> Sharded Store."""
    run_id = f"run-{uuid.uuid4().hex[:10]}"
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    has_manual_sources = bool(seed_files or nrd_files or crtsh_keywords or enable_certstream)
    use_ct_logs = enable_ct_logs if enable_ct_logs is not None else (not has_manual_sources)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    mode = "ctlog" if use_ct_logs else ("certstream" if enable_certstream else "batch")

    with SieveStore(settings.db_path) as store:
        tracker = RunMetricsTracker(
            runs_dir=settings.runs_dir,
            run_id=run_id,
            mode=mode,
            dns_mode=settings.dns_mode,
            classifier_id=classifier.classifier_id,
            store=store,
        )

        # Redirect root logger to run.log when rich Live dashboard owns the terminal
        file_handler: logging.FileHandler | None = None
        root_logger = logging.getLogger()
        saved_handlers: list[logging.Handler] = []
        if live_ui:
            saved_handlers = list(root_logger.handlers)
            for h in saved_handlers:
                root_logger.removeHandler(h)
            file_handler = logging.FileHandler(tracker.run_dir / "run.log", encoding="utf-8")
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
            )
            root_logger.addHandler(file_handler)

        # Backward historical seeds & NRD ingestion before opening browser pool
        tracker.set_stage("ingesting seeds")
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

        def _on_ct_batch(scanned: int, inserted: int, skipped: int, _log_url: str) -> None:
            tracker.inc("ct_scanned", scanned)
            if inserted:
                tracker.inc("discovered", inserted)
            if skipped:
                tracker.inc("deduped", skipped)

        discovery_tasks: list[asyncio.Task[int]] = []
        if enable_certstream:
            discovery_tasks.append(
                asyncio.create_task(
                    consume_certstream(settings.certstream_url, store, stop_event=stop_event)
                )
            )
        if use_ct_logs:
            discovery_tasks.append(
                asyncio.create_task(
                    poll_ct_logs(store, stop_event=stop_event, on_batch=_on_ct_batch)
                )
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
            tracker.set_stage(f"classifying batch ({len(outcomes)})")
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
                tracker.record_hit(
                    canonical_key=outcome.canonical_key,
                    category=verdict.category,
                    is_id_targeted=verdict.is_id_targeted,
                    confidence=verdict.confidence,
                )
                if verdict.category == "benign":
                    tracker.inc("benign")
                else:
                    tracker.inc("classified")

                if verdict.category in ILLEGAL_CATEGORIES and verdict.confidence >= 0.70:
                    from sieve.canon import validate_and_canonicalize

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

        async def _handle_crawl_outcome(outcome: CrawlOutcome) -> None:
            in_flight.discard(outcome.canonical_key)
            if outcome.status == "crawled":
                tracker.inc("crawled")
                tracker.record_crawl_latency(outcome.latency_ms)
                store.mark_crawled(
                    outcome.canonical_key,
                    http_status=outcome.http_status,
                )
                await classify_queue.put(outcome)
            elif outcome.status in ("isp_blocked", "dead"):
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

        clf_task = asyncio.create_task(_classifier_worker())

        def _render_ui() -> Group:
            q_cnt = store.count_queued()
            return build_live_dashboard(tracker, settings=settings, queued_count=q_cnt)

        live_ctx: Any = (
            Live(_render_ui(), refresh_per_second=2, screen=False)
            if live_ui
            else nullcontext(None)
        )

        async def _ui_refresher(live: Live | None) -> None:
            if live is None:
                return
            while not stop_event.is_set():
                live.update(_render_ui())
                await asyncio.sleep(0.5)

        ui_task: asyncio.Task[None] | None = None
        crawl_workers: list[asyncio.Task[None]] = []
        live_queue: asyncio.Queue[UrlRecord | None] = asyncio.Queue(
            maxsize=max(settings.crawl_concurrency * 3, 48)
        )
        try:
            with live_ctx as live:
                if live is not None:
                    ui_task = asyncio.create_task(_ui_refresher(live))

                tracker.set_stage("launching browser pool")
                async with CrawlPool(
                    doh_enabled=settings.doh_enabled,
                    concurrency=settings.crawl_concurrency,
                    memory_threshold_pct=settings.memory_threshold_pct,
                ) as pool:
                    processed_batches = 0
                    streaming = enable_certstream or use_ct_logs
                    use_continuous = hasattr(pool, "crawl_one")

                    if use_continuous:
                        async def _crawl_worker() -> None:
                            while True:
                                rec = await live_queue.get()
                                if rec is None:
                                    live_queue.task_done()
                                    break
                                try:
                                    if stop_event.is_set():
                                        in_flight.discard(rec.canonical_key)
                                    else:
                                        outcome = await pool.crawl_one(rec)
                                        await _handle_crawl_outcome(outcome)
                                finally:
                                    live_queue.task_done()

                        crawl_workers = [
                            asyncio.create_task(_crawl_worker())
                            for _ in range(settings.crawl_concurrency)
                        ]

                    while not stop_event.is_set():
                        if max_batches is not None and processed_batches >= max_batches:
                            break
                        queued: list[UrlRecord] = store.claim_queued_batch(
                            limit=batch_size,
                            exclude_keys=in_flight,
                        )
                        if not queued:
                            if streaming and not stop_event.is_set():
                                tracker.set_stage("waiting for CT discovery")
                                await asyncio.sleep(0.5)
                                continue
                            if use_continuous:
                                await live_queue.join()
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

                        tracker.set_stage(f"DNS/TCP pre-filter ({len(queued)})")
                        live_targets, dead = await prefilter_live_targets(
                            queued,
                            doh_enabled=settings.doh_enabled,
                        )
                        tracker.inc("dns_checked", len(queued))
                        tracker.inc("dns_passed", len(live_targets))
                        tracker.inc("dead", len(dead))

                        for dead_key, reason in dead:
                            store.mark_dead(dead_key)
                            in_flight.discard(dead_key)
                            logger.debug("Pre-filter dropped %s (%s)", dead_key, reason)

                        if live_targets:
                            if use_continuous:
                                tracker.set_stage(
                                    f"crawling ({settings.crawl_concurrency} workers, buf={live_queue.qsize()})"
                                )
                                for rec in live_targets:
                                    if stop_event.is_set():
                                        in_flight.discard(rec.canonical_key)
                                    else:
                                        await live_queue.put(rec)
                            else:
                                tracker.set_stage(f"crawling ({len(live_targets)} live)")
                                outcomes = await pool.crawl_batch(live_targets)
                                for outcome in outcomes:
                                    await _handle_crawl_outcome(outcome)

                        tracker.snapshot(ended=False)
                        if live is not None:
                            live.update(_render_ui())

                    if use_continuous and not stop_event.is_set():
                        await live_queue.join()
        finally:
            stop_event.set()
            for _ in crawl_workers:
                await live_queue.put(None)
            if crawl_workers:
                await asyncio.gather(*crawl_workers, return_exceptions=True)
            if ui_task is not None:
                ui_task.cancel()
                await asyncio.gather(ui_task, return_exceptions=True)
            if timer_task is not None:
                timer_task.cancel()
            for task in discovery_tasks:
                task.cancel()
            if discovery_tasks:
                await asyncio.gather(*discovery_tasks, return_exceptions=True)
            await classify_queue.put(None)
            await clf_task
            tracker.set_stage("finished")
            final_snap = tracker.snapshot(ended=True)
            logger.info("Run %s complete: %s", run_id, final_snap["counts"])
            if file_handler is not None:
                root_logger.removeHandler(file_handler)
                file_handler.close()
                for h in saved_handlers:
                    root_logger.addHandler(h)

    return dict(tracker.counts)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run domain-sieve pipeline")
    parser.add_argument("--classifier", choices=["nli", "jev"], default=None)
    parser.add_argument("--doh", action="store_true", help="Enable Cloudflare DoH 1.1.1.1")
    parser.add_argument("--certstream", action="store_true", help="Consume CertStream WebSocket")
    parser.add_argument(
        "--ct-logs",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Poll public RFC 6962 CT logs directly (auto-enabled when no seeds are passed)",
    )
    parser.add_argument("--dnstwist", action="store_true", help="Generate dnstwist variants on hits")
    parser.add_argument("--seed-file", type=Path, action="append", default=[])
    parser.add_argument("--nrd-file", type=Path, action="append", default=[])
    parser.add_argument("--crtsh", action="append", default=[], help="Historical crt.sh keyword")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-batches", type=int, default=None)
    parser.add_argument("--duration-seconds", type=float, default=None)
    parser.add_argument(
        "--ui",
        action=argparse.BooleanOptionalAction,
        default=sys.stdout.isatty(),
        help="Show interactive Rich live terminal dashboard (default: auto on TTY)",
    )
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
            enable_ct_logs=args.ct_logs,
            enable_dnstwist=args.dnstwist,
            max_batches=args.max_batches,
            batch_size=args.batch_size,
            duration_seconds=args.duration_seconds,
            live_ui=args.ui,
        )
    )
    print(f"Pipeline finished: {counts}")


if __name__ == "__main__":
    main()
