"""Discovery engine: CertStream SAN stream, ring spidering, dnstwist, and NRD/crt.sh seeds."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import struct
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import httpx
import websockets
from cryptography import x509

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
        "bid",
        "loan",
        "men",
        "date",
        "stream",
        "download",
        "racing",
        "review",
        "party",
        "trade",
        "webcam",
        "porn",
        "sex",
        "adult",
        "xxx",
        "tube",
        "cam",
        "gdn",
        "mom",
        "rest",
        "uno",
        "ink",
        "lol",
        "pics",
        "bond",
        "quest",
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
        "cloudflare-dns.com",
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
        "iana.org",
        "ietf.org",
        "pypi.org",
        "npmjs.com",
        "docker.com",
        "reddit.com",
        "stackoverflow.com",
        "medium.com",
        "substack.com",
        "wordpress.org",
        "kompas.com",
        "detik.com",
        "tempo.co",
        "cnnindonesia.com",
        "cnbcindonesia.com",
        "tribunnews.com",
        "liputan6.com",
        "kumparan.com",
        "republika.co.id",
        "bbc.com",
        "bbc.co.uk",
        "reuters.com",
        "apnews.com",
        "nytimes.com",
        "theguardian.com",
        "ojk.go.id",
        "bi.go.id",
        "komdigi.go.id",
        "kominfo.go.id",
        "sfdc.net",
        "cloudflarestorage.com",
        "zendesk.com",
        "microsoft-int.com",
        "hcaptcha.com",
        "hcaptchastatus.com",
        "recaptcha.net",
        "threads.com",
        "threads.net",
        "wa.me",
        "discord.com",
        "discord.gg",
        "line.me",
        "pinterest.com",
        "vimeo.com",
        "twitch.tv",
    }
)

BENIGN_TLD_SUFFIXES = (
    ".gov",
    ".gov.id",
    ".go.id",
    ".edu",
    ".ac.id",
    ".mil",
    ".sch.id",
    ".aws.dev",
    ".azure.us",
    ".internal",
    ".local",
    ".arpa",
)

DEFAULT_CT_LOGS = (
    "https://ct.cloudflare.com/logs/nimbus2026/",
    "https://ct.cloudflare.com/logs/nimbus2027/",
)
CT_LOG_LIST_URL = "https://www.gstatic.com/ct/log_list/v3/log_list.json"

_SUSPECT_DOMAIN_TOKENS = re.compile(
    r"(?:"
    # Indonesian & global gambling / casino / sportsbook / togel
    r"slot|gacor|maxwin|togel|toto|judi|judol|kasino|casino|poker|jackpot|scatter|"
    r"zeus|olympus|pragmatic|mahjong|spaceman|sbobet|pgsoft|habanero|spadegaming|"
    r"joker123|idnplay|idnpoker|pkvgames|bandar|parlay|mixparlay|sicbo|baccarat|"
    r"roulette|blackjack|dominoqq|bandarq|ceme|qiuqiu|sabung|taruhan|betting|"
    r"sportsbook|wagering|rungkad|depo|cuan|hoki|prediksi|syair|keluaran|livedraw|"
    r"nawala|alternatif|kakekzeus|bonanza|gamble|gambling|wager|freespin|bookie|"
    r"lotto|lottery|keno|bingo|1xbet|betway|bet365|roobet|rollbit|"
    # Global high-volume non-English Romanized betting / loan / adult tokens
    r"apuestas|tragamonedas|cassino|emprestimo|prestamo|putaria|novinha|"
    r"kazino|stavki|vulkan|azino|zaim|mikrozaim|nhacai|xoso|taixiu|nohu|bahis|kumari|"
    # Indonesian & global lending / pinjol
    r"pinjol|pinjam|pinjaman|kredit|tunai|duit|cair|rupiah|angsuran|tenor|"
    r"talangan|gadai|payday|cashadvance|fastcash|instantloan|microloan|quickloan|"
    r"cashloan|uangcepat|danacepat|danakilat|galbay|"
    # Phishing, brand impersonation, financial fraud
    r"phish|verify|verification|verifikasi|signin|logon|suspend|unlock|"
    r"recovery|claim|giveaway|undian|pemenang|hadiah|bansos|bantuan|tilang|etle|"
    r"pajak|npwp|bpjs|dukcapil|brimo|mybca|livin|mandiri|shopeepay|linkaja|"
    r"gopay|danakaget|airdrop|seedphrase|metamask|walletconnect|trustwallet|"
    r"akun|resmi|helpdesk|"
    # Adult / escort / illegal sexual content
    r"bokep|porn|porno|pornografi|hentai|nsfw|adult|18plus|erotic|escort|openbo|"
    r"cewekbo|cewebo|panggilan|ngewe|colmek|simontok|nekopoi|xnxx|xvideos|"
    r"xhamster|chaturbate|camgirl|livecam|sexcam|milf|javsub|viral18|"
    # Drugs, counterfeit documents, cybercrime
    r"obatkeras|aborsi|alprazolam|narkoba|ijazahpalsu|dokumenpalsu|stealer|"
    r"carding|cvvdump|banklog|fullz|cracked|combolist|pharma|fakeid|counterfeit"
    r")",
    re.IGNORECASE,
)

# Short 2-4 char tokens kept segment-bounded to avoid substring false positives
_BOUNDED_SUSPECT_TOKENS = re.compile(
    r"(?:^|[.\-_0-9])"
    r"(?:bo|jp|wd|rtp|bri|bca|bni|bsi|ovo|dana|sgp|sdy|hk|4d|pkv|idn|bet|win|"
    r"vip|sex|xxx|jav|cam|vcs|amp|apk|cvv|cc|rx)"
    r"(?:$|[.\-_0-9])",
    re.IGNORECASE,
)

_DIGIT_MIX_RE = re.compile(r"(?:[a-z]+\d{2,}|\d{2,}[a-z]+)", re.IGNORECASE)
_PARKING_SUBDOMAIN_RE = re.compile(r"^ww\d+\.", re.IGNORECASE)
_ASSET_EXT_RE = re.compile(
    r"\.(?:png|jpe?g|gif|webp|svg|ico|css|js|mjs|woff2?|ttf|eot|mp4|webm|pdf|zip|rss|atom|xml)$",
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
    host = domain.strip().lower()
    if host.startswith("*."):
        host = host[2:]
    if not host or len(host) > 300:
        return False

    if "://" in host or "/" in host:
        canon = validate_and_canonicalize(host)
        if canon is None:
            return False
        clean_host = canon.host
    else:
        if "." not in host or ":" in host or "@" in host or " " in host:
            return False
        clean_host = host[4:] if host.startswith("www.") else host

    if _is_benign_platform(clean_host) or _PARKING_SUBDOMAIN_RE.match(clean_host):
        return False

    tld = clean_host.rsplit(".", 1)[-1]
    sld = clean_host.split(".", 1)[0]

    matched = (
        tld in SUSPICIOUS_TLDS
        or bool(_SUSPECT_DOMAIN_TOKENS.search(clean_host))
        or bool(_BOUNDED_SUSPECT_TOKENS.search(clean_host))
        or bool(_DIGIT_MIX_RE.search(sld))
        or (sld.count("-") >= 2 and any(ch.isdigit() for ch in sld))
    )
    if not matched:
        return False
    return validate_and_canonicalize(clean_host) is not None


def extract_ring_candidates(
    *,
    source_category: str,
    source_host: str,
    outbound_links: Iterable[str],
    hop_depth: int = 0,
    max_hop_depth: int = 2,
    max_links: int = 15,
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
        # Only follow cross-domain links to discover mirror/affiliate domains without internal bloat
        if canon.host == source_host:
            continue
        if canon.canonical_key in seen:
            continue
        seen.add(canon.canonical_key)
        accepted.append(canon.fetch_url)
        if len(accepted) >= max_links:
            break
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


def extract_domains_from_ct_entry(entry: dict[str, Any]) -> list[str]:
    """Parse an RFC 6962 CT log entry and return suspect https:// URLs."""
    try:
        leaf = base64.b64decode(entry["leaf_input"])
        if len(leaf) < 15:
            return []
        entry_type = struct.unpack("!H", leaf[10:12])[0]
        if entry_type == 0:
            cert_len = int.from_bytes(leaf[12:15], "big")
            der = leaf[15 : 15 + cert_len]
        elif entry_type == 1:
            extra = base64.b64decode(entry["extra_data"])
            cert_len = int.from_bytes(extra[0:3], "big")
            der = extra[3 : 3 + cert_len]
        else:
            return []
        cert = x509.load_der_x509_certificate(der)
    except Exception:
        return []

    raw_domains: list[str] = []
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        raw_domains.extend(san.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        pass
    if not raw_domains:
        for attr in cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME):
            raw_domains.append(str(attr.value))

    suspects: list[str] = []
    seen: set[str] = set()
    for dom in raw_domains:
        cleaned = dom.strip().lower().lstrip("*.")
        if cleaned and cleaned not in seen and is_suspect_domain(cleaned):
            seen.add(cleaned)
            suspects.append(f"https://{cleaned}")
    return suspects


