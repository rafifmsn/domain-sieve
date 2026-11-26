"""Unit and boundary tests for URL validation and canonicalization."""

from __future__ import annotations

import pytest

from sieve.canon import validate_and_canonicalize


@pytest.mark.parametrize(
    ("raw_url", "expected_key"),
    [
        ("https://www.TokoBudi.id/", "tokobudi.id"),
        ("tokobudi.id", "tokobudi.id"),
        ("http://TOKOBUDI.id/?utm=ig#promo", "tokobudi.id"),
        ("tokobudi.id/produk", "tokobudi.id/produk"),
        ("tokobudi.id/Produk", "tokobudi.id/Produk"),
        ("blog.tokobudi.id", "blog.tokobudi.id"),
    ],
)
def test_official_reference_vectors(raw_url: str, expected_key: str) -> None:
    res = validate_and_canonicalize(raw_url)
    assert res is not None
    assert res.canonical_key == expected_key


def test_case_sensitive_path_produces_distinct_keys() -> None:
    lower = validate_and_canonicalize("https://tokobudi.id/produk")
    upper = validate_and_canonicalize("https://tokobudi.id/Produk")
    assert lower is not None and upper is not None
    assert lower.canonical_key != upper.canonical_key
    assert lower.canonical_hash != upper.canonical_hash


def test_only_leading_www_is_stripped() -> None:
    sub = validate_and_canonicalize("https://www.blog.tokobudi.id/Promo/")
    assert sub is not None
    assert sub.canonical_key == "blog.tokobudi.id/Promo"

    mid = validate_and_canonicalize("https://sub.www.tokobudi.id/")
    assert mid is not None
    assert mid.canonical_key == "sub.www.tokobudi.id"


@pytest.mark.parametrize(
    "invalid_url",
    [
        "",
        "   ",
        "ftp://tokobudi.id",
        "javascript:alert(1)",
        "//tokobudi.id",
        "https://tokobudi.id:8080/slot",
        "tokobudi.id:8443",
        "https://user:pass@tokobudi.id/",
        "user:pass@tokobudi.id",
        "http://127.0.0.1/admin",
        "https://192.168.1.1",
        "103.10.10.10",
        "http://[::1]/test",
        "http://localhost/test",
        "localhost",
        "https://sub.localhost/test",
        "https://tokobudi.id/with space",
        "https://example.com/" + ("a" * 281),  # > 300 chars
    ],
)
def test_invalid_urls_rejected(invalid_url: str) -> None:
    assert validate_and_canonicalize(invalid_url) is None


def test_exact_300_char_boundary_allowed_and_301_rejected() -> None:
    prefix = "https://example.com/"
    exact_300 = prefix + ("a" * (300 - len(prefix)))
    over_300 = prefix + ("a" * (301 - len(prefix)))
    assert len(exact_300) == 300
    assert len(over_300) == 301

    ok = validate_and_canonicalize(exact_300)
    assert ok is not None
    assert ok.canonical_key == "example.com/" + ("a" * (300 - len(prefix)))
    assert validate_and_canonicalize(over_300) is None
