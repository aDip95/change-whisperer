from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlparse


def escape_html(text: str) -> str:
    cleaned = "".join(c for c in text if c.isprintable() or c in "\n\t ")
    return cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def is_valid_url(url: str) -> bool:
    """Public http(s) URL only: no credentials, no private/loopback IP literal."""
    try:
        if url != url.strip() or any(c in url for c in '<>"'):
            return False
        result = urlparse(url)
        if (
            result.scheme not in ("http", "https")
            or not result.hostname
            or result.username
            or result.password
        ):
            return False
        try:
            return ip_address(result.hostname).is_global
        except ValueError:
            return True  # hostname: resolved IPs are checked by scraper.PublicResolver
    except (TypeError, ValueError):
        return False


def truncate(text: str, max_len: int = 100) -> str:
    return text[: max_len - 1] + "…" if len(text) > max_len else text
