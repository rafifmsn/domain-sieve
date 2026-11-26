"""Out-of-band DeepSeek Uncensored enricher for exported candidates.csv."""

from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from sieve.config import Settings
from sieve.store import now_wib_iso

logger = logging.getLogger(__name__)
MAX_NOTE_CHARS = 140


def _truncate_note(text: str, max_chars: int = MAX_NOTE_CHARS) -> str:
    clean = " ".join(text.strip().split())
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 3].rstrip() + "..."


def enrich_candidates_csv(
    *,
    candidates_csv: Path,
    scraped_root: Path,
    output_csv: Path,
    audit_log_path: Path,
    base_url: str,
    api_key: str,
    model: str = "deepseek-uncensored",
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Enrich exported candidates with 140-char Indonesian notes and mechanism tags."""
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY is required for sieve.enrich")

    rows = list(csv.DictReader(candidates_csv.open("r", encoding="utf-8")))
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    audit_log_path.parent.mkdir(parents=True, exist_ok=True)

    client = httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=30.0,
        transport=transport,
    )

    enriched_rows: list[dict[str, Any]] = []
    try:
        for row in rows:
            snippet = ""
            artifact_rel = row.get("artifact_path", "")
            if artifact_rel:
                artifact_file = scraped_root / artifact_rel
                if artifact_file.is_file():
                    artifact_data = json.loads(artifact_file.read_text(encoding="utf-8"))
                    snippet = f"{artifact_data.get('title', '')}\n{artifact_data.get('markdown', '')}"[:1200]

            prompt = (
                "Berikan ringkasan bukti maksimal 140 karakter dalam Bahasa Indonesia untuk laporan pemblokiran situs ilegal, "
                "serta tag mekanisme singkat (contoh: slot_gacor, qris_depo, pinjol_ilegal, phishing_login).\n"
                f"URL: {row.get('submit_url')}\nKategori: {row.get('category')}\nCuplikan:\n{snippet}\n"
                'Balas JSON: {"note_id": "...", "mechanism_tag": "..."}'
            )
            resp = client.post(
                "/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "max_tokens": 180,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            raw_content = str(body["choices"][0]["message"]["content"]).strip()

            note_id = ""
            mechanism_tag = row.get("category", "")
            try:
                parsed = json.loads(raw_content)
                note_id = _truncate_note(str(parsed.get("note_id", "")))
                mechanism_tag = str(parsed.get("mechanism_tag", mechanism_tag)).strip()
            except json.JSONDecodeError:
                note_id = _truncate_note(raw_content)

            enriched_rows.append(
                {
                    **row,
                    "note_140_id": note_id,
                    "mechanism_tag": mechanism_tag,
                }
            )
            with audit_log_path.open("a", encoding="utf-8") as log_fh:
                log_fh.write(
                    json.dumps(
                        {
                            "timestamp": now_wib_iso(),
                            "canonical_key": row.get("canonical_key"),
                            "model": model,
                            "note_140_id": note_id,
                            "mechanism_tag": mechanism_tag,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    finally:
        client.close()

    fieldnames = [
        "canonical_key",
        "submit_url",
        "category",
        "is_id_targeted",
        "confidence",
        "classifier_id",
        "source",
        "export_batch",
        "artifact_path",
        "note_140_id",
        "mechanism_tag",
    ]
    with output_csv.open("w", newline="", encoding="utf-8") as out_fh:
        writer = csv.DictWriter(out_fh, fieldnames=fieldnames)
        writer.writeheader()
        for erow in enriched_rows:
            writer.writerow({k: erow.get(k, "") for k in fieldnames})

    return len(enriched_rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Enrich candidates.csv using DeepSeek Uncensored")
    parser.add_argument("--input", type=Path, required=True, help="Path to candidates.csv")
    parser.add_argument("--output", type=Path, default=None, help="Path to enriched_candidates.csv")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    out_csv = args.output or (args.input.parent / "enriched_candidates.csv")
    audit_log = args.input.parent / "enrichment_audit.jsonl"
    count = enrich_candidates_csv(
        candidates_csv=args.input,
        scraped_root=settings.scraped_dir,
        output_csv=out_csv,
        audit_log_path=audit_log,
        base_url=settings.deepseek_base_url,
        api_key=settings.deepseek_api_key,
        model=settings.deepseek_model,
    )
    print(f"Enriched {count} candidate(s) -> {out_csv}")


if __name__ == "__main__":
    main()
