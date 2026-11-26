"""Tests for discovery pre-sieve, ring-link filtering, dnstwist, and source_cursors."""

from __future__ import annotations

from pathlib import Path

from sieve.discover import (
    extract_ring_candidates,
    generate_dnstwist_candidates,
    ingest_nrd_file,
    is_suspect_domain,
    parse_certstream_domains,
)
from sieve.store import SieveStore


def test_domain_pre_sieve_filtering() -> None:
    assert is_suspect_domain("slotgacor88.xyz") is True
    assert is_suspect_domain("pinjol-cepat.com") is True
    assert is_suspect_domain("pelangi77.org") is True
    assert is_suspect_domain("bokep-indo.site") is True

    assert is_suspect_domain("komdigi.go.id") is False
    assert is_suspect_domain("ui.ac.id") is False
    assert is_suspect_domain("docs.github.com") is False
    assert is_suspect_domain("tokobudi.id") is False


def test_ring_link_canonicalization_and_hop_cap() -> None:
    links = [
        "https://www.MirrorSlot88.xyz/?ref=home",
        "https://mirrorslot88.xyz/#top",
        "https://instagram.com/slotpromo",
        "https://cdn.example.com/logo.png",
        "http://127.0.0.1/panel",
        "https://source-judol.com/",
    ]
    accepted = extract_ring_candidates(
        source_category="judol",
        source_host="source-judol.com",
        outbound_links=links,
        hop_depth=0,
        max_hop_depth=2,
    )
    assert accepted == ["https://www.mirrorslot88.xyz"]

    # Benign sources and exhausted hop depth yield no ring links
    assert (
        extract_ring_candidates(
            source_category="benign",
            source_host="news.id",
            outbound_links=links,
            hop_depth=0,
            max_hop_depth=2,
        )
        == []
    )
    assert (
        extract_ring_candidates(
            source_category="judol",
            source_host="source-judol.com",
            outbound_links=links,
            hop_depth=2,
            max_hop_depth=2,
        )
        == []
    )


def test_nrd_ingestion_cursor_idempotency(tmp_path: Path) -> None:
    nrd_file = tmp_path / "nrd_2026-11-26.txt"
    nrd_file.write_text(
        "\n".join(
            [
                "# daily NRD",
                "slotgacor99.xyz",
                "tokobudi.id",
                "danacair-pinjol.com",
            ]
        ),
        encoding="utf-8",
    )
    with SieveStore(tmp_path / "sieve.db") as store:
        ins1, _ = ingest_nrd_file(nrd_file, store, cursor_name="nrd_daily")
        assert ins1 == 2  # tokobudi.id dropped by pre-sieve

        # Second call on unchanged file is skipped via source_cursors
        ins2, skip2 = ingest_nrd_file(nrd_file, store, cursor_name="nrd_daily")
        assert ins2 == 0 and skip2 == 0


def test_parse_certstream_and_dnstwist() -> None:
    msg = {
        "message_type": "certificate_update",
        "data": {
            "leaf_cert": {
                "subject": {"CN": "*.gacor777.vip"},
                "all_domains": ["*.gacor777.vip", "google.com"],
            }
        },
    }
    assert parse_certstream_domains(msg) == ["https://gacor777.vip"]

    variants = generate_dnstwist_candidates("slot88.xyz", max_variants=10)
    assert len(variants) > 0
    assert all(v.startswith("https://") for v in variants)
