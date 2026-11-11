"""IGNOU PYQ index builder.

Crawls the two IGNOU question-paper backends and writes a local SQLite index:

  legacy : https://webservices.ignou.ac.in/Pre-Question/Question Paper <Session>/<SCHOOL>/<SERIES>/<CODE>.pdf
  portal : https://www.ignou.ac.in/studentService/download/questionPapers/<Session>
           -> https://www.ignou.ac.in/viewFile/ldd/<bucket>/<CODE>.pdf

Never prints to stdout when imported (the MCP server owns stdout).
"""

import os
import re
import sqlite3
import time
import urllib.parse as up
from concurrent.futures import ThreadPoolExecutor

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "ignou.db")

ARCHIVE = "https://webservices.ignou.ac.in/Pre-Question/"
PORTAL_INDEX = "https://www.ignou.ac.in/pages/221"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ignou-tools/1.0"}

S = requests.Session()
S.headers.update(UA)

MONTHS = {"june": 6, "jun": 6, "december": 12, "dec": 12}

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    code        TEXT NOT NULL,
    key         TEXT NOT NULL,
    backend     TEXT NOT NULL,
    session     TEXT NOT NULL,
    year        INTEGER NOT NULL,
    month       INTEGER NOT NULL,
    url         TEXT NOT NULL,
    bucket      TEXT,
    sha256      TEXT,
    fetched_at  TEXT,
    PRIMARY KEY (code, session)
);
CREATE INDEX IF NOT EXISTS idx_key ON papers(key);
CREATE INDEX IF NOT EXISTS idx_session ON papers(year, month);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def norm(code):
    return re.sub(r"[^A-Z0-9]", "", code.upper())


def parse_session(label):
    """'June 2024' / 'Dec2025' / 'December 2025' -> ('June 2024', 2024, 6)."""
    lab = up.unquote(label).replace("_", " ").strip()
    m = re.match(r"([A-Za-z]+)\s*[- ]?\s*(\d{4})$", lab)
    if not m:
        return None
    mon, yr = m.group(1).lower(), int(m.group(2))
    if mon not in MONTHS:
        return None
    canonical = ("June" if MONTHS[mon] == 6 else "December") + f" {yr}"
    return canonical, yr, MONTHS[mon]


def get(url, tries=3, timeout=90):
    for _ in range(tries):
        try:
            r = S.get(url, timeout=timeout)
            if r.status_code == 200 and r.content:
                return r.text
        except Exception:
            pass
        time.sleep(1)
    return None


def pdf_links(html, page_url):
    """href -> (code, absolute_url, folder). Anchors are unreliable, hrefs are not."""
    out = {}
    for m in re.finditer(r'href\s*=\s*"([^"]+\.pdf)"', html, re.I):
        href = m.group(1)
        code = os.path.splitext(os.path.basename(href))[0].replace("%20", " ").strip().upper()
        if not code or len(code) > 40:
            continue
        out.setdefault(code, (code, up.urljoin(page_url, href), href.split("/")[0] if "/" in href else ""))
    return out


def archive_sessions():
    """Discover session folders from the archive landing page (no hard-coded list)."""
    html = get(ARCHIVE)
    if not html:
        return {}
    found = {}
    for m in re.finditer(r'href\s*=\s*"(Question%20Paper[^"/]+)/[^"]+\.htm"', html, re.I):
        parsed = parse_session(m.group(1).replace("Question%20Paper", ""))
        if parsed:
            found[parsed[0]] = up.urljoin(ARCHIVE, m.group(1) + "/")
    return found


def archive_index_page(folder, canonical):
    """The per-session index file name drifted over the years; probe both schemes."""
    candidates = [f"QP {canonical}.htm", f"QP{canonical.replace(' ', '')}.htm"]
    for name in candidates:
        url = up.urljoin(folder, up.quote(name))
        if get(url):
            return url
    return None


def crawl_archive():
    rows = []
    for canonical, folder in archive_sessions().items():
        _, yr, mon = parse_session(canonical)
        index_page = archive_index_page(folder, canonical)
        if not index_page:
            continue
        idx_html = get(index_page)
        if not idx_html:
            continue
        schools = {}
        for m in re.finditer(r'href\s*=\s*"([^"]+\.htm[^"]*)"', idx_html, re.I):
            href = m.group(1)
            if re.search(r"(filelist|themedata|\.css)", href, re.I):
                continue
            schools.setdefault(href.split("/")[0].upper(), up.urljoin(index_page, href).split("#")[0])
        pages = list(schools.values())
        with ThreadPoolExecutor(max_workers=6) as ex:
            for page, html in zip(pages, ex.map(get, pages)):
                if not html:
                    continue
                for _c, (code, url, folder_name) in pdf_links(html, page).items():
                    rows.append(("archive", canonical, yr, mon, code, url, folder_name))
    return rows


def crawl_portal():
    rows = []
    idx = get(PORTAL_INDEX)
    if not idx:
        return rows
    pages = []
    for m in re.finditer(r'href\s*=\s*"(/studentService/download/questionPapers/[A-Za-z0-9]+)"', idx, re.I):
        parsed = parse_session(m.group(1).rsplit("/", 1)[-1])
        if parsed:
            pages.append((parsed, "https://www.ignou.ac.in" + m.group(1)))
    for (canonical, yr, mon), page in pages:
        html = get(page)
        if not html:
            continue
        for tr in re.split(r"<tr[^>]*>", html, flags=re.I):
            m = re.search(r'href\s*=\s*"([^"]+\.pdf)"', tr, re.I)
            if not m:
                continue
            text = re.sub(r"&nbsp;|\s+", " ", re.sub(r"<[^>]*>", " ", tr)).strip()
            cm = re.search(r"\b([A-Z]{2,6}[A-Z0-9]*[-\u2013]\s?\d{2,3}[A-Z]?)\b", text)
            if not cm:
                continue
            code = cm.group(1).replace(" ", "").replace("\u2013", "-").upper()
            url = up.urljoin(page, m.group(1))
            bucket = url.split("/ldd/")[-1].split("/")[0] if "/ldd/" in url else ""
            rows.append(("portal", canonical, yr, mon, code, url, bucket))
    return rows


def build(verbose=False):
    rows = crawl_archive() + crawl_portal()
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    existing = {
        (c, s): (h, f)
        for c, s, h, f in con.execute("SELECT code, session, sha256, fetched_at FROM papers")
    }
    con.execute("DELETE FROM papers")
    payload = []
    seen = set()
    for backend, session, yr, mon, code, url, bucket in rows:
        if (code, session) in seen:
            continue
        seen.add((code, session))
        h, f = existing.get((code, session), (None, None))
        payload.append((code, norm(code), backend, session, yr, mon, url, bucket, h, f))
    con.executemany("INSERT OR REPLACE INTO papers VALUES (?,?,?,?,?,?,?,?,?,?)", payload)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('built_at', ?)", (time.strftime("%Y-%m-%d %H:%M:%S"),))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    sessions = con.execute("SELECT COUNT(DISTINCT session) FROM papers").fetchone()[0]
    con.close()
    if verbose:
        print(f"index: {n} papers across {sessions} sessions -> {DB_PATH}")
    return n, sessions


if __name__ == "__main__":
    build(verbose=True)
