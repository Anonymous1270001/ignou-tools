"""IGNOU assignment (TMA) index builder + downloader.

Crawls the assignment archive and writes a course -> session -> language map into
the shared SQLite index (ignou.db):

    assignments(code, key, session, year, month, lang, url, page, sha256, fetched_at)

Archive shape (depth is fixed, ~160 pages / ~6.3k PDFs):

    /assignments/                     landing (7 programme groups)
      <Group>/<group>.htm             e.g. Bachelor-Degree/Bachelor.htm
        <programme>/<page>.htm        e.g. cbcs/ba-Jul25-Jan26.htm
          .../<CODE> ... .pdf         English/Hindi assignment PDFs

Never prints to stdout when imported (the MCP server owns stdout).
"""

import os
import re
import sqlite3
import time
import urllib.parse as up
from concurrent.futures import ThreadPoolExecutor

import requests

import paths

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "ignou.db")

ROOT = "https://webservices.ignou.ac.in/assignments/"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ignou-tools/1.0"}
DEFAULT_ROOT = os.path.join(paths.documents_dir(), "IGNOU Assignments")

S = requests.Session()
S.headers.update(UA)

ABBR = {1: "Jan", 6: "Jun", 7: "Jul", 12: "Dec"}
MONTHS = {"jan": 1, "january": 1, "june": 6, "jun": 6,
          "jul": 7, "july": 7, "dec": 12, "december": 12}

