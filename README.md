# domain-sieve

Lightweight, single-node illegal-website discovery, rendering, and classification pipeline built for the Hackathon Berantas Web Ilegal di Indo (IMPHNEN, Dec 5-10, 2026).
It runs natively on a developer laptop (Fedora 45, Ryzen 5 6600H, 14.9 GiB RAM, RTX 3050 6 GB VRAM) to discover active illegal domains (online gambling, illegal lending, digital fraud/phishing, and illegal adult content), extract clean Markdown via `crawl4ai`, classify targets with calibrated zero-shot inference, and export deduplicated 20-URL submission batches with full metrics and evidence artifacts.

## Motivation

Existing open-source threat pipelines evaluated in [repo-analysis.html](repo-analysis.html) (IntelOwl, Lookyloo, AIL Framework, Amass, Firecrawl) target multi-node server infrastructure with heavy container stacks (Elasticsearch, Neo4j, Redis, RabbitMQ, MinIO) that overwhelm a 15 GiB laptop and leave hackathon-specific canonicalization, Indonesian/multilingual target filtering, and batch submission workflows unbuilt.
`domain-sieve` strips out multi-container bloat, multimodal VLM/OCR overhead, and redundant fallback chains in favor of one lean pipeline optimized for precision, O(1) cross-run deduplication, measurable A/B model validation, and zero wasted crawl slots.

## Architecture Overview

```text
[1. Discovery] --------> [2. Canon & Store] ----> [3. Crawl Pool] ------> [4. Single Classifier] ----> [5. Export & Enrich]
  - CertStream SAN         - Exact hackathon        - Async DNS/TCP         - A/B eval winner:           - 20-URL .txt batches
  - Ring/mirror crawl        canonical key            liveness check          bge-m3-zeroshot-v2.0         - Sharded evidence tree
  - dnstwist variants      - SQLite WAL +           - crawl4ai (10-14         (RTX 3050 FP16) OR           - Run metrics JSONL
  - NRD / crt.sh seeds       O(1) RAM set             tabs, no assets)        jev-1.14.0 API             - Offline DeepSeek CSV
```

## Documentation Index

Read these documents in order before writing implementation code:

1. [docs/hackathon-rules.md](docs/hackathon-rules.md): Official competition rules, timeline, URL validation constraints, exact `Duplikat` canonicalization spec, and submission limits.
2. [docs/architecture.md](docs/architecture.md): Sequence diagram, module boundaries, SQLite schema, sharded artifact directory layout, metrics/evidence logging, and laptop resource budget.
3. [docs/decisions.md](docs/decisions.md): Architectural trade-offs, dropped components, DNS strategy, discovery strategy, and classifier selection rationale.
4. [docs/implementation-plan.md](docs/implementation-plan.md): Phased checklist, A/B benchmark protocol, required test coverage, and acceptance gates.
5. [docs/conversation-transcript.md](docs/conversation-transcript.md): Complete verbatim planning discussion between the project owner and the planning agent.
6. [repo-analysis.html](repo-analysis.html): Prior landscape survey across 18 open-source discovery, crawling, and threat-classification repositories.

## Current Status & Quickstart

The core `sieve/` pipeline, `data/eval/benchmark_set.jsonl` (60 samples), and `tests/` suite are implemented and verified with `uv sync`, Playwright Chromium, and PyTorch CUDA (`RTX 3050`).
Copy `.env.example` to `.env`, configure optional API keys (`TYPESAFE_API_KEY` for Jev A/B comparison, `DEEPSEEK_API_KEY` for post-export enrichment), and run `uv run pytest tests/` or `uv run python -m sieve.eval --backends nli` before starting collection runs.

## Development & Commands

| Task | Command | Notes |
| :--- | :--- | :--- |
| Setup | `uv sync && uv run playwright install chromium` | Installs `crawl4ai`, `torch`, `transformers`, `httpx`, `pytest`, and Chromium |
| Unit & Boundary Tests | `uv run pytest tests/` | Covers URL canonicalization, pre-sieve, store dedup, sharded storage, export |
| Classifier A/B Eval | `uv run python -m sieve.eval --backends nli` | Add `jev` to `--backends` once `TYPESAFE_API_KEY` is set |
| Pipeline Run | `uv run python -m sieve.run --seed-file seeds.txt` | Supports `--certstream`, `--nrd-file`, `--crtsh`, `--dnstwist`, `--doh` |
| Batch Export | `uv run python -m sieve.export` | Writes 20-URL `.txt` submission batches and `candidates.csv` |
| Post-Export Enrich | `uv run python -m sieve.enrich --input data/exports/<YYYY-MM-DD>/candidates.csv` | Out-of-band DeepSeek Uncensored CSV annotation and audit log |
