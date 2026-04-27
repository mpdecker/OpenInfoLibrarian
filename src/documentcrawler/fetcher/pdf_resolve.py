"""Extract likely PDF / download URLs from HTML interstitial pages."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser


def _absolutize(url: str, base: str) -> str:
    u = url.strip().strip("'\"")
    if not u or u.startswith(("javascript:", "mailto:", "#")):
        return ""
    if u.startswith("//"):
        scheme = urlparse(base).scheme or "https"
        return f"{scheme}:{u}"
    if u.startswith(("http://", "https://")):
        return u
    # Preserve leading ``/`` semantics (site-root) — do not strip it before urljoin.
    return urljoin(base, u)


def _meta_refresh_url(html: str, base: str) -> str | None:
    m = re.search(
        r'<meta[^>]+http-equiv\s*=\s*["\']?refresh["\']?[^>]+content\s*=\s*["\']'
        r"([^\"']+)",
        html,
        re.I,
    )
    if not m:
        return None
    content = m.group(1)
    um = re.search(r"url\s*=\s*([^;]+)", content, re.I)
    if not um:
        return None
    return _absolutize(um.group(1).strip(), base) or None


def _onclick_href(onclick: str) -> str | None:
    oc = onclick or ""
    if "location.href" not in oc:
        return None
    tail = oc.split("location.href", 1)[1]
    for sep in ("=",):
        if sep in tail:
            frag = tail.split(sep, 1)[1].strip()
            frag = frag.strip(" '\")];")
            if frag:
                return frag
    return None


def pdf_urls_from_html(html: str, base: str) -> list[str]:
    """Return absolute URLs that may point to a PDF or a binary download.

    Order is discovery order; callers should pass through ``prioritize_pdf_urls``.
    """
    tree = HTMLParser(html)
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        if not raw:
            return
        abs_u = _absolutize(raw, base)
        if abs_u and abs_u not in seen:
            seen.add(abs_u)
            found.append(abs_u)

    mr = _meta_refresh_url(html, base)
    if mr:
        add(mr)

    for node in tree.css("iframe"):
        add(node.attributes.get("src"))
    for node in tree.css("embed"):
        add(node.attributes.get("src"))
    for node in tree.css("object"):
        add(node.attributes.get("data"))

    for node in tree.css("button[onclick]"):
        href = _onclick_href(node.attributes.get("onclick", "") or "")
        if href:
            add(href)

    for node in tree.css("a[href]"):
        href = node.attributes.get("href", "") or ""
        if not href:
            continue
        hl = href.lower()
        if (
            hl.endswith(".pdf")
            or ".pdf?" in hl
            or "/pdf" in hl
            or hl.endswith("/pdf")
            or "/dl/" in hl
            or "get.php" in hl
            or "download" in hl
            or "view" in hl
            or "sci-hub" in hl
            or "slow_download" in hl
        ):
            add(href)

    # LibGen scimag / library.lol — mirror rows often lack ``.pdf`` in the href.
    for node in tree.css("table a[href], .catalog a[href]"):
        href = node.attributes.get("href", "") or ""
        if not href or href in ("#", "javascript:void(0)"):
            continue
        hl = href.lower()
        if "md5=" in hl and "get.php" not in hl:
            continue
        if any(
            x in hl
            for x in (
                ".pdf",
                "get.php",
                "library.lol",
                "libgen.li",
                "sci-hub",
                "download",
                "cloudflare",
                "ipfs",
                "twirpx",
                "bookfi",
            )
        ):
            add(href)

    return found


def prioritize_pdf_urls(urls: list[str]) -> list[str]:
    """Prefer direct ``.pdf`` and known download paths over generic links."""

    def rank(u: str) -> tuple[int, str]:
        ul = u.lower()
        if ul.endswith(".pdf") or ".pdf?" in ul:
            return (0, u)
        if "/dl/" in ul or "get.php" in ul or "download" in ul:
            return (1, u)
        if "slow_download" in ul or "sci-hub" in ul or "library.lol" in ul:
            return (2, u)
        return (3, u)

    return sorted(urls, key=rank)


def is_probably_html(data: bytes) -> bool:
    sample = data[:8000].lstrip().lower()
    return (
        sample.startswith(b"<!")
        or sample.startswith(b"<html")
        or sample.startswith(b"<head")
        or sample.startswith(b"<body")
        or sample.startswith(b"<script")
        or sample.startswith(b"<div")
        or sample.startswith(b"<meta")
    )