# BECC-108 / BECC 108 / BANC-183 / ACS-01 / BCOA-001
CODE_RE = re.compile(r"\b([A-Z]{2,6})[\s_-]?(\d{2,3})\b")
STOP = {"PDF", "ASSIGNMENT", "ASSIGNMENTS", "ASST", "TMA", "BOOKLET", "JULY",
        "JANUARY", "JUNE", "DECEMBER", "ENGLISH", "HINDI", "ENG", "HIN", "YEAR",
        "SEMESTER", "MIL", "FIRST", "SECOND", "THIRD", "OLD", "NEW", "PART",
        "FINAL", "PROGRAMME", "COURSE", "SEM", "TEE", "IGNOU", "UNIVERSITY"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
    code        TEXT NOT NULL,
    key         TEXT NOT NULL,
    session     TEXT NOT NULL,
    year        INTEGER NOT NULL,
    month       INTEGER NOT NULL,
    lang        TEXT NOT NULL,
    url         TEXT NOT NULL,
    page        TEXT,
    sha256      TEXT,
    fetched_at  TEXT,
    PRIMARY KEY (code, session, lang)
);
CREATE INDEX IF NOT EXISTS idx_assign_key ON assignments(key);
"""


def norm(code):
    return re.sub(r"[^A-Z0-9]", "", code.upper())


def _get(url):
    try:
        r = S.get(url, timeout=60)
        if r.status_code == 200 and r.content:
            return r.text
    except Exception:
        pass
    return None


# ------------------------------------------------------------------ parsing
def session_of(url, page=""):
    """Infer the assignment session from the PDF path, then the page path.

    'July 2024-Jan 2025/ECONOMICS/BECC-108 2024-25 English.pdf' -> ('Jul 2024', 2024, 7)
    '2025-26/BECC-108 2025-26 English.pdf'                      -> ('Jul 2025', 2025, 7)
    'bsc/2025/BBCS-185.pdf'                                     -> ('Jul 2025', 2025, 7)
    'sopva/2023/jan/OV-002.pdf'                                 -> ('Jan 2023', 2023, 1)
    'ba-Jul25-Jan26.htm' (page)                                 -> ('Jul 2025', 2025, 7)
    'FYUG-BAM24-25.htm' (page)                                  -> ('Jul 2024', 2024, 7)
    """
    for src in (up.unquote(url), up.unquote(page)):
        low = src.replace("_", " ").lower()
        # 1. month name adjacent to a 4-digit year
        m = re.search(r"(jan(?:uary)?|june|jun|jul(?:y)?|dec(?:ember)?)"
                      r"\s*[-\s]?\s*(20\d{2})\b", low)
        # 2. month token anywhere in the path + year anywhere in the path
        if not m:
            mm = re.search(r"\b(jan(?:uary)?|june|jun|jul(?:y)?|dec(?:ember)?)\b", low)
            yy = re.search(r"\b(20\d{2})\b", low)
            if mm and yy:
                mon = MONTHS[mm.group(1)]
                return f"{ABBR[mon]} {yy.group(1)}", int(yy.group(1)), mon
        else:
            mon, yr = MONTHS[m.group(1).lower()], int(m.group(2))
            return f"{ABBR[mon]} {yr}", yr, mon
        # 3. academic year 2025-26 / 2024-2025 -> July session
        m = re.search(r"\b(20\d{2})\s*[-\u2013]\s*(?:\d{2}|20\d{2})\b", low)
        if m:
            yr = int(m.group(1))
            return f"Jul {yr}", yr, 7
        # 4. compact year pair '24-25' (page names like FYUG-BAM24-25.htm)
        m = re.search(r"(?<!\d)(\d{2})\s*[-\u2013]\s*(\d{2})(?!\d)", low)
        if m and int(m.group(2)) == int(m.group(1)) + 1:
            yr = 2000 + int(m.group(1))
            return f"Jul {yr}", yr, 7
        # 5. bare 4-digit year folder -> July session
        m = re.search(r"\b(20\d{2})\b", low)
        if m:
            yr = int(m.group(1))
            return f"Jul {yr}", yr, 7
        # 6. compact month+2-digit year (Jul25 / Jan26)
        for seg in [s for s in src.split("/") if s]:
            m = re.search(r"\b(jan|jun|jul|dec)[-_ ]?(\d{2})\b", seg, re.I)
            if m:
                mon, yr = MONTHS[m.group(1).lower()], 2000 + int(m.group(2))
                return f"{ABBR[mon]} {yr}", yr, mon
    return "unknown", 0, 0


def lang_of(text):
    t = up.unquote(text).lower()
    if re.search(r"hindi|\bhin\b|_hin|\(hin|[-_ ]h[-_. ]", t):
        return "hindi"
    if re.search(r"english|\beng\b|_eng|\(e\)|\(eng|[-_ ]e[-_. ]", t):
        return "english"
    return "unknown"


NUM = {"1": 1, "first": 1, "i": 1, "2": 2, "second": 2, "ii": 2,
       "3": 3, "third": 3, "iii": 3, "4": 4, "fourth": 4, "iv": 4,
       "5": 5, "fifth": 5, "v": 5, "6": 6, "sixth": 6, "vi": 6}


def _variant(name):
    """'MSO 1st year ...' -> '1Y', 'BCA SEMESTER-V ...' -> 'S5'."""
    t = up.unquote(name)
    # semester: 'SEMESTER-V', 'Sem 3', 'First Sem'
    m = (re.search(r"\b(?:semester|sem)\s*[-_. ]?\s*(1st|2nd|3rd|4th|5th|6th|"
                   r"first|second|third|fourth|fifth|sixth|[1-6]|I{1,3}V?|V)\b", t, re.I)
         or re.search(r"\b(1st|2nd|3rd|4th|5th|6th|first|second|third|fourth|fifth|sixth)"
                      r"\s*[-_. ]?\s*(?:semester|sem)\b", t, re.I))
    if m:
        t1 = m.group(1).lower()
        n = int(t1[0]) if t1[0].isdigit() else NUM.get(t1)
        if n:
            return f"S{n}"
    # year: '1st year', 'II year', '1 yr.'
    m = re.search(r"\b(1st|2nd|3rd|first|second|third|[123]|I{1,3}|IV)"
                  r"\s*[- ]?\s*(?:year|yr)\b", t, re.I)
    if not m:
        return ""
    token = m.group(1).lower()
    return {"1st": "1Y", "first": "1Y", "1": "1Y",
            "2nd": "2Y", "second": "2Y", "2": "2Y",
            "3rd": "3Y", "third": "3Y", "3": "3Y",
            "i": "1Y", "ii": "2Y", "iii": "3Y", "iv": "4Y"}.get(token, "")


def code_of(name, context=""):
    """Course code from the filename, else the link text.

    Falls back to a PROGRAMME-level code when a booklet covers a whole
    programme/year instead of one course (e.g. 'MSO 1st year Eng 2024-25.pdf'
    -> 'MSO-1Y'), so those files remain findable from any course in the programme.
    """
    for src in (up.unquote(name), context):
        for m in CODE_RE.finditer(src.upper()):
            letters = m.group(1)
            if letters in STOP or len(letters) < 2:
                continue
            return f"{letters}-{m.group(2)}"
    # programme-level booklet
    tokens = [t for t in re.findall(r"(?<![A-Za-z])[A-Z]{2,6}(?![A-Za-z])",
                                    up.unquote(name)) if t not in STOP]
    if not tokens:
        return None
    prog = max(tokens, key=len)
    var = _variant(name)
    return f"{prog}-{var}" if var else prog


def _text_before(html, pos, width=600):
    chunk = html[max(0, pos - width):pos]
    chunk = re.sub(r"<[^>]*>", " ", chunk)
    return re.sub(r"&nbsp;|\s+", " ", chunk).strip()


# ------------------------------------------------------------------- crawl
def crawl():
    """Return [(code, page, url)] for every assignment PDF in the archive."""
    landing = _get(ROOT)
    if not landing:
        return []
    found, seen = [], set()
    frontier = [(up.urljoin(ROOT, m), 1) for m in
                re.findall(r'href\s*=\s*"([^"#]+\.htm[l]?)"', landing, re.I)]

    while frontier:
        batch, frontier = frontier[:48], frontier[48:]
        pages = ThreadPoolExecutor(max_workers=8).map(
            lambda t: (t, _get(t[0])), batch)
        for (base, depth), html in pages:
            if not html or base in seen:
                continue
            seen.add(base)
            for m in re.finditer(r'href\s*=\s*"([^"#]+\.pdf)"', html, re.I):
                url = up.urljoin(base, m.group(1))
                name = url.rsplit("/", 1)[-1]
                code = code_of(name, _text_before(html, m.start()))
                if code:
                    found.append((code, base, url))
            if depth < 3:
                for m in re.finditer(r'href\s*=\s*"([^"#]+\.htm[l]?)"', html, re.I):
                    nxt = up.urljoin(base, m.group(1))
                    if nxt.startswith(ROOT):
                        frontier.append((nxt, depth + 1))

    # one PDF per (code, session, lang): shortest path wins, URL breaks ties
    best = {}
    for code, page, url in sorted(found, key=lambda r: (len(r[2]), r[2])):
        session, _, _ = session_of(url, page)
        lang = lang_of(url.rsplit("/", 1)[-1]) or lang_of(url)
        best.setdefault((code, session, lang), (code, page, url))
    return list(best.values())


def build(verbose=False):
    rows = crawl()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.executescript(SCHEMA)
    prev = {(c, s, l): (h, f) for c, s, l, h, f in con.execute(
        "SELECT code, session, lang, sha256, fetched_at FROM assignments")}
    con.execute("DELETE FROM assignments")
    payload = []
    for code, page, url in rows:
        session, yr, mon = session_of(url, page)
        lang = lang_of(url.rsplit("/", 1)[-1]) or lang_of(url)
        h, f = prev.get((code, session, lang), (None, None))
        payload.append((code, norm(code), session, yr, mon, lang, url, page, h, f))
    con.executemany("INSERT OR REPLACE INTO assignments VALUES (?,?,?,?,?,?,?,?,?,?)",
                    payload)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
    codes = con.execute("SELECT COUNT(DISTINCT code) FROM assignments").fetchone()[0]
    sessions = con.execute("SELECT COUNT(DISTINCT session) FROM assignments").fetchone()[0]
    con.close()
    if verbose:
        print(f"assignments: {n} files, {codes} codes, {sessions} sessions -> {DB_PATH}")
    return codes, n, sessions


# ------------------------------------------------------------------ lookups
def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    return con


def resolve(con, course):
    key = norm(course)
    sort = lambda rs: sorted(rs, key=lambda r: (r["year"], r["month"], r["lang"]))

    # letters-only query = programme, so return every course/semester/year booklet
    prog = re.match(r"[A-Z]+$", key)
    if prog:
        level = re.compile(rf"^{key}(-\dY|-?S[1-6])?$")
        rows = [r for r in con.execute("SELECT * FROM assignments")
                if level.match(r["code"])]
        if rows:
            return sort(rows)

    rows = con.execute("SELECT * FROM assignments WHERE key = ?", (key,)).fetchall()
    if not rows:
        rows = con.execute("SELECT * FROM assignments WHERE key LIKE ?",
                           (f"%{key}%",)).fetchall()
        codes = {r["code"] for r in rows}
        if len(codes) > 1:
            m = re.match(r"[A-Z]+", key)
            same = {c for c in codes if m and re.match(rf"^{m.group(0)}(-|$)", c)}
            keep = same or {sorted(codes)[0]}
            rows = [r for r in rows if r["code"] in keep]
    if not rows:
        # programme-level booklets: MSO-001 -> MSO-1Y / MSO-2Y, BCS-011 -> (none)
        m = re.match(r"[A-Z]+", key)
        if m:
            p = m.group(0)
            level = re.compile(rf"^{p}(-\dY|-?S[1-6])?$")
            rows = [r for r in con.execute(
                "SELECT * FROM assignments WHERE key LIKE ?", (p + "%",))
                if level.match(r["code"])]
    return sort(rows)


def label(row):
    return row["session"]


def _span(rows):
    lo, hi = rows[0]["session"], rows[-1]["session"]
    return lo if lo == hi else f"{lo} – {hi}"


# ----------------------------------------------------------------- download
def _valid(path):
    try:
        if os.path.getsize(path) < 20_000:
            return False
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def _sha(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(url, path):
    tmp = path + ".part"
    r = S.get(url, timeout=120, stream=True)
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


def download(course, session="latest", lang="both", dest="", overwrite=False):
    """Download assignment PDFs for one course code."""
    con = connect()
    rows = resolve(con, course)
    if not rows:
        con.close()
        return f"{course}: no assignments in index (try refresh_index, or check the code)"
    code = rows[0]["code"]

    s = (session or "latest").strip().lower()
    if s in ("latest", "newest", "current"):
        newest = max((r["year"], r["month"]) for r in rows)
        rows = [r for r in rows if (r["year"], r["month"]) == newest]
    elif s not in ("all", "both", "*"):
        rows = [r for r in rows if s in r["session"].lower()] or rows[-1:]

    # language: exact matches win; otherwise fall back to unmarked files
    l = (lang or "both").strip().lower()
    if not l.startswith("both"):
        want = "hindi" if l.startswith(("hi", "hin")) else "english"
        chosen = [r for r in rows if r["lang"] == want]
        for r in rows:
            if r["lang"] != "unknown":
                continue
            if not any(x["session"] == r["session"] for x in chosen):
                chosen.append(r)
        rows = sorted(chosen, key=lambda r: (r["year"], r["month"], r["lang"]))

    if not rows:
        con.close()
        return f"{code}: no assignments match (session={session}, lang={lang})"

    folder = dest or os.path.join(DEFAULT_ROOT, re.sub(r"[^\w.-]", "_", code))
    os.makedirs(folder, exist_ok=True)

    new = skipped = 0
    failed = []
    for r in rows:
        stamp = r["session"].replace(" ", "-")
        name = f"{code}_ASSIGN_{stamp}_{r['lang'].capitalize()}.pdf"
        path = os.path.join(folder, name)
        if not overwrite and _valid(path):
            skipped += 1
            continue
        try:
            ok = _fetch(r["url"], path)
        except Exception as e:
            ok = False
            failed.append(f"{name} ({type(e).__name__})")
        if ok:
            new += 1
            con.execute(
                "UPDATE assignments SET sha256=?, fetched_at=? "
                "WHERE code=? AND session=? AND lang=?",
                (_sha(path), time.strftime("%Y-%m-%d %H:%M:%S"),
                 code, r["session"], r["lang"]),
            )
        else:
            failed.append(name)
        time.sleep(0.4)

    con.commit()
    con.close()

    head = (f"{code}: {new + skipped}/{len(rows)} saved "
            f"({_span(rows)}) → {folder}")
    tail = f"\nnew {new} · skipped {skipped} · failed {len(failed)}"
    if failed:
        tail += "\nfailed: " + ", ".join(failed[:6])
    return head + tail


if __name__ == "__main__":
    build(verbose=True)
