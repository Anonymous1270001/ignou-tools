"""ignou-tools — MCP server over a local IGNOU previous-year-question-paper index.

Tools are deliberately terse: one call = one download job, one short line back.
"""

import datetime
import hashlib
import os
import re
import sqlite3
import time

import requests
from mcp.server.mcpserver import MCPServer

import assignments
import books
import indexer
import paths

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = indexer.DB_PATH
DEFAULT_ROOT = os.path.join(paths.documents_dir(), "IGNOU PYQs")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ignou-tools/1.0"}
MON_ABBR = {6: "Jun", 12: "Dec"}

mcp = MCPServer(
    "ignou-tools",
    instructions=(
        "IGNOU term-end question papers (PYQs), official study material (SLM books "
        "from eGyanKosh) and assignments (TMA). Course codes accept any form: BECC-104, "
        "becc104, becc 104. Use download_papers for PYQs, download_books for books and "
        "download_assignments for assignments; each is one call and returns one short "
        "line."
    ),
)


# ---------------------------------------------------------------- index access
def db():
    if not os.path.exists(DB_PATH):
        indexer.build()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    return con


def resolve(course):
    key = indexer.norm(course)
    con = db()
    rows = con.execute(
        "SELECT * FROM papers WHERE key = ? ORDER BY year, month", (key,)
    ).fetchall()
    if not rows:
        rows = con.execute(
            "SELECT * FROM papers WHERE key LIKE ? ORDER BY code, year, month",
            (f"%{key}%",),
        ).fetchall()
        codes = {r["code"] for r in rows}
        if len(codes) > 1:
            rows = [r for r in rows if r["code"] == sorted(codes)[0]]
    con.close()
    return rows


def label(row):
    return f"{MON_ABBR[row['month']]} {row['year']}"


def span(rows):
    return f"{label(rows[0])} – {label(rows[-1])}" if rows else "none"


# ---------------------------------------------------------------------- tools
@mcp.tool()
def find_papers(course: str) -> str:
    """List which exam sessions have a paper for a course code.

    Use this only to check availability; download_papers already resolves codes.
    """
    rows = resolve(course)
    if not rows:
        return f"{course}: not in index (try refresh_index, or check the code)"
    code = rows[0]["code"]
    years = sorted({r["year"] for r in rows})
    return (
        f"{code}: {len(rows)} sessions ({span(rows)}); years {' '.join(map(str, years))}"
    )


@mcp.tool()
def get_paper_url(course: str, session: str = "") -> str:
    """Exact PDF URL for one session, e.g. session='Jun 2024' (default: newest)."""
    rows = resolve(course)
    if not rows:
        return f"{course}: not in index"
    if session:
        want = indexer.parse_session(session)
        rows = [r for r in rows if want and r["session"] == want[0]] or rows[-1:]
    r = rows[-1]
    return f"{r['code']} {r['session']} [{r['backend']}/{r['bucket']}] {r['url']}"


@mcp.tool()
def download_papers(
    course: str,
    years: int = 6,
    session: str = "both",
    dest: str = "",
    overwrite: bool = False,
) -> str:
    """Download a course's PYQs and save them as cleanly named PDFs.

    course: BECC-104 / becc104. years: how many calendar years back (default 6).
    session: both | june | december. dest: folder (default Documents/IGNOU PYQs/<CODE>).
    """
    rows = resolve(course)
    if not rows:
        return f"{course}: not in index — run refresh_index first"
    code = rows[0]["code"]

    cutoff = datetime.date.today().year - max(1, int(years)) + 1
    rows = [r for r in rows if r["year"] >= cutoff]

    s = (session or "both").strip().lower()
    if s.startswith("jun"):
        rows = [r for r in rows if r["month"] == 6]
    elif s.startswith("dec"):
        rows = [r for r in rows if r["month"] == 12]

    if not rows:
        return f"{code}: no sessions match (years={years}, session={session})"

    folder = dest or os.path.join(DEFAULT_ROOT, re.sub(r"[^\w.-]", "_", code))
    os.makedirs(folder, exist_ok=True)

    S = requests.Session()
    S.headers.update(UA)
    new = skipped = 0
    failed = []
    con = db()

    for r in rows:
        name = f"{code}_PYQ_{MON_ABBR[r['month']]}-{r['year']}.pdf"
        path = os.path.join(folder, name)
        if not overwrite and _valid(path):
            skipped += 1
            continue
        try:
            ok = _fetch(S, r["url"], path)
        except Exception as e:
            ok = False
            failed.append(f"{name} ({type(e).__name__})")
        if ok:
            new += 1
            con.execute(
                "UPDATE papers SET sha256=?, fetched_at=? WHERE code=? AND session=?",
                (_sha(path), time.strftime("%Y-%m-%d %H:%M:%S"), code, r["session"]),
            )
        else:
            failed.append(name)
        time.sleep(0.4)

    con.commit()
    con.close()

    head = (
        f"{code}: {new + skipped}/{len(rows)} saved ({span(rows)}) → {folder}"
        if not failed
        else f"{code}: {new + skipped}/{len(rows)} saved ({span(rows)}) → {folder}"
    )
    tail = f"\nnew {new} · skipped {skipped} · failed {len(failed)}"
    if failed:
        tail += "\nfailed: " + ", ".join(failed[:6])
    return head + tail


