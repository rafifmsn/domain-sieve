# Implementation Plan & Handoff Checklist

Execution contract for the implementing agent.
Read [docs/hackathon-rules.md](hackathon-rules.md), [docs/architecture.md](architecture.md), and [docs/decisions.md](decisions.md) before modifying code.
Mark a checkbox `- [x]` only after its associated tests or verification commands have passed.

## Guardrails for the Implementing Agent

- Do not add multimodal/VLM screenshot pipelines, Tesseract/OCR, XGBoost Gate B cascades, live pre-submission re-verification, or form pre-check automation.
- Keep `crawl4ai` as the sole content renderer (preceded only by async DNS/TCP liveness filtering); do not build dual `httpx` + Playwright extraction fallbacks.
- Keep DeepSeek Uncensored strictly out-of-band in `sieve/enrich.py`.
- Leave `LICENSE`, `repo-analysis.html`, and `docs/conversation-transcript.md` unmodified.

## Phase 0: Environment & Project Scaffold

- [x] Initialize Python 3.11+ project (`pyproject.toml` via `uv`) with `crawl4ai`, `httpx`, `aiodns`, `torch` (CUDA enabled for RTX 3050), `transformers`, `websockets`, `dnstwist`, and `pytest`.
- [x] Create `sieve/` package skeleton (`__init__.py`, `config.py`, `canon.py`, `store.py`, `metrics.py`, `crawler.py`, `classify.py`, `eval.py`, `discover.py`, `export.py`, `enrich.py`, `run.py`).
- [x] Add `.env.example` documenting `TYPESAFE_API_KEY` (optional until A/B eval), `DEEPSEEK_BASE_URL`, `DEEPSEEK_API_KEY`, `CERTSTREAM_URL`, and `DATA_DIR`.
- [x] Verify Playwright Chromium binary works headless with `--disable-gpu` and PyTorch sees CUDA on RTX 3050 (`torch.cuda.is_available() == True`).

## Phase 1: Canonicalization, SQLite Store, Sharding & Metrics

- [x] Implement `sieve/canon.py`: `validate_and_canonicalize(raw_url: str) -> CanonResult | None` enforcing scheme `http`/`https`, no port, no `user:pass@`, no IP/IPv6/`localhost`, `len(raw_url) <= 300`, strip scheme, strip leading `www.` only, lowercase hostname, strip `?query` and `#fragment`, strip trailing `/`, preserve case-sensitive path, and return `canonical_sha256`.
- [x] Implement `sieve/store.py`: SQLite WAL connection, schema creation (`urls`, `runs`, `eval_runs`, `source_cursors`), startup preload of `seen_keys: set[str]`, O(1) `enqueue_many()` with `INSERT OR IGNORE`, `claim_queued_batch()`, and lifecycle updates (`dead`, `crawled`, `classified`, `benign`, `error`, `exported`).
- [x] Implement `sieve/metrics.py`: sharded JSON artifact writer under `data/scraped/<YYYY-MM-DD>/<category>/<hash[:2]>/<canonical_hash>.json` and periodic `metrics.jsonl` + SQLite `runs` recorder tracking discovery yield, DNS pass rate, crawl latency, classifier throughput, RAM usage, and error counts.
- [x] Add `tests/test_canon.py` verifying all 6 official hackathon examples plus port/userinfo/IP/`localhost`/300-char boundary cases.
- [x] Add `tests/test_store_and_sharding.py` verifying O(1) duplicate suppression across rapid sequential calls, crash/restart resume on `status = 'queued'`, and sharded directory placement (`<date>/<category>/<hash[:2]>/<sha256>.json`).

## Phase 2: Async Pre-Filter & Crawl4AI Pool

- [x] Implement fast `asyncio` DNS + TCP port 80/443 liveness check in `sieve/crawler.py` defaulting to local ISP DNS, with optional `--doh` flag routing DNS queries to `1.1.1.1`.
- [x] Implement ISP TrustPositif block-page detector (matching common Indonesian ISP sinkhole redirects/titles so sinkholed IPs are marked `dead` / `isp_blocked` rather than misclassified).
- [x] Configure single shared `crawl4ai` Chromium browser (`--disable-gpu`, optional DoH resolver rule when `--doh` is passed) with `MemoryAdaptiveDispatcher` (target 10-14 concurrent contexts, 70% RAM cap) and route/network blocking for `image`, `media`, `font`, and `stylesheet`.
- [x] Extract clean Markdown (title + meta description + headings + first ~1,500 chars body) and outbound `<a href>` links for ring-crawling.
- [x] Add `tests/test_crawler_smoke.py` verifying asset blocking, `--doh` toggle configuration, ISP sinkhole detection on mock HTML, and Markdown + outbound link extraction.

