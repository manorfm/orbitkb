"""Validation of literal public HTTP destinations shared by language analyzers."""

import re
from urllib.parse import urlparse


def literal_public_http_destination(url: str) -> tuple[str, str, int | None, str] | None:
    try:
        parsed = urlparse(url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or host is None or "@" in parsed.netloc:
        return None
    if "?" in url or "#" in url or not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", host):
        return None
    if any(label.startswith("-") or label.endswith("-") for label in host.split(".")):
        return None
    return parsed.scheme, host, port, parsed.path or "/"
