# Architectural Decisions

Record of production design choices and rejected alternatives for `domain-sieve`.

## Scope & Language Handling

- **Multilingual illegal scope with Indonesian priority tagging:**
  Illegal websites routinely rotate across `.com`, `.xyz`, `.vip`, `.site`, and `.bet` domains while mixing Indonesian, English, and global copy.
  `NliClassifier` (`MoritzLaurer/bge-m3-zeroshot-v2.0`) classifies pages across 100+ languages while `detect_indonesian_targeting` sets `is_id_targeted` (checking `.id` TLDs, Indonesian payment rails such as QRIS/GoPay/OVO/DANA/BCA/BRI/Mandiri, `+62` contacts, and Indonesian copy) so `sieve.export` prioritizes Indonesian-targeted sites first.
- **Targeted slang normalization over bulk dictionary rewriting:**
  Evaluating `_SLANG_REPLACEMENTS` on `data/eval/benchmark_set.jsonl` showed that rewriting standard dictionary words or injecting English tokens into Indonesian text degrades NLI `Macro F1`.
  Normalization is therefore restricted to obfuscated leetspeak (`jud0l`, `g4c0r`, `b0kep`) and local jargon (`rungkad`, `nawala`, `galbay`, `open bo`, `vcs`), using pure Indonesian expansions.

## DNS Resolution Policy

- **Default to local DNS with an optional `--doh` toggle:**
  Using the host's local resolver in `prefilter_live_targets` drops already-sinkholed or NXDOMAIN domains before opening Chromium tabs, ensuring captured hits are live under standard local resolution.
  Passing `--doh` switches both `resolve_host_ips` (via `https://1.1.1.1/dns-query`) and Chromium resolver args to Cloudflare DoH without maintaining dual-query fallback logic.

## Discovery, Deduplication & Backpressure

- **Direct RFC 6962 CT polling instead of external CertStream daemons or flaky `crt.sh` scans:**
  Live probing showed `wss://certstream.calidog.io/` unreachable and `crt.sh` wildcard queries timing out (`502` / read timeout), whereas Cloudflare CT logs (`nimbus2026`, `nimbus2027`) respond reliably with low latency.
  `poll_ct_logs` parses X.509 SANs directly in Python, eliminating the need to build or supervise `certstream-server-go` while keeping `consume_certstream` available as an optional flag.
- **Pause-and-snap backpressure over SQLite queue deletion:**
  When `store.count_queued()` reaches `3,000`, `poll_ct_logs` pauses polling and snaps its CT cursor forward to `tree_size - 1500` upon resuming rather than deleting queued rows.
  This preserves `seen_keys` deduplication history, avoids SQLite WAL delete churn, and keeps crawling focused on newly issued certificates.
- **`O(1)` `canonical_key` state instead of timestamp-only cursors:**
  `canonical_key` serves as the SQLite `PRIMARY KEY` and is preloaded into an in-memory `set[str]` (`seen_keys`), dropping cross-source duplicates and rapid restarts in `O(1)` time while `status = 'queued'` supports clean crash resumption.
- **Skip generic public malware/phish dumps (`URLhaus`, `OpenPhish`, `PhishTank`):**
  Public bulk feeds skew heavily toward stale global compromised CMS paths and high duplicate overlap.

## Fetching & Concurrency

- **Single `crawl4ai` renderer preceded by parallel async DNS/TCP liveness checks:**
  Modern gambling mirrors and AMP cloakers frequently use client-side JS redirects, SPA frameworks, or browser checks where raw `httpx` fetches fail or return empty shells.
  Maintaining one `crawl4ai` pool (`--disable-gpu`, `image`/`media`/`font`/`stylesheet` route blocking, `max_pages_before_recycle=300`) eliminates dual-extractor drift, while concurrent DNS + parallel `443`/`80` TCP probing (`timeout_s=1.5`) filters dead hosts cheaply beforehand.
- **Continuous `crawl_one` workers over `arun_many` batch barriers:**
  Batch `arun_many` stalls all browser tabs until the slowest `page_timeout` in a batch expires.
  Switching `sieve/run.py` to `14` independent `_crawl_worker` coroutines calling `CrawlPool.crawl_one` (`page_timeout_ms=12000`, `70%` system RAM gate) raised measured live throughput from `6.3` to `220.9` classified pages/min.
- **Filter parked/error stubs while keeping geo-blocked gambling pages:**
  `is_dead_or_parked_page` drops `>= 400` HTTP errors, empty shells, `"Redirecting..."` stubs, and domain-for-sale pages, while preserving regional geo-block notices (e.g. `.bet`/`.casino` sites) that still identify active illegal operators.

## Classification & Calibration

- **Single local NLI classifier (`bge-m3-zeroshot-v2.0`) over multi-stage cascades or paid APIs:**
  We rejected multimodal VLM/OCR pipelines, local XGBoost Gate B cascades, and hot-path generative LLMs to keep latency low and failure modes minimal.
  On `data/eval/benchmark_set.jsonl` (60 samples), local `MoritzLaurer/bge-m3-zeroshot-v2.0` FP16 (`log_odds` + `softmax` with intent-aligned hypotheses) achieved `Precision = 0.8750`, `Recall = 0.9333`, `Binary F1 = 0.9032`, `Macro F1 = 0.7378`, `p50 = 163.38ms`, and `$0.00` marginal cost.
  `--classifier nli` is locked as the default while `JevClassifier` (`jev-1.14.0`) remains available via `--classifier jev`.
- **Out-of-band LLM enrichment:**
  `sieve/enrich.py` isolates generative LLM annotation (`note_140_id`, `mechanism_tag`) strictly to post-export CSVs so API latency and uncalibrated outputs never affect the crawl/classification hot path.

## Storage, Observability & Export

- **Time- and category-sharded JSON artifacts + Rich live telemetry:**
  Partitioning scraped outputs under `data/scraped/<YYYY-MM-DD>/<category>/<hash[:2]>/<canonical_hash>.json` keeps directory indexes fast, while `metrics.jsonl`, `errors.jsonl`, and the `rich.live.Live` terminal UI expose real-time stage liveness and throughput.
- **Clean 20-URL batch export without automated form submission:**
  `sieve/export.py` emits validated `batch_XXXX.txt` files (max 20 URLs each) and `candidates.csv` without coupling collection to downstream submission endpoints.
