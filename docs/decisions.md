# Architectural Decisions

Concise record of locked architectural choices and rejected alternatives for `domain-sieve`.

## Scope & Language Handling

- **Multilingual illegal web scope (Indonesian + English + global languages):**
  Hackathon rules do not restrict entries to `.id` domains, and Indonesian *judol*/*pinjol* operators routinely use `.com`, `.xyz`, `.vip`, and `.site` domains while mixing Indonesian, English, and other UI copy.
  We use a multilingual classifier (`bge-m3-zeroshot-v2.0` or `jev-1.14.0`) to cover Indonesian, English, and foreign-language illegal sites while still recording an `is_id_targeted` signal (IDR, QRIS, `+62`, Indonesian copy) so highest-relevance Komdigi targets can be prioritized.
  Generic global adult/malware dumps remain deprioritized because of high `Duplikat` collision and `Ditolak` relevance risk.

## DNS Resolution Policy

- **Default to local ISP DNS with a single `--doh` toggle:**
  Under Indonesian ISP DNS, domains already blocked by Komdigi TrustPositif fail fast at DNS resolution (`ERR_NAME_NOT_RESOLVED` or resolve to ISP sinkholes).
  Defaulting to ISP DNS avoids wasting `crawl4ai` browser tabs on already-blocked sites and guarantees captured targets are live and reachable in Indonesia today.
  If broader recall on ISP-blocked mirrors is later desired, a single `--doh` flag switches the async resolver and Chromium DNS to Cloudflare DoH (`1.1.1.1`) without maintaining dual-query fallback logic.

## Discovery Sources & Deduplication

- **Combine forward CertStream SANs + ring-crawling with backward historical seeds:**
  CertStream SAN filtering catches brand-new domains minutes after TLS issuance, but misses sites behind shared CDN/wildcard certs, neutral domain names, and already-live domains whose certs were issued before an 8-hour run.
  We pair CertStream with outbound link extraction (ring-crawling from confirmed hits), `dnstwist` TLD/numeric permutations on known active brands, and historical `crt.sh`/NRD daily drops.
- **Skip public malware/phish dumps (URLhaus, OpenPhish, PhishTank):**
  Public feeds are heavily scraped by competing teams (high `Duplikat` rate) and skew toward global compromised WordPress paths rather than Indonesian-relevant illegal sites.
  With submission throttled to 20 links per 3 seconds, novelty and high precision matter more than raw bulk dump volume.
- **O(1) `canonical_key` state instead of timestamp-only cursors:**
  Because the pipeline runs in 8-hour batches rather than 24/7 and may be restarted seconds apart, wall-clock cursors cannot prevent cross-source or rapid-restart duplicates.
  Enforcing `canonical_key` as SQLite `PRIMARY KEY` plus a startup `set[str]` drops duplicates in O(1) nanoseconds, while row `status` lets paused runs resume cleanly from `WHERE status = 'queued'`.

## Fetching & Concurrency

- **Single `crawl4ai` extractor preceded by a fast async DNS/TCP liveness gate:**
  We reject dual `httpx` + Playwright content-extraction fallbacks to eliminate code bloat and maintenance drift.
  A lightweight `asyncio` DNS/TCP liveness check drops dead/NXDOMAIN domains in milliseconds, and `crawl4ai` renders all live candidates through one shared Chromium pool (10-14 contexts, `--disable-gpu`, blocking images, fonts, CSS, and media).
- **Drop live pre-submission re-verification:**
  Within a <10-day competition window, maintaining a separate re-verification pipeline adds complexity with minimal yield; panitia evaluates submitted links directly.

## Classification & A/B Benchmarking

- **Drop multimodal/screenshots, Tesseract/OCR, and XGBoost + generative-LLM cascades:**
  Image-only landing pages are rare relative to renderable text/DOM pages, and stacking local XGBoost -> generative JSON-schema LLM adds failure modes and latency.
- **Single-classifier architecture decided by an A/B eval (`bge-m3-zeroshot-v2.0` vs. `jev-1.14.0`):**
  Local `MoritzLaurer/bge-m3-zeroshot-v2.0` on the RTX 3050 6 GB GPU (FP16 micro-batches) has $0 marginal cost, no rate limits, and zero safety refusals, while hosted TypeSafe `jev-1.14.0` provides calibrated zero-shot API decisions with potential cost/rate-limit/ToS exposure.
  Rather than chaining them, we benchmark both on the same 50-100 page labeled eval set first and run only the winning backend in production.
  Baseline eval on the 60-sample `data/eval/benchmark_set.jsonl` locked `--classifier nli` (`MoritzLaurer/bge-m3-zeroshot-v2.0` FP16) as the default (`Precision=0.8750`, `Recall=0.9333`, `Binary F1=0.9032`, `Macro F1=0.7378`, `p50=163.38ms`, `$0.00` cost); `jev-1.14.0` can be benchmarked side-by-side once `TYPESAFE_API_KEY` is provided.
- **Keep DeepSeek Uncensored strictly out-of-band:**
  The 7-day Netra Runtime DeepSeek Uncensored access is slower and uncalibrated for hot-path classification, so it is isolated to `sieve/enrich.py` to annotate exported CSVs (140-char notes and Top-3 write-up summaries).

## Storage & Observability

- **Time- and category-sharded JSON artifacts + structured JSONL metrics:**
  Storing tens of thousands of scraped files in one directory slows filesystem indexing.
  Scraped payloads are partitioned under `data/scraped/<YYYY-MM-DD>/<category>/<hash[:2]>/<canonical_hash>.json`, indexed by SQLite, and paired with per-run `metrics.jsonl` snapshots and A/B eval reports for verifiable evidence.