async def discover_usable_ct_logs(
    client: httpx.AsyncClient,
    *,
    candidates: Iterable[str] = DEFAULT_CT_LOGS,
    max_logs: int = 3,
) -> list[str]:
    """Probe fallback CT logs and gstatic log_list.json for responsive RFC 6962 logs."""
    probe_urls: list[str] = [u if u.endswith("/") else f"{u}/" for u in candidates]

    try:
        resp = await client.get(CT_LOG_LIST_URL, timeout=8.0)
        if resp.status_code == 200:
            data = resp.json()
            for operator in data.get("operators", []):
                for log_info in operator.get("logs", []):
                    state = log_info.get("state") or {}
                    url = str(log_info.get("url", ""))
                    if "usable" in state and url:
                        normalized = url if url.endswith("/") else f"{url}/"
                        if normalized not in probe_urls:
                            probe_urls.append(normalized)
    except Exception as exc:
        logger.debug("CT log list fetch skipped: %s", exc)

    usable: list[str] = []
    for url in probe_urls:
        try:
            sth = await client.get(f"{url}ct/v1/get-sth", timeout=5.0)
            if sth.status_code == 200 and int(sth.json().get("tree_size", 0)) > 0:
                usable.append(url)
                if len(usable) >= max_logs:
                    break
        except Exception:
            continue
    return usable


