"""Web tools — Brave search + single-URL fetch/extract for tool-use dispatch."""

from __future__ import annotations

import io
import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse, urljoin

import requests


def web_search(query: str, count: int = 5, *, ctx: Any) -> dict[str, Any]:
    if ctx.brave is None:
        return {"error": "web search unavailable"}
    results = ctx.brave.search(query, count=count)
    return {
        "results": [
            {"title": r.title, "url": r.url, "description": r.description}
            for r in results
        ]
    }


# --------------------------------------------------------------------------- #
# fetch_url — retrieve & read a single HTML/PDF/text source.
#
# Security boundary: this runs on the host with the dashboard / cron, takes an
# agent-chosen URL, and has NO per-fetch human approval. The SSRF guard is
# load-bearing: scheme allowlist + hard-block of all private/loopback/link-local/
# reserved IPs, re-checked on every redirect hop. Never raises; every failure
# path returns {"error": "<short reason>"}.
#
# The DNS resolver (`_resolve_host`) and the HTTP layer (`_http_get`) are kept as
# module-level functions so unit tests can monkeypatch them and avoid real
# network I/O.
# --------------------------------------------------------------------------- #

_ALLOWED_SCHEMES = {"http", "https"}
_ALLOWED_CONTENT_TYPES = {"text/html", "application/pdf", "text/plain"}
_MAX_BYTES = 20 * 1024 * 1024          # 20 MB download cap
_TIMEOUT_SECONDS = 20
_MAX_REDIRECTS = 5
_DEFAULT_MAX_CHARS = 30000
_STREAM_CHUNK = 64 * 1024
_USER_AGENT = "autonomous-agent-fetch/1.0 (+research; text-only)"

# CGNAT 100.64.0.0/10 — not flagged by ipaddress.is_private; cover explicitly.
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    """True if `ip` is in any disallowed range (private/loopback/etc.)."""
    # Unwrap IPv4-mapped / 6to4-style IPv6 so the v4 ranges are enforced too.
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or (ip.sixtofour if ip.sixtofour else None)
        if mapped is not None:
            ip = mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NET:
        return True
    return False


def _resolve_host(host: str) -> list[str]:
    """Resolve a hostname to a list of IP-address strings (all A/AAAA records).

    Monkeypatched in tests. A bare IP literal resolves to itself.
    """
    infos = socket.getaddrinfo(host, None)
    addrs: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0] not in addrs:
            addrs.append(sockaddr[0])
    return addrs


def _host_is_blocked(host: str) -> bool:
    """True if `host` resolves to (or is) any blocked IP, or cannot be resolved.

    Fails closed: a resolution failure is treated as blocked.
    """
    if not host:
        return True
    # Strip IPv6 brackets if present.
    h = host.strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    try:
        addrs = _resolve_host(h)
    except Exception:
        return True
    if not addrs:
        return True
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            return True
        if _ip_is_blocked(ip):
            return True
    return False


def _http_get(url: str, *, stream: bool = True) -> requests.Response:
    """Perform a single (non-redirect-following) GET. Monkeypatched in tests."""
    return requests.get(
        url,
        stream=stream,
        allow_redirects=False,
        timeout=_TIMEOUT_SECONDS,
        headers={"User-Agent": _USER_AGENT},
    )


def _parse_page_range(page_range: str, total_pages: int) -> tuple[int, int] | None:
    """Parse 1-indexed inclusive "N" or "N-M" against total_pages.

    Returns (start0, end0) as 0-indexed half-open bounds, or None if invalid.
    """
    if page_range is None:
        return None
    s = str(page_range).strip()
    if not s:
        return None
    try:
        if "-" in s:
            lo_s, hi_s = s.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
        else:
            lo = hi = int(s)
    except ValueError:
        return None
    if lo < 1 or hi < 1 or hi < lo or lo > total_pages:
        return None
    hi = min(hi, total_pages)
    return (lo - 1, hi)


def _read_body(resp: requests.Response) -> bytes | dict:
    """Stream the body up to the cap. Returns bytes, or {"error": ...} on overflow."""
    # Honor an oversize Content-Length up front.
    clen = resp.headers.get("Content-Length")
    if clen is not None:
        try:
            if int(clen) > _MAX_BYTES:
                return {"error": "response too large (Content-Length exceeds 20MB cap)"}
        except ValueError:
            pass
    buf = io.BytesIO()
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=_STREAM_CHUNK):
            if not chunk:
                continue
            total += len(chunk)
            if total > _MAX_BYTES:
                return {"error": "response too large (exceeded 20MB cap while streaming)"}
            buf.write(chunk)
    except Exception as e:
        return {"error": f"read error: {type(e).__name__}"}
    return buf.getvalue()


def _extract_html(body: bytes) -> tuple[str, str | None]:
    """Return (text, title) from HTML using trafilatura, with a tag-strip fallback."""
    try:
        html = body.decode("utf-8", errors="replace")
    except Exception:
        html = body.decode("latin-1", errors="replace")

    text = ""
    title: str | None = None
    try:
        import trafilatura

        extracted = trafilatura.extract(
            html, include_comments=False, include_tables=True
        )
        if extracted:
            text = extracted.strip()
    except Exception:
        text = ""

    # Title: prefer the document <title> element, then trafilatura metadata.
    title = _title_from_html(html)
    if not title:
        try:
            import trafilatura

            meta = trafilatura.extract_metadata(html)
            if meta is not None and getattr(meta, "title", None):
                title = meta.title
        except Exception:
            title = None

    if not text:
        text = _strip_tags(html)
    return text, title


