"""Deterministic URL validation and canonicalization."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_URL_LENGTH = 300
_WHITESPACE_RE = re.compile(r"\s")
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True, slots=True)
class CanonResult:
    """Validated and canonicalized URL representation."""

    raw_url: str
    scheme: str
    host: str
    canonical_key: str
    canonical_hash: str
    fetch_url: str
    submit_url: str


def _is_ip_literal(host: str) -> bool:
    candidate = host.strip("[]")
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        return False


def _is_valid_hostname(host: str) -> bool:
    if not host or host == "localhost" or host.endswith(".localhost"):
        return False
    if _is_ip_literal(host):
        return False
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return False
    if len(ascii_host) > 253 or "." not in ascii_host:
        return False
    labels = ascii_host.split(".")
    if any(not _HOST_LABEL_RE.match(label) for label in labels):
        return False
    if labels[-1].isdigit():
        return False
    return True


def validate_and_canonicalize(raw_url: str) -> CanonResult | None:
    """Validate URL constraints and compute the exact canonical key.

    Returns None if the URL violates any constraint:
    - Non-empty string with length <= 300 and no whitespace/control chars
    - Scheme http:// or https:// (or bare domain/path without scheme)
    - No port, no user:pass@, no raw IP/IPv6, no localhost
    """
    if not isinstance(raw_url, str):
        return None
    cleaned = raw_url.strip()
    if not cleaned or len(cleaned) > MAX_URL_LENGTH:
        return None
    if _WHITESPACE_RE.search(cleaned) or any(ord(ch) < 32 for ch in cleaned):
        return None

    if "://" in cleaned:
        scheme_part, rest = cleaned.split("://", 1)
        scheme = scheme_part.lower()
        if scheme not in ("http", "https") or not rest:
            return None
        to_parse = f"{scheme}://{rest}"
    else:
        if cleaned.startswith("//"):
            return None
        scheme = "https"
        to_parse = f"https://{cleaned}"

    try:
        parsed = urlsplit(to_parse)
    except ValueError:
        return None

    if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
        return None
    if ":" in parsed.netloc:
        return None

    try:
        if parsed.port is not None:
            return None
    except ValueError:
        return None

    raw_host = (parsed.hostname or "").rstrip(".").lower()
    if not _is_valid_hostname(raw_host):
        return None

    canon_host = raw_host[4:] if raw_host.startswith("www.") else raw_host
    if not _is_valid_hostname(canon_host):
        return None

    path = parsed.path.rstrip("/")
    canonical_key = f"{canon_host}{path}" if path else canon_host
    canonical_hash = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()
    fetch_url = f"{scheme}://{raw_host}{path}"
    submit_url = f"{scheme}://{canonical_key}"
    if len(submit_url) > MAX_URL_LENGTH:
        return None

    return CanonResult(
        raw_url=cleaned,
        scheme=scheme,
        host=canon_host,
        canonical_key=canonical_key,
        canonical_hash=canonical_hash,
        fetch_url=fetch_url,
        submit_url=submit_url,
    )