async def poll_ct_logs(
    store: SieveStore,
    *,
    stop_event: asyncio.Event,
    log_urls: Iterable[str] | None = None,
    backfill_entries: int = 1500,
    batch_entries: int = 256,
    poll_interval_s: float = 2.0,
    max_queued: int = 3000,
    on_batch: Callable[[int, int, int, str], None] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Poll public RFC 6962 CT logs directly and enqueue suspect SAN domains."""
    total_enqueued = 0
    async with httpx.AsyncClient(timeout=15.0, transport=transport) as client:
        active_logs = (
            [u if u.endswith("/") else f"{u}/" for u in log_urls]
            if log_urls is not None
            else await discover_usable_ct_logs(client)
        )
        if not active_logs:
            logger.warning("No responsive RFC 6962 CT logs found")
            return 0

        paused_by_backpressure = False
        while not stop_event.is_set():
            if store.count_queued() >= max_queued:
                paused_by_backpressure = True
                await asyncio.sleep(5.0)
                continue

            progressed = False
            for log_url in active_logs:
                if stop_event.is_set() or store.count_queued() >= max_queued:
                    break
                try:
                    sth_resp = await client.get(f"{log_url}ct/v1/get-sth", timeout=8.0)
                    sth_resp.raise_for_status()
                    tree_size = int(sth_resp.json().get("tree_size", 0))
                    if tree_size <= 0:
                        continue

                    cursor_name = f"ctlog:{log_url}"
                    saved = store.get_cursor(cursor_name)
                    cur = int(saved) if saved is not None else max(0, tree_size - backfill_entries)
                    # Snap forward to fresh certs after downtime or backpressure pause
                    fresh_floor = max(0, tree_size - backfill_entries)
                    if paused_by_backpressure or cur < tree_size - (backfill_entries * 4):
                        cur = max(cur, fresh_floor)

                    while cur < tree_size and not stop_event.is_set():
                        if store.count_queued() >= max_queued:
                            paused_by_backpressure = True
                            break
                        end = min(cur + batch_entries - 1, tree_size - 1)
                        ent_resp = await client.get(
                            f"{log_url}ct/v1/get-entries",
                            params={"start": cur, "end": end},
                            timeout=15.0,
                        )
                        ent_resp.raise_for_status()
                        entries = ent_resp.json().get("entries") or []
                        if not entries:
                            break

                        candidates: list[tuple[str, str]] = []
                        for entry in entries:
                            for cand_url in extract_domains_from_ct_entry(entry):
                                candidates.append((cand_url, "certstream"))

                        ins, skip = store.enqueue_many(candidates) if candidates else (0, 0)
                        total_enqueued += ins
                        cur += len(entries)
                        store.set_cursor(cursor_name, str(cur))
                        progressed = True

                        if on_batch is not None:
                            on_batch(len(entries), ins, skip, log_url)
                        await asyncio.sleep(0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("CT log poll error (%s): %s", log_url, exc)

            if paused_by_backpressure and store.count_queued() < max_queued:
                paused_by_backpressure = False
            if stop_event.is_set():
                break
            await asyncio.sleep(0.2 if progressed else poll_interval_s)

    return total_enqueued
