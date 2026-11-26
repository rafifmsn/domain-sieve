<p align="center">
  <img src="./domain-sieve-thumb.jpg" alt="Domain Sieve" width="100%" />
</p>

<p align="center">
  <a href="https://www.python.org/downloads/release/python-3120/">
    <img src="https://img.shields.io/badge/python-3.12-blue" alt="Python 3.12" />
  </a>
  <a href="https://pytorch.org/">
    <img src="https://img.shields.io/badge/pytorch-CUDA%20FP16-ee4c2c" alt="PyTorch CUDA" />
  </a>
  <a href="https://github.com/unclecode/crawl4ai">
    <img src="https://img.shields.io/badge/renderer-crawl4ai-0b6e65" alt="Crawl4AI" />
  </a>
  <a href="./LICENSE">
    <img src="https://img.shields.io/badge/license-MIT-slate" alt="License MIT" />
  </a>
</p>

<p align="center">
  A hardware-agnostic, single-node pipeline for automated discovery, browser rendering, and multilingual classification of illegal websites. It ingests live Certificate Transparency (CT) logs, filters live hosts via async DNS/TCP checks, renders targets through a shared headless Chromium pool (<code>crawl4ai</code>), classifies pages across <code>judol</code>, <code>pinjol</code>, <code>phishing_scam</code>, <code>adult_ilegal</code>, and <code>other_ilegal</code> using FP16 zero-shot NLI (<code>MoritzLaurer/bge-m3-zeroshot-v2.0</code>), and exports deduplicated 20-URL batches with sharded JSON artifacts.
</p>

## Motivation

Server-scale OSINT and threat platforms evaluated in [repo-analysis.html](repo-analysis.html) rely on heavy multi-container stacks (Elasticsearch, Neo4j, Redis, RabbitMQ, MinIO) that overtax single-machine memory and omit deterministic URL canonicalization, Indonesian slang normalization, and compact batch export workflows.
`domain-sieve` replaces distributed broker overhead, multimodal VLM/OCR pipelines, and external CT daemons with one self-contained Python pipeline optimized for O(1) cross-run deduplication, continuous worker concurrency, and verifiable per-run telemetry.

## Architecture Overview

```mermaid
flowchart LR
    D["1. Discovery (CT Logs, Ring, dnstwist)"] --> S["2. Canon and Store (SQLite WAL, O(1) Set)"]
    S --> P["3. DNS/TCP Gate and Crawl4AI (14 Workers)"]
    P --> M["4. Zero-Shot NLI (FP16) and Sharded JSON"]
    M --> E["5. 20-URL Batch Export and CSV Enricher"]
```

![Evaluation and Throughput Summary](docs/assets/eval_benchmark.png)

Benchmarks above were measured on a single CUDA-enabled machine (15 GiB RAM, 6 GB VRAM RTX 3050, 12-thread AMD Ryzen 5 6600H).
Details on module boundaries, state transitions, and architectural trade-offs live in [docs/architecture.md](docs/architecture.md) and [docs/decisions.md](docs/decisions.md).

## Quickstart

```sh
uv sync
uv run playwright install chromium
cp .env.example .env

# Run live discovery, crawling, GPU classification, and interactive Rich dashboard
uv run python -m sieve.run

# Export unexported high-confidence hits into 20-URL .txt batches
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
