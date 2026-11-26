"""Async DNS/TCP liveness pre-filter and single-browser Crawl4AI rendering pool."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import aiodns
import httpx

from sieve.store import UrlRecord

logger = logging.getLogger(__name__)

BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font", "stylesheet"})

TRUSTPOSITIF_SINKHOLE_IPS = frozenset(
    {
        "36.86.63.185",
        "118.97.115.202",
        "202.3.218.137",
        "103.226.139.200",
        "114.7.252.50",
        "127.0.0.1",
        "0.0.0.0",
    }
)

_TRUSTPOSITIF_HOST_MARKERS = (
    "internetbaik.telkomsel.com",
    "internetpositif.uzone.id",
    "mercusuar.uzone.id",
    "internet-positif.org",
    "lamanlabuh.aduankonten.id",
    "trustpositif.komdigi.go.id",
    "trustpositif.kominfo.go.id",
    "blockpage.xl.co.id",
    "safesurf.indosat",
)

_TRUSTPOSITIF_TEXT_MARKERS = (
    "situs ini diblokir",
    "diblokir oleh kominfo",
    "diblokir oleh komdigi",
    "internet positif",
    "internet baik",
    "laman labuh",
    "aduankonten",
)

_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
_META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^#{1,3}\s+.+$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class CrawlOutcome:
    """Normalized Crawl4AI extraction result for a single URL."""

    canonical_key: str
    canonical_hash: str
    raw_url: str
    source: str
    status: str  # crawled | dead | isp_blocked | error
    http_status: int | None
    headers: dict[str, str]
    title: str
    markdown: str
    outbound_links: list[str]
    latency_ms: float
    error_message: str = ""


def is_trustpositif_block_page(url: str, title: str, content: str) -> bool:
    """Detect Indonesian ISP TrustPositif sinkhole redirects or block pages."""
    lower_url = url.lower()
    if any(marker in lower_url for marker in _TRUSTPOSITIF_HOST_MARKERS):
        return True
    combined = f"{title}\n{content[:2000]}".lower()
    return any(marker in combined for marker in _TRUSTPOSITIF_TEXT_MARKERS)


def build_pruned_markdown(
    *,
    title: str,
    meta_description: str,
    raw_markdown: str,
    max_body_chars: int = 1500,
) -> str:
    """Assemble compact Markdown (title + meta + headings + first ~1,500 chars body)."""
    sections: list[str] = []
    if title.strip():
        sections.append(f"# {title.strip()}")
    if meta_description.strip():
        sections.append(f"Description: {meta_description.strip()}")

    headings = _HEADING_RE.findall(raw_markdown)
    if headings:
        unique_headings = list(dict.fromkeys(h.strip() for h in headings[:12]))
        sections.append("\n".join(unique_headings))

    body_clean = re.sub(r"\n{3,}", "\n\n", raw_markdown).strip()
    if body_clean:
        sections.append(body_clean[:max_body_chars])
    return "\n\n".join(sections).strip()


def extract_outbound_links(
    *,
    base_url: str,
    crawl4ai_links: dict[str, Any] | None,
    html: str = "",
) -> list[str]:
    """Collect deduplicated outbound HTTP(S) links from Crawl4AI links or raw HTML."""
    raw_hrefs: list[str] = []
    if isinstance(crawl4ai_links, dict):
        for bucket in ("external", "internal"):
            for entry in crawl4ai_links.get(bucket) or []:
                href = entry.get("href") if isinstance(entry, dict) else str(entry)
                if href:
                    raw_hrefs.append(str(href))
    if not raw_hrefs and html:
        raw_hrefs.extend(_HREF_RE.findall(html))

    resolved: list[str] = []
    seen: set[str] = set()
    for href in raw_hrefs:
        joined = urljoin(base_url, href.strip())
        if not (joined.startswith("http://") or joined.startswith("https://")):
            continue
        if joined not in seen:
            seen.add(joined)
            resolved.append(joined)
    return resolved


async def resolve_host_ips(
    host: str,
    *,
    doh_enabled: bool = False,
    timeout_s: float = 3.0,
    doh_client: httpx.AsyncClient | None = None,
) -> list[str]:
    """Resolve IPv4 A records using either system ISP DNS or Cloudflare DoH (1.1.1.1)."""
    if doh_enabled:
        owns_client = doh_client is None
        client = doh_client or httpx.AsyncClient(timeout=timeout_s)
        try:
            resp = await client.get(
                "https://1.1.1.1/dns-query",
                params={"name": host, "type": "A"},
                headers={"accept": "application/dns-json"},
            )
            resp.raise_for_status()
            payload = resp.json()
            answers = payload.get("Answer") or []
            return [str(ans["data"]) for ans in answers if ans.get("type") == 1 and ans.get("data")]
        except Exception:
            return []
        finally:
            if owns_client:
                await client.aclose()

    resolver = aiodns.DNSResolver(timeout=timeout_s)
    try:
        records = await resolver.query(host, "A")
        return [str(rec.host) for rec in records if getattr(rec, "host", None)]
    except Exception:
        return []


async def check_tcp_liveness(
    ips: Sequence[str],
    *,
    ports: Sequence[int] = (443, 80),
    timeout_s: float = 2.5,
) -> bool:
    """Verify at least one resolved IP accepts TCP connections on 443 or 80."""
    for ip in ips[:2]:
        for port in ports:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(ip, port),
                    timeout=timeout_s,
                )
                writer.close()
                await writer.wait_closed()
                return True
            except Exception:
                continue
    return False


async def prefilter_live_targets(
    records: Sequence[UrlRecord],
    *,
    doh_enabled: bool = False,
    concurrency: int = 100,
    timeout_s: float = 3.0,
) -> tuple[list[UrlRecord], list[tuple[str, str]]]:
    """Run high-concurrency async DNS + sinkhole + TCP liveness check before Crawl4AI."""
    sem = asyncio.Semaphore(concurrency)
    live: list[UrlRecord] = []
    dead: list[tuple[str, str]] = []

    doh_client = httpx.AsyncClient(timeout=timeout_s) if doh_enabled else None

    async def _probe(rec: UrlRecord) -> tuple[UrlRecord, str | None]:
        host = rec.canonical_key.split("/", 1)[0]
        async with sem:
            ips = await resolve_host_ips(
                host,
                doh_enabled=doh_enabled,
                timeout_s=timeout_s,
                doh_client=doh_client,
            )
            if not ips:
                return rec, "nxdomain"
            if any(ip in TRUSTPOSITIF_SINKHOLE_IPS for ip in ips):
                return rec, "isp_blocked"
            if not await check_tcp_liveness(ips, timeout_s=min(timeout_s, 2.5)):
                return rec, "tcp_unreachable"
            return rec, None

    try:
        outcomes = await asyncio.gather(*(_probe(r) for r in records))
        for rec, reason in outcomes:
            if reason is None:
                live.append(rec)
            else:
                dead.append((rec.canonical_key, reason))
    finally:
        if doh_client is not None:
            await doh_client.aclose()

    return live, dead


def build_browser_args(*, doh_enabled: bool = False) -> list[str]:
    """Return Chromium launch args (--disable-gpu plus optional DoH resolver flags)."""
    args = [
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    if doh_enabled:
        args.extend(
            [
                "--dns-over-https-mode=secure",
                "--dns-over-https-templates=https://1.1.1.1/dns-query{?dns}",
            ]
        )
    return args


def should_abort_resource(resource_type: str) -> bool:
    """Return True if Playwright resource type should be aborted to save RAM/bandwidth."""
    return resource_type.lower() in BLOCKED_RESOURCE_TYPES


async def _abort_heavy_assets_route(route: Any) -> None:
    if should_abort_resource(getattr(route.request, "resource_type", "")):
        await route.abort()
    else:
        await route.continue_()


class CrawlPool:
    """Single shared Crawl4AI Chromium pool with asset blocking and RAM adaptive cap."""

    def __init__(
        self,
        *,
        doh_enabled: bool = False,
        concurrency: int = 12,
        memory_threshold_pct: float = 70.0,
        page_timeout_ms: int = 20000,
    ) -> None:
        self.doh_enabled = doh_enabled
        self.concurrency = concurrency
        self.memory_threshold_pct = memory_threshold_pct
        self.page_timeout_ms = page_timeout_ms
        self._crawler: Any = None
        self._dispatcher: Any = None
        self._run_config: Any = None

    async def __aenter__(self) -> CrawlPool:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
        from crawl4ai.async_dispatcher import MemoryAdaptiveDispatcher

        browser_cfg = BrowserConfig(
            headless=True,
            extra_args=build_browser_args(doh_enabled=self.doh_enabled),
            verbose=False,
        )
        self._run_config = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            page_timeout=self.page_timeout_ms,
            wait_until="domcontentloaded",
            exclude_all_images=True,
            verbose=False,
        )
        self._dispatcher = MemoryAdaptiveDispatcher(
            memory_threshold_percent=self.memory_threshold_pct,
            max_session_permit=self.concurrency,
        )
        self._crawler = AsyncWebCrawler(config=browser_cfg)
        await self._crawler.__aenter__()

        async def _on_page_context_created(page: Any, context: Any = None, **_: Any) -> Any:
            await page.route("**/*", _abort_heavy_assets_route)
            return page

        if hasattr(self._crawler, "crawler_strategy") and hasattr(
            self._crawler.crawler_strategy, "set_hook"
        ):
            self._crawler.crawler_strategy.set_hook(
                "on_page_context_created", _on_page_context_created
            )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._crawler is not None:
            await self._crawler.__aexit__(*args)
            self._crawler = None

    async def crawl_batch(self, records: Sequence[UrlRecord]) -> list[CrawlOutcome]:
        if not records:
            return []
        if self._crawler is None:
            raise RuntimeError("CrawlPool must be entered with async with")

        by_url = {rec.raw_url: rec for rec in records}
        urls = [rec.raw_url for rec in records]
        t0 = time.perf_counter()
        raw_results = await self._crawler.arun_many(
            urls=urls,
            config=self._run_config,
            dispatcher=self._dispatcher,
        )
        batch_ms = (time.perf_counter() - t0) * 1000.0
        per_url_ms = batch_ms / max(len(records), 1)

        outcomes: list[CrawlOutcome] = []
        seen_keys: set[str] = set()
        for res in raw_results:
            rec = by_url.get(getattr(res, "url", ""))
            if rec is None:
                continue
            seen_keys.add(rec.canonical_key)
            if not getattr(res, "success", False):
                outcomes.append(
                    CrawlOutcome(
                        canonical_key=rec.canonical_key,
                        canonical_hash=rec.canonical_hash,
                        raw_url=rec.raw_url,
                        source=rec.source,
                        status="error",
                        http_status=getattr(res, "status_code", None),
                        headers={},
                        title="",
                        markdown="",
                        outbound_links=[],
                        latency_ms=round(per_url_ms, 2),
                        error_message=str(getattr(res, "error_message", "crawl failed")),
                    )
                )
                continue

            html = str(getattr(res, "html", "") or "")
            raw_md = str(getattr(res, "markdown", "") or "")
            metadata = getattr(res, "metadata", None) or {}
            title = str(metadata.get("title", "") or "")
            meta_desc = str(metadata.get("description", "") or "")
            if not meta_desc and html:
                m = _META_DESC_RE.search(html)
                if m:
                    meta_desc = m.group(1)

            final_url = str(getattr(res, "redirected_url", None) or getattr(res, "url", rec.raw_url))
            if is_trustpositif_block_page(final_url, title, raw_md or html):
                outcomes.append(
                    CrawlOutcome(
                        canonical_key=rec.canonical_key,
                        canonical_hash=rec.canonical_hash,
                        raw_url=rec.raw_url,
                        source=rec.source,
                        status="isp_blocked",
                        http_status=getattr(res, "status_code", None),
                        headers=dict(getattr(res, "response_headers", None) or {}),
                        title=title,
                        markdown="",
                        outbound_links=[],
                        latency_ms=round(per_url_ms, 2),
                    )
                )
                continue

            pruned_md = build_pruned_markdown(
                title=title,
                meta_description=meta_desc,
                raw_markdown=raw_md,
            )
            links = extract_outbound_links(
                base_url=final_url,
                crawl4ai_links=getattr(res, "links", None),
                html=html,
            )
            headers = {
                str(k): str(v) for k, v in (getattr(res, "response_headers", None) or {}).items()
            }
            outcomes.append(
                CrawlOutcome(
                    canonical_key=rec.canonical_key,
                    canonical_hash=rec.canonical_hash,
                    raw_url=rec.raw_url,
                    source=rec.source,
                    status="crawled",
                    http_status=getattr(res, "status_code", None),
                    headers=headers,
                    title=title,
                    markdown=pruned_md,
                    outbound_links=links,
                    latency_ms=round(per_url_ms, 2),
                )
            )

        for rec in records:
            if rec.canonical_key not in seen_keys:
                outcomes.append(
                    CrawlOutcome(
                        canonical_key=rec.canonical_key,
                        canonical_hash=rec.canonical_hash,
                        raw_url=rec.raw_url,
                        source=rec.source,
                        status="error",
                        http_status=None,
                        headers={},
                        title="",
                        markdown="",
                        outbound_links=[],
                        latency_ms=round(per_url_ms, 2),
                        error_message="missing result from crawl4ai batch",
                    )
                )

        return outcomes
