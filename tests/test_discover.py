"""Tests for discovery pre-sieve, ring-link filtering, dnstwist, and source_cursors."""

from __future__ import annotations

import asyncio
import base64
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from sieve.discover import (
    extract_domains_from_ct_entry,
    extract_ring_candidates,
    generate_dnstwist_candidates,
    ingest_nrd_file,
    is_suspect_domain,
    parse_certstream_domains,
    poll_ct_logs,
)
from sieve.store import SieveStore


def test_domain_pre_sieve_filtering() -> None:
    # Indonesian & global suspect domains across categories
    assert is_suspect_domain("slotgacor88.xyz") is True
    assert is_suspect_domain("pinjol-cepat.com") is True
    assert is_suspect_domain("pelangi77.org") is True
    assert is_suspect_domain("bokep-indo.site") is True
    assert is_suspect_domain("sportsbook-live.com") is True
    assert is_suspect_domain("payday-cashadvance.net") is True
    assert is_suspect_domain("brimo-verifikasi.com") is True
    assert is_suspect_domain("bri-akun.com") is True
    assert is_suspect_domain("bo-togel.com") is True
    assert is_suspect_domain("apuestas-mx.com") is True
    assert is_suspect_domain("kazino-online.org") is True
    assert is_suspect_domain("nhacai-uy-tin.com") is True

    # Benign platforms, gov/edu, and short-token lookalikes must not trigger false positives
    assert is_suspect_domain("komdigi.go.id") is False
    assert is_suspect_domain("ui.ac.id") is False
    assert is_suspect_domain("docs.github.com") is False
    assert is_suspect_domain("tokobudi.id") is False
    assert is_suspect_domain("kompas.com") is False
    assert is_suspect_domain("britishmuseum.org") is False
    assert is_suspect_domain("about-window.org") is False
    assert is_suspect_domain("carpool.org") is False
    assert is_suspect_domain("abc123.r2.cloudflarestorage.com") is False
    assert is_suspect_domain("ww17.ar15.bce-na.com") is False
    assert is_suspect_domain("z95.w.api.fabric.microsoft-int.com") is False
    assert is_suspect_domain("webmail.hyundaicanada.com") is False


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


def _build_test_cert_der(dns_names: list[str]) -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_names[0])])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in dns_names]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


def test_ct_log_entry_parser_and_poller(tmp_path: Path) -> None:
    der = _build_test_cert_der(["slotgacor99.xyz", "www.google.com"])
    leaf_input = b"\x00\x00" + (b"\x00" * 8) + struct.pack("!H", 0) + len(der).to_bytes(3, "big") + der
    entry = {"leaf_input": base64.b64encode(leaf_input).decode("ascii"), "extra_data": ""}

    parsed = extract_domains_from_ct_entry(entry)
    assert parsed == ["https://slotgacor99.xyz"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ct/v1/get-sth"):
            return httpx.Response(200, json={"tree_size": 1})
        if request.url.path.endswith("/ct/v1/get-entries"):
            return httpx.Response(200, json={"entries": [entry]})
        return httpx.Response(404)

    async def _run() -> int:
        stop_event = asyncio.Event()
        with SieveStore(tmp_path / "sieve.db") as store:
            def _on_batch(scanned: int, ins: int, skip: int, _url: str) -> None:
                assert scanned == 1
                assert ins == 1
                stop_event.set()

            return await poll_ct_logs(
                store,
                stop_event=stop_event,
                log_urls=["https://ct.example.test/log/"],
                backfill_entries=10,
                on_batch=_on_batch,
                transport=httpx.MockTransport(handler),
            )

    enqueued = asyncio.run(_run())
    assert enqueued == 1
