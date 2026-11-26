"""Smoke tests for crawler asset blocking, DoH toggle, sinkhole detection, and Markdown extraction."""

from __future__ import annotations

import asyncio

import httpx

from sieve.crawler import (
    build_browser_args,
    build_pruned_markdown,
    extract_outbound_links,
    is_dead_or_parked_page,
    is_trustpositif_block_page,
    prefilter_live_targets,
    resolve_host_ips,
    should_abort_resource,
)
from sieve.store import UrlRecord


def test_asset_blocking_and_doh_browser_args() -> None:
    assert should_abort_resource("image") is True
    assert should_abort_resource("media") is True
    assert should_abort_resource("font") is True
    assert should_abort_resource("stylesheet") is True
    assert should_abort_resource("document") is False
    assert should_abort_resource("script") is False

    isp_args = build_browser_args(doh_enabled=False)
    assert "--disable-gpu" in isp_args
    assert not any("dns-over-https" in a for a in isp_args)

    doh_args = build_browser_args(doh_enabled=True)
    assert "--disable-gpu" in doh_args
    assert any("1.1.1.1" in a for a in doh_args)


def test_trustpositif_sinkhole_detection() -> None:
    assert is_trustpositif_block_page("https://internetpositif.uzone.id/", "", "") is True
    assert (
        is_trustpositif_block_page(
            "https://example.com",
            "Situs Ini Diblokir",
            "Konten diblokir oleh Komdigi sesuai peraturan.",
        )
        is True
    )
    assert (
        is_trustpositif_block_page(
            "https://tokobudi.id",
            "Toko Budi",
            "Perlengkapan rumah tangga murah.",
        )
        is False
    )


def test_dead_or_parked_page_detection() -> None:
    assert is_dead_or_parked_page("", "") is True
    assert is_dead_or_parked_page("Redirecting...", "Redirecting...") is True
    assert (
        is_dead_or_parked_page(
            "fly88app.com is for sale - Buy now for $499 | GoDaddy",
            "fly88app.com is for sale - Buy now for $499. Get this domain today.",
        )
        is True
    )
    assert (
        is_dead_or_parked_page(
            "Slot88 Gacor",
            "# Slot88 Gacor\nDaftar akun sekarang dan klaim bonus deposit harian via QRIS dan transfer bank.",
        )
        is False
    )


def test_pruned_markdown_and_outbound_links() -> None:
    md = build_pruned_markdown(
        title="Slot Gacor 88",
        meta_description="Deposit QRIS 10rb",
        raw_markdown="# Menu Utama\n## Promo Bonus\n" + ("Isi konten promo " * 200),
        max_body_chars=1500,
    )
    assert md.startswith("# Slot Gacor 88")
    assert "Description: Deposit QRIS 10rb" in md
    assert "## Promo Bonus" in md
    assert len(md) < 2000

    links = extract_outbound_links(
        base_url="https://slot88.xyz",
        crawl4ai_links={"external": [{"href": "https://mirror88.vip/daftar"}], "internal": [{"href": "/promo"}]},
    )
    assert "https://mirror88.vip/daftar" in links
    assert "https://slot88.xyz/promo" in links


def test_doh_resolver_and_sinkhole_prefilter(monkeypatch) -> None:
    def doh_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "1.1.1.1"
        name = request.url.params["name"]
        if name == "blocked.xyz":
            return httpx.Response(200, json={"Answer": [{"type": 1, "data": "36.86.63.185"}]})
        return httpx.Response(200, json={"Answer": [{"type": 1, "data": "93.184.216.34"}]})

    async def _run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(doh_handler)) as client:
            ips = await resolve_host_ips("live.xyz", doh_enabled=True, doh_client=client)
            assert ips == ["93.184.216.34"]

        async def fake_resolve(host: str, **_: object) -> list[str]:
            if host == "blocked.xyz":
                return ["36.86.63.185"]
            if host == "nx.xyz":
                return []
            return ["93.184.216.34"]

        async def fake_tcp(ips: list[str], **_: object) -> bool:
            return ips == ["93.184.216.34"]

        monkeypatch.setattr("sieve.crawler.resolve_host_ips", fake_resolve)
        monkeypatch.setattr("sieve.crawler.check_tcp_liveness", fake_tcp)

        recs = [
            UrlRecord("live.xyz", "h1", "https://live.xyz", "seed", "queued", None, None, None, None, None, None, None, "t", None, None),
            UrlRecord("blocked.xyz", "h2", "https://blocked.xyz", "seed", "queued", None, None, None, None, None, None, None, "t", None, None),
            UrlRecord("nx.xyz", "h3", "https://nx.xyz", "seed", "queued", None, None, None, None, None, None, None, "t", None, None),
        ]
        live, dead = await prefilter_live_targets(recs)
        assert [r.canonical_key for r in live] == ["live.xyz"]
        assert dict(dead) == {"blocked.xyz": "isp_blocked", "nx.xyz": "nxdomain"}

    asyncio.run(_run())
