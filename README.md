# ignou-tools

MCP server that resolves IGNOU course codes to term-end question papers (PYQs),
official study material (SLM books) **and** assignments (TMA), then downloads them as
cleanly named PDFs.

One call does the whole job — no per-session lookups, no guessed URLs, minimal tokens:

```
download_papers(course="becc104", years=6)
→ BECC-104: 10/10 saved (Dec 2021 – Jun 2026) → %USERPROFILE%\Documents\IGNOU PYQs\BECC-104
  new 10 · skipped 0 · failed 0

download_books(course="becc107")
→ BECC-107: 4/4 files (4 blocks, english) → %USERPROFILE%\Documents\IGNOU Books\BECC-107
  new 4 · skipped 0 · failed 0

download_assignments(course="becc108", session="all")
→ BECC-108: 4/4 saved (Jul 2024 – Jul 2025) → %USERPROFILE%\Documents\IGNOU Assignments\BECC-108
  new 4 · skipped 0 · failed 0
```

## Tools

| Tool | Purpose |
|---|---|
| `download_papers(course, years=6, session="both", dest="", overwrite=False)` | PYQs: resolve, filter, download, verify, name |
| `find_papers(course)` | which exam sessions exist (one line) |
| `get_paper_url(course, session="")` | exact PYQ PDF URL |
| `download_books(course, lang="english", detail="block", dest="", overwrite=False)` | SLM books from eGyanKosh |
| `find_books(course, lang="english")` | which blocks exist (one line + titles) |
| `download_assignments(course, session="latest", lang="both", dest="", overwrite=False)` | assignments (TMA) |
| `find_assignments(course)` | the session × language × URL map for one course |
| `get_assignment_url(course, session="", lang="english")` | exact assignment PDF URL |
| `refresh_index()` | re-crawl every source and rebuild the indexes |

`session` accepts `both`, `june`, `december` for papers; `latest`, `all` or a label such
as `Jul 2025` for assignments. `years` is calendar years back from today.
`lang` accepts `english`, `hindi`, `urdu` for books and `both`, `english`, `hindi` for
assignments. `detail` accepts `block` (one PDF per block, default) or `unit` (per-unit
PDFs, nested per block folder).

Downloads land in your real Documents folder — Windows folder redirection, including
OneDrive Known Folder Move, is honoured. Pass `dest=` to write somewhere else.

## Install

```sh
pip install -r requirements.txt
python indexer.py          # build ignou.db papers from live IGNOU (1-3 min)
python books.py            # build the eGyanKosh book index (one request, ~10 s)
python assignments.py      # build the assignment index (~160 pages, ~1 min)
```

Registered in `~/.config/opencode/opencode.json`:

```json
"ignou-tools": {
  "type": "local",
  "command": ["<python.exe>", "C:\\path\\to\\ignou-tools\\server.py"]
}
```

## Sources

**PYQs** — two backends, both scraped into one table:

- **archive** `webservices.ignou.ac.in/Pre-Question/` — June 2014 → June 2024
- **portal** `ignou.ac.in/studentService/download/questionPapers/` — Dec 2024 → June 2026

**Books** — eGyanKosh, IGNOU's official SLM repository (`egyankosh.ac.in`):

```
course community   /handle/<id>     "BECC-104 Mathematical Methods in Economics-II"
  block collection /handle/<id>     "Block-1 Functions of Several Variables"
    item           /handle/<id>     full block PDF and/or per-unit PDFs
      bitstream    /bitstream/<item>/<seq>/<name>.pdf
```

**Assignments** — the assignment archive, scraped three levels deep:

```
/assignments/                      7 programme groups
  Bachelor-Degree/Bachelor.htm
    cbcs/ba-Jul25-Jan26.htm        one page per programme × session
      cbcs/2025-26/BECC-108 2025-26 English.pdf
```

Session is inferred from the PDF path, then the page path: a month name next to a year
(`July 2024-Jan 2025`), an academic year (`2025-26`), a bare year folder (`bsc/2025/`)
or a compact page name (`FYUG-BAM24-25`). Language comes from the filename
(`English`/`Hindi`); files that do not advertise one stay `unknown` and are used as the
fallback when a specific language is requested.

## Index

SQLite `ignou.db`:

```
papers(code, key, backend, session, year, month, url, bucket, sha256, fetched_at)
book_courses(handle, code, key, title, lang, school, chain, nblocks, built_at)
book_blocks(course_handle, seq, handle, title)
book_items(block_handle, item_handle, title, kind, url, size, sha256, fetched_at)
assignments(code, key, session, year, month, lang, url, page, sha256, fetched_at)
```

`key` is the course code stripped to `[A-Z0-9]`, so `BECC-104`, `becc104` and `BECC 104`
all hit the same rows.

Coverage:

- **papers** — 39,096 papers across 21 sessions (Jun 2014 → Jun 2026)
- **books** — 3,223 course codes, 6,406 course entries (English/Hindi/Urdu copies),
  21,542 blocks. Built from a single 6 MB `/community-list` page.
- **assignments** — 1,581 course codes, 4,429 PDFs across 36 sessions
  (Jul 2009 → Jul 2026). Built from 160 pages in one crawl. Codes include
  programme-level booklets (`MSO-1Y`, `MSO-2Y`, `BCA-S3`) that cover several courses at
  once; `MSO-001` resolves to the MSO booklets, and asking for `BCA` returns every
  semester. Programmes whose booklets use a different prefix from their course codes
  (BCA publishes `BCS-*` courses) must be requested by programme code.

## Why indexes and not live lookups

The PYQ portal is fully server-rendered (no JSON API, no form actions, ~2.6k table rows
per session), so there is nothing to call. eGyanKosh runs DSpace 6 with its REST API
disabled (`/rest/status` → 404) but publishes the whole course → block tree on one page.

The two sources get different treatment on purpose:

- **papers**: fully enumerated up front (21 sessions × ~2k rows is cheap).
- **books**: the course → block mapping is enumerated (one request), but unit/PDF links
  are resolved **lazily per course** and cached. Crawling all 21k block pages up front
  would hammer a public university server for data that is only needed per request.
  First call for a course costs ~20 requests / a few seconds; later calls are instant.

## Caveats

- **`ldd/downloads/` is a generic bucket.** Dec 2024 papers live there while every other
  session has a session-named bucket. A generic name can be overwritten; that is why every
  download stores a `sha256` and `fetched_at`.
- **The indexes are snapshots.** Run `refresh_index` when IGNOU publishes a new session.
- **PYQ coverage is uneven.** Legacy sessions are partial (1.1k–2.0k courses vs 2.4k–2.7k
  on the portal), and some courses are simply absent in some sessions.
- **Book coverage is uneven.** 1,157 of 6,406 course entries are plain collections of unit
  PDFs rather than block collections (handled transparently), and some codes have only an
  English copy — a `hindi` request then falls back to English and says so in the result.
- **eGyanKosh serves an incomplete TLS chain**, which `requests` rejects. `truststore`
  routes verification through the Windows cert store, which fetches the missing
  intermediate. Without it every request fails with `unable to verify the first certificate`.
- **Local use only.** The server downloads to the user's machine; do not expose it as a
  public mirror of IGNOU PDFs.