def _strip_tags(html: str) -> str:
    """Very small fallback: drop script/style and tags, collapse whitespace."""
    import re

    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    import html as _htmlmod

    text = _htmlmod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _title_from_html(html: str) -> str | None:
    import re

    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if m:
        import html as _htmlmod

        return re.sub(r"\s+", " ", _htmlmod.unescape(m.group(1))).strip() or None
    return None


def _extract_pdf(body: bytes, page_range: str | None) -> dict:
    """Return {text, pages, pages_returned} or {"error": ...}."""
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(body)) as pdf:
            total_pages = len(pdf.pages)
            if total_pages == 0:
                return {"error": "pdf has no pages"}

            if page_range is not None and str(page_range).strip():
                bounds = _parse_page_range(page_range, total_pages)
                if bounds is None:
                    return {"error": f"invalid page_range: {page_range!r}"}
                start0, end0 = bounds
                pages_returned = (
                    f"{start0 + 1}"
                    if end0 - start0 == 1
                    else f"{start0 + 1}-{end0}"
                )
            else:
                start0, end0 = 0, total_pages
                pages_returned = f"1-{total_pages}" if total_pages > 1 else "1"

            parts: list[str] = []
            for page in pdf.pages[start0:end0]:
                parts.append(page.extract_text() or "")
            text = "\n\n".join(p for p in parts if p).strip()
    except Exception as e:
        return {"error": f"pdf parse error: {type(e).__name__}"}

    return {"text": text, "pages": total_pages, "pages_returned": pages_returned}


def _normalize_content_type(raw: str | None) -> str:
    if not raw:
        return ""
    return raw.split(";", 1)[0].strip().lower()


def fetch_url(
    url: str,
    max_chars: int = _DEFAULT_MAX_CHARS,
    page_range: str | None = None,
    *,
    ctx: Any = None,
) -> dict[str, Any]:
    """Fetch a single web page or PDF by URL and return its extracted text.

    Never raises; returns {"error": "<short reason>"} on any failure.
    """
    if not url or not isinstance(url, str):
        return {"error": "url is required"}

    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        max_chars = _DEFAULT_MAX_CHARS
    if max_chars <= 0:
        max_chars = _DEFAULT_MAX_CHARS

    current = url
    seen_hops = 0

    while True:
        parsed = urlparse(current)
        scheme = (parsed.scheme or "").lower()
        if scheme not in _ALLOWED_SCHEMES:
            return {"error": f"unsupported scheme: {scheme or '(none)'}"}
        host = parsed.hostname
        if not host:
            return {"error": "no host in url"}

        # SSRF check BEFORE any network call to this host.
        if _host_is_blocked(host):
            return {"error": f"blocked host (resolves to a private/reserved address): {host}"}

        try:
            resp = _http_get(current)
        except requests.exceptions.Timeout:
            return {"error": "timeout"}
        except Exception as e:
            return {"error": f"fetch error: {type(e).__name__}"}

        status = getattr(resp, "status_code", None)

        # Manual redirect handling.
        if status in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            try:
                resp.close()
            except Exception:
                pass
            if not location:
                return {"error": f"redirect ({status}) with no Location"}
            seen_hops += 1
            if seen_hops > _MAX_REDIRECTS:
                return {"error": "too many redirects"}
            current = urljoin(current, location)
            continue  # loop re-runs the SSRF host check on the new host

        if status is not None and status >= 400:
            try:
                resp.close()
            except Exception:
                pass
            return {"error": f"http {status}"}

        # Terminal response.
        final_url = current
        ctype = _normalize_content_type(resp.headers.get("Content-Type"))

        body = _read_body(resp)
        try:
            resp.close()
        except Exception:
            pass
        if isinstance(body, dict):  # overflow error
            return body

        # Content-type allowlist, with a %PDF magic-byte backstop for generic types.
        is_pdf = ctype == "application/pdf"
        if not is_pdf and body[:5] == b"%PDF-" and ctype in (
            "",
            "application/octet-stream",
            "binary/octet-stream",
        ):
            is_pdf = True
            ctype = "application/pdf"

        if not is_pdf and ctype not in _ALLOWED_CONTENT_TYPES:
            return {"error": f"unsupported content-type: {ctype or '(none)'}"}

        if is_pdf:
            pdf = _extract_pdf(body, page_range)
            if "error" in pdf:
                return pdf
            full_text = pdf["text"]
            pages = pdf["pages"]
            pages_returned = pdf["pages_returned"]
            content_type = "application/pdf"
            title = None
        elif ctype == "text/html":
            full_text, title = _extract_html(body)
            pages = None
            pages_returned = None
            content_type = "text/html"
        else:  # text/plain
            full_text = body.decode("utf-8", errors="replace").strip()
            title = None
            pages = None
            pages_returned = None
            content_type = "text/plain"

        total_chars = len(full_text)
        truncated = total_chars > max_chars
        text = full_text[:max_chars] if truncated else full_text

        return {
            "url": url,
            "final_url": final_url,
            "content_type": content_type,
            "title": title,
            "text": text,
            "truncated": truncated,
            "total_chars": total_chars,
            "pages": pages,
            "pages_returned": pages_returned,
        }