@mcp.tool()
def find_books(course: str, lang: str = "english") -> str:
    """List the official IGNOU study material (SLM) blocks for a course code."""
    c = books.resolve_course(course, lang)
    if not c:
        return f"{course}: no SLM in eGyanKosh index (try refresh_index, or check the code)"
    blocks = books.blocks_of(c["handle"])
    if not blocks:
        return (f"{c['code']}: no block collections ({c['lang']}) — {c['title']}; "
                f"download_books still works (unit PDFs directly)")
    titles = " | ".join(b["title"][:34] for b in blocks[:8])
    return f"{c['code']}: {len(blocks)} blocks ({c['lang']}) — {c['title']}\n{titles}"


@mcp.tool()
def download_books(
    course: str,
    lang: str = "english",
    detail: str = "block",
    dest: str = "",
    overwrite: bool = False,
) -> str:
    """Download a course's official IGNOU study material (SLM) from eGyanKosh.

    course: BECC-104 / becc104. lang: english | hindi | urdu. detail: block (one PDF
    per block, default) or unit (per-unit PDFs). dest: folder (default
    Documents/IGNOU Books/<CODE>).
    """
    try:
        return books.download(course, lang=lang, detail=detail, dest=dest, overwrite=overwrite)
    except Exception as e:
        return f"{course}: download failed ({type(e).__name__}: {str(e)[:120]})"


@mcp.tool()
def find_assignments(course: str) -> str:
    """Map a course's assignments: session, language and exact PDF URL per row."""
    con = _ensure_assignments()
    rows = assignments.resolve(con, course)
    con.close()
    if not rows:
        return f"{course}: no assignments in index (try refresh_index, or check the code)"
    code = rows[0]["code"]
    head = (f"{code}: {len(rows)} files · {len({r['session'] for r in rows})} sessions "
            f"({rows[0]['session']} – {rows[-1]['session']})")
    lines = [f"{r['session']} · {r['lang']} · {r['url']}" for r in rows]
    note = ""
    if indexer.norm(course) not in {indexer.norm(r["code"]) for r in rows}:
        note = (f"\nnote: {course} has no course-specific file — these are "
                f"programme-level ({code}) booklets covering several courses")
    return head + note + "\n" + "\n".join(lines)


@mcp.tool()
def get_assignment_url(course: str, session: str = "", lang: str = "english") -> str:
    """Exact assignment PDF URL for one session (default: newest) and language."""
    con = _ensure_assignments()
    rows = assignments.resolve(con, course)
    con.close()
    if not rows:
        return f"{course}: no assignments in index"
    if session:
        want = session.strip().lower()
        rows = [r for r in rows if want in r["session"].lower()] or rows[-1:]
    l = (lang or "both").strip().lower()
    if not l.startswith("both"):
        want_lang = "hindi" if l.startswith(("hi", "hin")) else "english"
        rows = [r for r in rows if r["lang"] == want_lang] or \
               [r for r in rows if r["lang"] == "unknown"] or rows
    r = rows[-1]
    return f"{r['code']} {r['session']} [{r['lang']}] {r['url']}"


@mcp.tool()
def download_assignments(
    course: str,
    session: str = "latest",
    lang: str = "both",
    dest: str = "",
    overwrite: bool = False,
) -> str:
    """Download a course's IGNOU assignments (TMA) as cleanly named PDFs.

    course: BECC-104 / becc104. session: latest (default) | all | 'Jul 2025'.
    lang: both | english | hindi. dest: folder (default
    Documents/IGNOU Assignments/<CODE>).
    """
    _ensure_assignments().close()
    try:
        return assignments.download(course, session=session, lang=lang,
                                    dest=dest, overwrite=overwrite)
    except Exception as e:
        return f"{course}: download failed ({type(e).__name__}: {str(e)[:120]})"


@mcp.tool()
def refresh_index() -> str:
    """Re-crawl IGNOU and eGyanKosh and rebuild all three local indexes (1-4 minutes)."""
    before = _count()
    n, sessions = indexer.build()
    n_codes, _, n_blocks = books.build()
    a_codes, a_files, a_sessions = assignments.build()
    return (
        f"index rebuilt: {before} → {n} papers, {sessions} sessions; "
        f"books {n_codes} codes, {n_blocks} blocks; "
        f"assignments {a_codes} codes, {a_files} files, {a_sessions} sessions"
    )


# --------------------------------------------------------------------- helpers
def _count():
    try:
        con = sqlite3.connect(DB_PATH)
        n = con.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        con.close()
        return n
    except Exception:
        return 0


def _ensure_assignments():
    """Return an open connection, building the assignment index on first use."""
    con = db()
    try:
        n = con.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
    except sqlite3.OperationalError:
        n = 0
    if not n:
        con.close()
        assignments.build()
        con = db()
    return con


def _valid(path):
    try:
        if os.path.getsize(path) < 20_000:
            return False
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(session, url, path):
    tmp = path + ".part"
    r = session.get(url, timeout=120, stream=True)
    r.raise_for_status()
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
    if os.path.getsize(tmp) < 20_000:
        os.remove(tmp)
        return False
    with open(tmp, "rb") as f:
        if f.read(5) != b"%PDF-":
            os.remove(tmp)
            return False
    os.replace(tmp, path)
    return True


if __name__ == "__main__":
    mcp.run("stdio")
