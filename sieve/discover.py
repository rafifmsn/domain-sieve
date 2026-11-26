"""Discovery engine: CertStream SAN stream, ring spidering, dnstwist, and NRD/crt.sh seeds."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import httpx
import websockets

from sieve.canon import validate_and_canonicalize
from sieve.config import ILLEGAL_CATEGORIES
from sieve.store import SieveStore

logger = logging.getLogger(__name__)

SUSPICIOUS_TLDS = frozenset(
    {
        "xyz",
        "vip",
        "site",
        "top",
        "click",
        "icu",
        "live",
        "bet",
        "casino",
        "win",
        "club",
        "fun",
        "online",
        "store",
        "shop",
        "pro",
        "cfd",
        "sbs",
        "cyou",
        "buzz",
        "lat",
        "asia",
        "pw",
        "cc",
        "ws",
    }
)

BENIGN_ROOT_DOMAINS = frozenset(
    {
        "google.com",
        "googleapis.com",
        "gstatic.com",
        "microsoft.com",
        "apple.com",
        "icloud.com",
        "amazonaws.com",
        "cloudflare.com",
        "github.com",
        "githubusercontent.com",
        "facebook.com",
        "instagram.com",
        "youtube.com",
        "youtu.be",
        "tiktok.com",
        "twitter.com",
        "x.com",
        "whatsapp.com",
        "t.me",
        "telegram.org",
        "wikipedia.org",
        "linkedin.com",
        "mozilla.org",
        "w3.org",
    }
)

BENIGN_TLD_SUFFIXES = (".gov", ".gov.id", ".go.id", ".edu", ".ac.id", ".mil", ".sch.id")

_SUSPECT_DOMAIN_TOKENS = re.compile(
    r"(?:slot|gacor|maxwin|togel|toto|judi|judol|casino|poker|jackpot|scatter|"
    r"zeus|olympus|pragmatic|mahjong|spaceman|rtp|depo|cuan|hoki|"
    r"pinjol|pinjam|kredit|tunai|duit|cair|dana|"
    r"bokep|porn|xxx|jav|sex|adult|18plus|"
    r"login|verify|akun|resmi|claim|bonus|gift|amp|alternatif)",
    re.IGNORECASE,
)

_DIGIT_MIX_RE = re.compile(r"(?:[a-z]+\d{2,}|\d{2,}[a-z]+)", re.IGNORECASE)
_ASSET_EXT_RE = re.compile(
    r"\.(?:png|jpe?g|gif|webp|svg|ico|css|js|mjs|woff2?|ttf|eot|mp4|webm|pdf|zip)$",
    re.IGNORECASE,
)


def _is_benign_platform(host: str) -> bool:
    if host.endswith(BENIGN_TLD_SUFFIXES):
        return True
    parts = host.split(".")
    if len(parts) >= 2:
        root = ".".join(parts[-2:])
        if root in BENIGN_ROOT_DOMAINS:
            return True
    return False


def is_suspect_domain(domain: str) -> bool:
    """High-recall Stage 1 pre-sieve for CertStream SANs and NRD dumps."""
    host = domain.strip().lower().lstrip("*.")
    canon = validate_and_canonicalize(host)
    if canon is None:
        return False
    clean_host = canon.host
    if _is_benign_platform(clean_host):
        return False

    tld = clean_host.rsplit(".", 1)[-1]
    sld = clean_host.split(".")[0]

    if tld in SUSPICIOUS_TLDS:
        return True
    if _SUSPECT_DOMAIN_TOKENS.search(clean_host):
        return True
    if _DIGIT_MIX_RE.search(sld):
        return True
    if sld.count("-") >= 2 and any(ch.isdigit() for ch in sld):
        return True
    return False


def extract_ring_candidates(
    *,
    source_category: str,
    source_host: str,
    outbound_links: Iterable[str],
    hop_depth: int = 0,
    max_hop_depth: int = 2,
) -> list[str]:
    """Filter outbound links from confirmed illegal pages for bounded ring-crawling."""
    if source_category not in ILLEGAL_CATEGORIES:
        return []
    if hop_depth >= max_hop_depth:
        return []

    accepted: list[str] = []
    seen: set[str] = set()
    for link in outbound_links:
        if not link or _ASSET_EXT_RE.search(link.split("?", 1)[0]):
            continue
        canon = validate_and_canonicalize(link)
        if canon is None:
            continue
        if _is_benign_platform(canon.host):
            continue
        # Skip intra-page anchors or identical canonical host root when only path is noisy
        if canon.host == source_host and canon.canonical_key == source_host:
            continue
        if canon.canonical_key in seen:
            continue
        seen.add(canon.canonical_key)
        accepted.append(canon.fetch_url)
    return accepted


def generate_dnstwist_candidates(domain: str, max_variants: int = 40) -> list[str]:
    """Generate TLD/typo/numeric permutations on a confirmed illegal domain."""
    canon = validate_and_canonicalize(domain)
    if canon is None:
        return []

    variants: list[str] = []
    seen: set[str] = {canon.host}

    try:
        import dnstwist

        fuzzer = dnstwist.Fuzzer(canon.host)
        fuzzer.generate()
        for entry in fuzzer.domains:
            cand = str(entry.get("domain", "")).strip().lower()
            if cand and cand not in seen and validate_and_canonicalize(cand) is not None:
                seen.add(cand)
                variants.append(f"https://{cand}")
                if len(variants) >= max_variants:
                    return variants
    except Exception as exc:
        logger.debug("dnstwist fuzzer fallback for %s: %s", canon.host, exc)

    sld = canon.host.split(".", 1)[0]
    for tld in ("xyz", "vip", "site", "top", "club", "bet", "net", "com"):
        for suffix in ("", "77", "88", "99", "138"):
            cand = f"{sld}{suffix}.{tld}"
            if cand not in seen and validate_and_canonicalize(cand) is not None:
                seen.add(cand)
                variants.append(f"https://{cand}")
                if len(variants) >= max_variants:
                    return variants

    return variants


async def fetch_crtsh_domains(
    keyword: str,
    *,
    limit: int = 200,
    timeout_s: float = 20.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """Query historical CT logs from crt.sh for a keyword pattern."""
    url = "https://crt.sh/"
    params = {"q": f"%{keyword}%", "output": "json"}
    async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        entries = resp.json()

    results: list[str] = []
    seen: set[str] = set()
    for row in entries:
        name_val = str(row.get("name_value", ""))
        for line in name_val.splitlines():
            clean = line.strip().lower().lstrip("*.")
            if clean and clean not in seen and is_suspect_domain(clean):
                seen.add(clean)
                results.append(f"https://{clean}")
                if len(results) >= limit:
                    return results
    return results


def ingest_nrd_file(
    path: Path,
    store: SieveStore,
    *,
    cursor_name: str = "nrd_daily",
    source: str = "nrd",
) -> tuple[int, int]:
    """Ingest a daily NRD/domain list idempotently using source_cursors."""
    cursor_key = f"{path.name}:{path.stat().st_size}"
    if store.get_cursor(cursor_name) == cursor_key:
        return 0, 0

    candidates: list[tuple[str, str]] = []
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        domain = raw_line.strip().lstrip("*.")
        if not domain or domain.startswith("#"):
            continue
        if source == "nrd" and not is_suspect_domain(domain):
            continue
        candidates.append((domain, source))

    inserted, skipped = store.enqueue_many(candidates)
    store.set_cursor(cursor_name, cursor_key)
    return inserted, skipped


def parse_certstream_domains(message: dict[str, Any]) -> list[str]:
    """Extract candidate domains passing Stage 1 pre-sieve from a CertStream update."""
    if message.get("message_type") != "certificate_update":
        return []
    data = message.get("data") or {}
    leaf = data.get("leaf_cert") or {}
    all_domains: list[str] = list(leaf.get("all_domains") or [])
    cn = (leaf.get("subject") or {}).get("CN")
    if cn:
        all_domains.append(str(cn))

    suspects: list[str] = []
    seen: set[str] = set()
    for dom in all_domains:
        cleaned = str(dom).strip().lower().lstrip("*.")
        if cleaned and cleaned not in seen and is_suspect_domain(cleaned):
            seen.add(cleaned)
            suspects.append(f"https://{cleaned}")
    return suspects


async def consume_certstream(
    ws_url: str,
    store: SieveStore,
    *,
    stop_event: asyncio.Event,
    max_messages: int | None = None,
    batch_flush_size: int = 50,
) -> int:
    """Stream CertStream SAN updates into the SQLite queue until stopped."""
    total_enqueued = 0
    seen_msgs = 0
    buffer: list[tuple[str, str]] = []

    while not stop_event.is_set():
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except TimeoutError:
                        if buffer:
                            ins, _ = store.enqueue_many(buffer)
                            total_enqueued += ins
                            buffer.clear()
                        continue
                    seen_msgs += 1
                    payload = json.loads(raw)
                    for cand in parse_certstream_domains(payload):
                        buffer.append((cand, "certstream"))
                    if len(buffer) >= batch_flush_size:
                        ins, _ = store.enqueue_many(buffer)
                        total_enqueued += ins
                        buffer.clear()
                    if max_messages is not None and seen_msgs >= max_messages:
                        stop_event.set()
                        break
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("CertStream connection error (%s): %s", ws_url, exc)
            if stop_event.is_set():
                break
            await asyncio.sleep(2.0)

    if buffer:
        ins, _ = store.enqueue_many(buffer)
        total_enqueued += ins
    return total_enqueued