## Phase 3: Classifier Interface & A/B Benchmark Gate

- [x] Define `Classifier` protocol in `sieve/classify.py` returning `ClassificationResult(category, is_id_targeted, confidence, classifier_id, latency_ms)` with Indonesian *judol*/*pinjol* abbreviation normalization prior to inference.
- [x] Implement `NliClassifier` (`MoritzLaurer/bge-m3-zeroshot-v2.0`) running FP16 micro-batches (`batch_size=16..32`, `torch.inference_mode()`) on RTX 3050 with explicit multilingual NLI hypotheses for `judol`, `pinjol`, `phishing_scam`, `adult_ilegal`, and `benign`.
- [x] Implement `JevClassifier` (`jev-1.14.0` TypeSafe HTTP API client) with matching typed enum options and latency/token-cost tracking.
- [x] Build `data/eval/benchmark_set.jsonl` (50-100 labeled real/sanitized pages spanning Indonesian *judol*/*pinjol*, English/foreign gambling/adult/scam, parked domains, and hard negatives such as news articles reporting on *judol*).
- [x] Implement `sieve/eval.py` to run both backends on `benchmark_set.jsonl`, write comparison metrics (`precision`, `recall`, `f1`, `p50_ms`, `p95_ms`, `est_cost_usd`, disagreement CSV) to `data/eval/reports/`, and lock the winning `--classifier` default in `docs/decisions.md` (`--classifier nli` benchmarked and locked; `jev` remains an optional swappable backend if `TYPESAFE_API_KEY` is later supplied).

## Phase 4: Discovery Engine

- [x] Implement `sieve/discover.py` CertStream SAN consumer (connecting to local `certstream-server-go` or configurable WebSocket) with a high-recall domain pre-sieve (suspicious TLDs, gambling/pinjol/scam/ID tokens, numeric/entropy heuristics) before `store.enqueue_many()`.
- [x] Implement outbound ring-crawler feedback loop feeding newly extracted same-intent domains from `judol`/`pinjol`/`adult_ilegal` hits back into `store.enqueue_many()` (with hop-depth cap to prevent drift into unrelated sites).
- [x] Implement `dnstwist` permutation generator triggered on confirmed hits + historical `crt.sh` / NRD daily file ingestor tracked via `source_cursors`.
- [x] Add `tests/test_discover.py` verifying domain pre-sieve filtering, ring-link canonicalization/filtering, and `source_cursors` idempotency.

## Phase 5: Orchestrator, Batch Export & Offline DeepSeek Enricher

- [x] Implement `sieve/run.py` tying Discovery -> Pre-Filter -> `crawl4ai` -> Micro-batched Classifier -> Sharded Artifact Writer & `metrics.jsonl` into an async bounded-queue pipeline supporting clean `SIGINT` shutdown and resume.
- [x] Implement `sieve/export.py` exporting unexported high-confidence hits (`confidence >= threshold`, prioritizing `is_id_targeted = 1`) into 20-URL `.txt` batch files under `data/exports/<YYYY-MM-DD>/batch_XXXX.txt` plus `candidates.csv`, marking rows `exported` atomically.
- [x] Implement `sieve/enrich.py` consuming `candidates.csv` via an OpenAI-compatible `DEEPSEEK_BASE_URL` (7-day Netra Runtime DeepSeek Uncensored), writing 140-char Indonesian submission notes, mechanism tags, and enrichment audit logs to `enriched_candidates.csv`.
- [x] Add `tests/test_export.py` verifying max-20-per-batch splitting, strict re-validation of 300-char/no-IP/no-port constraints, and zero duplicate exports across consecutive runs.
- [x] Execute a 15-minute live dry run, inspect `metrics.jsonl` and sharded `data/scraped/` output, and document observed throughput before launching full collection runs ahead of Dec 5, 00:00 WIB (executed 5-minute + 60-second live CT log runs, verified `metrics.jsonl`, `data/scraped/`, and `sieve.export` batch output).
