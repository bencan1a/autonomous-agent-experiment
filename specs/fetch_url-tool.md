# Tool Spec — `fetch_url` (retrieve & read HTML/PDF primary sources)

**Status:** IMPLEMENTED (2026-05-31, commit 31e39e9). Tool live in v2/v3/v4
toolsets; capability_request id=1 marked granted. Takes effect at v4's next wake.
**Origin:** v4-continuous capability_request (session 1) — `web_search` returns only
snippets, so the agent cannot verify figures against primary PDFs/papers; it is
forced to label numbers "snippet-sourced, unverified." This closes that gap.

---

## 1. Purpose & scope

Add one runtime tool, `fetch_url`, that retrieves a single URL and returns extracted
**text** for both **HTML** and **PDF** documents. It lets the agent read the primary
sources `web_search` already surfaces, so it can cite exact figures. Out of scope:
JavaScript execution, login/auth, crawling/link-following, non-text assets.

## 2. Placement (mirror `web_search` exactly)

- Handler `fetch_url(...)` added to `agent_tools/web.py`; registered in
  `agent_tools/registry.py` `_HANDLERS`.
- A `fetch_url` spec dict added to the v1 `TOOLS_SPEC` list **and** the name added to
  the shared `_V2_KEEP` set, so **v2 / v3 / v4 all inherit it** via the existing
  spec-assembly. (Only v4 is active; v1's list-based set gets it directly.)
- `build_v4_system_prompt` "WHAT YOU CAN DO" list updated to describe it accurately
  (Path-A wording: "fetch a web page or PDF by URL and read its text" — no claim of
  JS execution, login, or arbitrary system access).
- Never raises: returns `{"error": "..."}` on any failure, same contract as
  `web_search`.

## 3. Signature & return

```
fetch_url(url: str,
          max_chars: int = 30000,
          page_range: str | None = None,   # PDF only, e.g. "3-6" or "5"
          *, ctx) -> dict
```

**Success:**
```json
{
  "url": "<requested>",
  "final_url": "<after redirects>",
  "content_type": "text/html" | "application/pdf" | "text/plain",
  "title": "<best-effort>",
  "text": "<extracted, <= max_chars>",
  "truncated": true|false,
  "total_chars": <int, full length before truncation>,
  "pages": <int|null>,            // PDF: total pages in the document
  "pages_returned": "<str|null>"  // PDF: the page_range actually extracted
}
```
**Failure:** `{"error": "<short reason>"}` (blocked host, disallowed content-type,
timeout, oversize, fetch error, parse error).

## 4. Extraction

- **HTML →** `trafilatura` (Apache-2.0) main-content extraction (strips nav/boilerplate
  so figures and table text survive). Fallback to a plain text-strip if trafilatura
  returns empty. `title` from the document `<title>` / metadata.
- **PDF →** `pdfplumber` (MIT) — text + reasonable table fidelity (chosen for *exact
  figures*, which often live in tables).
  - `page_range`: `"N"` (single), `"N-M"` (inclusive), 1-indexed. Invalid/empty →
    `{"error": ...}`. Omitted → start of document up to the page cap (§6).
  - Report `pages` (total) and `pages_returned`.

## 5. Truncation (transcript hygiene)

- Default `max_chars = 30000`. If the extracted text exceeds it, return the first
  `max_chars`, set `truncated=true`, and report `total_chars` so the agent knows there
  is more and can re-fetch a narrower `page_range` (PDF) or raise `max_chars`.
- Rationale: the running transcript is prompt-cached and grows; an unbounded dump would
  inflate cost and crowd context.

## 6. Safety — SSRF hard block (load-bearing)

`fetch_url` takes an agent-chosen URL and runs on the host alongside the dashboard
(:8081), cron, and other local services. The guard is the security boundary (there is
**no per-fetch human approval**, consistent with the autonomy design):

- **Scheme allowlist:** `http`, `https` only. Anything else → error.
- **Hard-block private/loopback/link-local/reserved IPs.** Resolve the hostname; if
  *any* resolved address is in a blocked range, reject:
  - IPv4: `127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`,
    `169.254.0.0/16` (incl. cloud-metadata `169.254.169.254`), `0.0.0.0/8`,
    `100.64.0.0/10`, multicast/reserved.
  - IPv6: `::1`, `fc00::/7` (ULA), `fe80::/10` (link-local), IPv4-mapped equivalents.
  - This also blocks `localhost`, `localhost:8081` (the dashboard), and any internal
    service by side effect. **Ben's decision: hard block, no internal host reachable.**
- **Redirects:** do NOT auto-follow. Follow manually, max 5 hops, and **re-run the
  IP block check on every hop's host** (a public URL can 302 into an internal one).
  Return the terminal URL as `final_url`.
- **Timeout:** 20s total.
- **Download cap:** **20 MB** (Ben's decision — doubled from the 10 MB default).
  Stream the body; abort + error if exceeded (and honor a too-large `Content-Length`
  up front).
- **Content-type allowlist:** `text/html`, `application/pdf`, `text/plain`. Anything
  else → `{"error": "unsupported content-type: <ct>"}`. (Sniff PDF by magic bytes
  `%PDF` as a backstop when the header is generic like `application/octet-stream`.)

## 7. Dependencies

Add to `requirements.txt` and install into `venv`:
- `pdfplumber` (MIT) — PDF text/tables. Pulls `pdfminer.six`.
- `trafilatura` (Apache-2.0) — HTML main-content extraction. Pulls `lxml`.
- `requests` (already present) — the HTTP fetch, used with manual redirect handling
  and streaming for the size cap.

Both new deps are license-compatible with the public MIT repo. No AGPL (explicitly
avoided PyMuPDF).

## 8. Capability-loop closure

- Add a small `resolve_capability_request(id, status="granted", note=...)` helper to
  `memory/episodic.py` (the table + `pending_capability_requests()` already exist; the
  dashboard shows pending ones).
- On deploy: mark the originating request **granted** and post a one-line grant note to
  the v4 `-chat` channel, so on next wake the agent sees both the new tool (in its
  schema + prompt) and the acknowledgement.

## 9. Tests (mocked; `tests/test_fetch_url.py`, plain runner, no live spend)

1. SSRF: a private-IP host (e.g. resolves to `127.0.0.1` / `10.x` / `169.254.169.254`)
   → `{"error": ...}`, no network call to it.
2. SSRF via redirect: public host 302 → internal host is blocked at the hop check.
3. Scheme guard: `file://`, `ftp://`, `gopher://` → error.
4. HTML: main-content text extracted; nav/boilerplate excluded; `title` set.
5. PDF: text extracted from a tiny fixture; `page_range="2"` returns only that page;
   `pages` / `pages_returned` correct; invalid range → error.
6. Truncation: oversize text sets `truncated=true` and correct `total_chars`.
7. Content-type: disallowed type and `%PDF`-sniff backstop both behave.
8. Failure modes: timeout, oversize (Content-Length and streamed), and a 404 each
   return `{"error": ...}` rather than raising.

## 10. Rollout

- Tools load at **session start**, so this takes effect at the v4 agent's **next wake**
  (after the current session's ~4h sleep) — no mid-session disruption; arrives as a
  capability "granted between sessions."
- All versions inherit it; only v4 is active.
- Dashboard needs no change (tool calls render generically). Optionally note the grant
  in `experiments/v4-continuous.md` changelog.

## 11. Decisions recorded

- PDF lib: **pdfplumber** ✓
- SSRF: **hard block** all private/loopback/link-local/reserved; no internal host
  reachable ✓
- Download cap: **20 MB** ✓
- `page_range`: **included** ✓
- `max_chars` default: **30000** ✓

## 12. Prerequisite

Ben has a small side-task to do **before** implementation. Do not build until that is
done and Ben gives the go.
