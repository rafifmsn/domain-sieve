# domain-sieve

![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![PyTorch CUDA](https://img.shields.io/badge/pytorch-CUDA%20FP16-ee4c2c)
![Crawl4AI](https://img.shields.io/badge/renderer-crawl4ai-0b6e65)
![License MIT](https://img.shields.io/badge/license-MIT-slate)

`domain-sieve` is a single-node illegal-website discovery, rendering, and classification pipeline built for the Hackathon Berantas Web Ilegal di Indo (IMPHNEN, Dec 5-10, 2026).
It runs locally on a developer laptop (Fedora 45, Ryzen 5 6600H, 14.9 GiB RAM, RTX 3050 6 GB VRAM) to ingest live Certificate Transparency (CT) logs, filter active domains via async DNS/TCP checks, render pages through a shared Chromium pool (`crawl4ai`), classify multilingual illegal sites (`judol`, `pinjol`, `phishing_scam`, `adult_ilegal`, `other_ilegal`) with local FP16 zero-shot NLI inference (`MoritzLaurer/bge-m3-zeroshot-v2.0`), and export deduplicated 20-URL submission batches with sharded evidence artifacts.

## Motivation

Server-scale OSINT and threat platforms evaluated in [repo-analysis.html](repo-analysis.html) rely on heavy multi-container stacks (Elasticsearch, Neo4j, Redis, RabbitMQ, MinIO) that exhaust laptop memory and omit competition-specific URL canonicalization, Indonesian slang normalization, and 20-link batch export workflows.
`domain-sieve` replaces distributed broker overhead, multimodal VLM/OCR pipelines, and external CT daemons with one self-contained Python pipeline optimized for O(1) cross-run deduplication, continuous worker concurrency, and verifiable per-run metrics.

## Architecture Overview

```text
[1. Discovery] ---------> [2. Canon & Store] -----> [3. Async Gate & Crawl] ---> [4. Classifier & Store] ---> [5. Export & Enrich]
  - Direct RFC 6962 CT      - Exact hackathon         - Parallel DNS + TCP         - Micro-batched NLI          - 20-URL .txt batches
    logs (nimbus2026/27)      canonical key             443/80 liveness            (RTX 3050 FP16)            - candidates.csv
  - Cross-domain ring       - SQLite WAL +            - 14 crawl_one workers     - Sharded JSON artifacts     - Optional DeepSeek
    links + dnstwist          O(1) seen_keys set        (assets aborted)         - Rich live dashboard          Uncensored notes
```

![Evaluation and Throughput Summary](docs/assets/eval_benchmark.png)

Details on module boundaries, state transitions, and architectural trade-offs live in [docs/architecture.md](docs/architecture.md), [docs/decisions.md](docs/decisions.md), and [docs/hackathon-rules.md](docs/hackathon-rules.md).

## Quickstart

```sh
uv sync
uv run playwright install chromium
cp .env.example .env

# Run live discovery, crawling, GPU classification, and interactive Rich dashboard
uv run python -m sieve.run

# Export unexported high-confidence hits into 20-URL .txt submission batches
uv run python -m sieve.export
```

## Development & Commands

| Task | Command | Notes |
| :--- | :--- | :--- |
| Install | `uv sync && uv run playwright install chromium` | Installs Python dependencies, CUDA PyTorch, and headless Chromium |
| Run Pipeline | `uv run python -m sieve.run` | Auto-polls Cloudflare CT logs with live Rich terminal UI (`--doh`, `--dnstwist`, `--seed-file`, `--nrd-file`, `--no-ui`) |
| Test Suite | `uv run pytest tests/ -v` | Runs 48 unit and integration tests covering canonicalization, store dedup, pre-filter, discovery, export, and runner |
| Classifier Eval | `uv run python -m sieve.eval --backends nli` | Benchmarks local `bge-m3-zeroshot-v2.0` (and optional `jev` with `TYPESAFE_API_KEY`) on `data/eval/benchmark_set.jsonl` |
| Batch Export | `uv run python -m sieve.export --min-confidence 0.70` | Writes max-20 URL `.txt` files and `candidates.csv` under `data/exports/<YYYY-MM-DD>/` |
| CSV Enrichment | `uv run python -m sieve.enrich --input data/exports/<YYYY-MM-DD>/candidates.csv` | Appends 140-char Indonesian notes (`note_140_id`) and `mechanism_tag` using `DEEPSEEK_API_KEY` |
