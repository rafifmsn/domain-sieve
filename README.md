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

This repository currently contains only planning documentation, competition specifications, and [repo-analysis.html](repo-analysis.html); application source code has not been scaffolded yet.
Follow [docs/implementation-plan.md](docs/implementation-plan.md) starting at Phase 0 to initialize the Python environment, build the canonicalization/store core, and run the 50-100 page classifier A/B benchmark before starting full collection runs.

## Planned Development & Commands

| Task | Planned Command | Notes |
| :--- | :--- | :--- |
| Setup | `uv sync` | Installs `crawl4ai`, `torch`, `transformers`, `httpx`, `pytest` |
| Unit & Boundary Tests | `uv run pytest tests/` | Covers URL canonicalization, pre-sieve, store dedup, sharded storage |
| Classifier A/B Eval | `uv run python -m sieve.eval` | Benchmarks `bge-m3-zeroshot-v2.0` vs `jev-1.14.0` on labeled set |
| Pipeline Run | `uv run python -m sieve.run` | Runs discovery, crawl pool, classification, and metrics logging |
| Batch Export | `uv run python -m sieve.export` | Writes 20-URL `.txt` submission batches and summary CSV |
| Post-Export Enrich | `uv run python -m sieve.enrich` | Out-of-band DeepSeek Uncensored CSV annotation and write-up notes |
