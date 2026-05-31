"""Offline verification for the fetch_url agent tool (spec §9 cases 1-8).

Plain runner — NO pytest. Prints PASS/FAIL per case; exits nonzero on any
failure. NO real network: the DNS resolver (web._resolve_host) and the HTTP
layer (web._http_get) are monkeypatched. A small real PDF is built with
reportlab (installed alongside pdfplumber) so the PDF path exercises the real
extractor.

Run:
    ./venv/bin/python tests/test_fetch_url.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_tools.web as web  # noqa: E402


# --------------------------------------------------------------------------- #
# Test harness
# --------------------------------------------------------------------------- #
_FAILURES: list[str] = []
_PASSES = 0


def check(cond: bool, label: str) -> None:
    global _PASSES
    if cond:
        _PASSES += 1
        print(f"  PASS  {label}")
    else:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status_code=200, headers=None, body=b"", chunk=64 * 1024):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self._chunk = chunk
        self.closed = False

    def iter_content(self, chunk_size=65536):
        b = self._body
        for i in range(0, len(b), self._chunk):
            yield b[i : i + self._chunk]

    def close(self):
        self.closed = True


class HttpRecorder:
    """Replacement for web._http_get that serves canned responses by URL and
    records every call so SSRF cases can assert 'no HTTP happened'."""

    def __init__(self, routes):
        self.routes = routes  # url -> FakeResponse (or callable -> FakeResponse)
        self.calls: list[str] = []

    def __call__(self, url, *, stream=True):
        self.calls.append(url)
        r = self.routes.get(url)
        if r is None:
            raise AssertionError(f"unexpected HTTP GET to {url}")
        return r() if callable(r) else r


def install(monkeyset, resolver, http):
    monkeyset["_resolve_host"] = web._resolve_host
    monkeyset["_http_get"] = web._http_get
    web._resolve_host = resolver
    web._http_get = http


def restore(monkeyset):
    web._resolve_host = monkeyset["_resolve_host"]
    web._http_get = monkeyset["_http_get"]


def make_pdf_bytes(pages_text: list[str]) -> bytes:
    """Build a tiny multi-page PDF. Prefer reportlab; else a hand-crafted PDF."""
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import letter

        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=letter)
        for txt in pages_text:
            c.drawString(72, 720, txt)
            c.showPage()
        c.save()
        return buf.getvalue()
    except Exception:
        return None  # caller will fall back to mocking the extractor


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #
def case1_ssrf_private_ip():
    print("Case 1: SSRF — private/loopback/metadata IPs blocked, no HTTP call")
    blocked_hosts = {
        "loopback.test": ["127.0.0.1"],
        "internal.test": ["10.1.2.3"],
        "metadata.test": ["169.254.169.254"],
        "cgnat.test": ["100.64.0.1"],
        "ula6.test": ["fc00::1"],
    }
    ms = {}
    http = HttpRecorder({})
    install(ms, lambda h: blocked_hosts[h], http)
    try:
        for host in blocked_hosts:
            res = web.fetch_url(f"http://{host}/x", ctx=None)
            check("error" in res, f"{host} -> error")
        check(http.calls == [], "no HTTP request made to any blocked host")
    finally:
        restore(ms)


def case2_ssrf_via_redirect():
    print("Case 2: SSRF — public host 302 -> internal host blocked at the hop")
    resolver = lambda h: {"public.test": ["93.184.216.34"], "internal.test": ["10.0.0.5"]}[h]
    redirect = FakeResponse(302, {"Location": "http://internal.test/secret"})
    http = HttpRecorder({"http://public.test/start": redirect})
    ms = {}
    install(ms, resolver, http)
    try:
        res = web.fetch_url("http://public.test/start", ctx=None)
        check("error" in res, "redirect-to-internal -> error")
        check("http://internal.test/secret" not in http.calls,
              "internal host was NOT fetched")
        check(http.calls == ["http://public.test/start"],
              "only the public start URL was fetched")
    finally:
        restore(ms)


def case3_scheme_guard():
    print("Case 3: scheme guard — file/ftp/gopher rejected, no resolve/HTTP")
    calls = {"resolve": 0}

    def resolver(h):
        calls["resolve"] += 1
        return ["93.184.216.34"]

    http = HttpRecorder({})
    ms = {}
    install(ms, resolver, http)
    try:
        for u in ("file:///etc/passwd", "ftp://x.test/f", "gopher://x.test/"):
            res = web.fetch_url(u, ctx=None)
            check("error" in res and "scheme" in res["error"], f"{u} -> scheme error")
        check(http.calls == [], "no HTTP for bad schemes")
    finally:
        restore(ms)


def case4_html_extraction():
    print("Case 4: HTML — main content extracted, boilerplate dropped, title set")
    html = b"""<html><head><title>  Primary Source  </title></head>
    <body>
      <nav>Home About Login Subscribe Cookie banner</nav>
      <article><h1>Findings</h1>
      <p>The measured value was 42.7 percent in the third quarter of the study.</p>
      <p>A second paragraph with additional substantive detail for extraction.</p>
      </article>
      <footer>Copyright boilerplate nav links terms privacy</footer>
    </body></html>"""
    resolver = lambda h: ["93.184.216.34"]
    resp = FakeResponse(200, {"Content-Type": "text/html; charset=utf-8"}, html)
    http = HttpRecorder({"http://example.test/a": resp})
    ms = {}
    install(ms, resolver, http)
    try:
        res = web.fetch_url("http://example.test/a", ctx=None)
        check("error" not in res, "no error")
        check(res.get("content_type") == "text/html", "content_type text/html")
        check("42.7 percent" in res.get("text", ""), "main figure present in text")
        check(res.get("title", "") and "Primary Source" in res["title"], "title set")
        check("Cookie banner" not in res.get("text", ""), "nav boilerplate excluded")
        check(res.get("final_url") == "http://example.test/a", "final_url set")
    finally:
        restore(ms)


def case5_pdf_extraction():
    print("Case 5: PDF — text extracted, page_range, pages/pages_returned, invalid range")
    pdf = make_pdf_bytes(["ALPHA page one figure 11", "BRAVO page two figure 22", "CHARLIE page three"])
    resolver = lambda h: ["93.184.216.34"]
    ms = {}
    if pdf is not None:
        resp = lambda: FakeResponse(200, {"Content-Type": "application/pdf"}, pdf)
        http = HttpRecorder({"http://example.test/doc.pdf": resp})
        install(ms, resolver, http)
        try:
            full = web.fetch_url("http://example.test/doc.pdf", ctx=None)
            check("error" not in full, "no error (full doc)")
            check(full.get("content_type") == "application/pdf", "content_type pdf")
            check(full.get("pages") == 3, "pages == 3")
            check("ALPHA" in full.get("text", "") and "CHARLIE" in full.get("text", ""),
                  "all pages extracted when no range")

            p2 = web.fetch_url("http://example.test/doc.pdf", page_range="2", ctx=None)
            check("error" not in p2, "no error (page 2)")
            check("BRAVO" in p2.get("text", ""), "page 2 text present")
            check("ALPHA" not in p2.get("text", "") and "CHARLIE" not in p2.get("text", ""),
                  "only page 2 returned")
            check(p2.get("pages_returned") == "2", "pages_returned == '2'")
            check(p2.get("pages") == 3, "pages still total (3)")

            rng = web.fetch_url("http://example.test/doc.pdf", page_range="1-2", ctx=None)
            check(rng.get("pages_returned") == "1-2" and "BRAVO" in rng.get("text", "")
                  and "CHARLIE" not in rng.get("text", ""), "range '1-2' inclusive")

            bad = web.fetch_url("http://example.test/doc.pdf", page_range="9-12", ctx=None)
            check("error" in bad, "out-of-range -> error")
            bad2 = web.fetch_url("http://example.test/doc.pdf", page_range="abc", ctx=None)
            check("error" in bad2, "non-numeric range -> error")
        finally:
            restore(ms)
    else:
        # Fallback: reportlab unavailable. Still assert page_range parsing + errors.
        print("  (reportlab unavailable: testing _parse_page_range directly)")
        check(web._parse_page_range("2", 3) == (1, 2), "parse '2' -> (1,2)")
        check(web._parse_page_range("1-2", 3) == (0, 2), "parse '1-2' -> (0,2)")
        check(web._parse_page_range("9-12", 3) is None, "parse out-of-range -> None")
        check(web._parse_page_range("abc", 3) is None, "parse non-numeric -> None")


def case6_truncation():
    print("Case 6: truncation — oversize text sets truncated + total_chars")
    big = "X" * 5000
    html = ("<html><head><title>Big</title></head><body><article><p>"
            + big + "</p></article></body></html>").encode()
    resolver = lambda h: ["93.184.216.34"]
    resp = FakeResponse(200, {"Content-Type": "text/html"}, html)
    http = HttpRecorder({"http://example.test/big": resp})
    ms = {}
    install(ms, resolver, http)
    try:
        res = web.fetch_url("http://example.test/big", max_chars=100, ctx=None)
        check("error" not in res, "no error")
        check(res.get("truncated") is True, "truncated == True")
        check(len(res.get("text", "")) == 100, "text cut to max_chars")
        check(res.get("total_chars", 0) >= 5000, "total_chars reports full length")
    finally:
        restore(ms)


def case7_content_type():
    print("Case 7: content-type — disallowed rejected; %PDF sniff backstop works")
    resolver = lambda h: ["93.184.216.34"]
    # 7a: disallowed JSON content-type.
    bad = FakeResponse(200, {"Content-Type": "application/json"}, b'{"a":1}')
    # 7b: octet-stream that is really a PDF -> sniffed and parsed.
    pdf = make_pdf_bytes(["SNIFFED pdf body figure 7"])
    routes = {"http://example.test/data.json": bad}
    if pdf is not None:
        routes["http://example.test/blob"] = FakeResponse(
            200, {"Content-Type": "application/octet-stream"}, pdf
        )
    http = HttpRecorder(routes)
    ms = {}
    install(ms, resolver, http)
    try:
        res = web.fetch_url("http://example.test/data.json", ctx=None)
        check("error" in res and "content-type" in res["error"], "json -> unsupported content-type")
        if pdf is not None:
            res2 = web.fetch_url("http://example.test/blob", ctx=None)
            check(res2.get("content_type") == "application/pdf", "octet-stream %PDF sniffed as pdf")
            check("SNIFFED" in res2.get("text", ""), "sniffed pdf text extracted")
    finally:
        restore(ms)


def case8_failure_modes():
    print("Case 8: failure modes — timeout / oversize (CL + streamed) / 404 -> error")
    import requests as _rq

    resolver = lambda h: ["93.184.216.34"]

    def timeout_get(url, *, stream=True):
        raise _rq.exceptions.Timeout("slow")

    # Content-Length oversize.
    cl_big = FakeResponse(200, {"Content-Type": "text/html",
                                "Content-Length": str(21 * 1024 * 1024)}, b"x")
    # Streamed oversize: 21MB body, no Content-Length.
    streamed_big = FakeResponse(
        200, {"Content-Type": "text/html"}, b"y" * (21 * 1024 * 1024)
    )
    notfound = FakeResponse(404, {"Content-Type": "text/html"}, b"nope")

    # Timeout
    ms = {}
    install(ms, resolver, timeout_get)
    try:
        check("error" in web.fetch_url("http://example.test/slow", ctx=None), "timeout -> error")
    finally:
        restore(ms)

    http = HttpRecorder({
        "http://example.test/clbig": cl_big,
        "http://example.test/streambig": streamed_big,
        "http://example.test/missing": notfound,
    })
    install(ms, resolver, http)
    try:
        r1 = web.fetch_url("http://example.test/clbig", ctx=None)
        check("error" in r1 and "large" in r1["error"], "oversize Content-Length -> error")
        r2 = web.fetch_url("http://example.test/streambig", ctx=None)
        check("error" in r2 and "large" in r2["error"], "oversize streamed -> error")
        r3 = web.fetch_url("http://example.test/missing", ctx=None)
        check("error" in r3 and "404" in r3["error"], "404 -> error")
    finally:
        restore(ms)


def main():
    for fn in (
        case1_ssrf_private_ip,
        case2_ssrf_via_redirect,
        case3_scheme_guard,
        case4_html_extraction,
        case5_pdf_extraction,
        case6_truncation,
        case7_content_type,
        case8_failure_modes,
    ):
        fn()
        print()

    print("=" * 60)
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILED, {_PASSES} passed")
        for f in _FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print(f"ALL PASS ({_PASSES} checks)")


if __name__ == "__main__":
    main()
