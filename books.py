"""eGyanKosh (IGNOU Self Learning Material) index + resolver.

eGyanKosh is IGNOU's official SLM repository. Structure:

    course community  /handle/<id>          e.g. "BECC-104 Mathematical Methods in Economics-II"
      block collection /handle/<id>         e.g. "Block-1 Functions of Several Variables"
        item           /handle/<id>         full block PDF and/or per-unit PDFs
          bitstream    /bitstream/<item>/<seq>/<name>.pdf

The entire course -> block tree is published on one page (/community-list, ~6 MB),
so the index costs exactly one request. Unit/PDF links are resolved lazily per
course and cached, because crawling ~21k block pages up front is not polite.

Never prints to stdout when imported (the MCP server owns stdout).
"""

import hashlib
import html as htmlmod
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

import requests

try:  # eGyanKosh serves an incomplete TLS chain; the Windows store fixes it
    import truststore

    truststore.inject_into_ssl()
except Exception:  # pragma: no cover - fall back to normal verification
    pass

import indexer
import paths

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = indexer.DB_PATH
BASE = "https://egyankosh.ac.in"
LIST_URL = BASE + "/community-list"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ignou-tools/1.0"}

CODE_RE = re.compile(r"^\s*([A-Z]{2,8})\s*[-\u2013]\s*(\d{2,4})\b")
DEVANAGARI = re.compile(r"[\u0900-\u097F]")
ARABIC = re.compile(r"[\u0600-\u06FF]")
BLOCK_PREFIX = ("block", "kh", "\u0916\u0902\u0921", "\u0916\u0923\u094d\u0921", "\u0916\u0923\u094d\u0921")

SCHEMA = """
CREATE TABLE IF NOT EXISTS book_courses (
    handle      TEXT PRIMARY KEY,
    code        TEXT NOT NULL,
    key         TEXT NOT NULL,
    title       TEXT NOT NULL,
    lang        TEXT NOT NULL,
    school      TEXT,
    chain       TEXT,
    nblocks     INTEGER NOT NULL DEFAULT 0,
    built_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_book_key ON book_courses(key);
CREATE TABLE IF NOT EXISTS book_blocks (
    course_handle TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    handle        TEXT NOT NULL,
    title         TEXT NOT NULL,
    PRIMARY KEY (course_handle, handle)
);
CREATE TABLE IF NOT EXISTS book_items (
    block_handle TEXT NOT NULL,
    item_handle  TEXT NOT NULL,
    title        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    url          TEXT NOT NULL,
    size         INTEGER,
    sha256       TEXT,
    fetched_at   TEXT,
    PRIMARY KEY (block_handle, item_handle)
);
"""

S = requests.Session()
S.headers.update(UA)


# ------------------------------------------------------------------ HTML parse
class _Tree(HTMLParser):
    """Walks <li class="media well"> nesting on /community-list."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.open = []
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "li":
            media = "media" in (a.get("class") or "")
            node = None
            if media:
                parent = next((e["node"] for e in reversed(self.open) if e["media"]), None)
                node = {"href": None, "label": "", "parent": parent, "grab": False}
                self.nodes.append(node)
            self.open.append({"media": media, "node": node})
        elif tag == "a" and self.open:
            top = self.open[-1]
            n = top["node"] if top["media"] else None
            if n is not None and n["href"] is None:
                href = a.get("href") or ""
                if href.startswith("/handle/"):
                    n["href"] = href
                    n["grab"] = True

    def handle_endtag(self, tag):
        if tag == "li" and self.open:
            self.open.pop()

    def handle_data(self, data):
        if self.open:
            top = self.open[-1]
            n = top["node"] if top["media"] else None
            if n is not None and n["grab"] and not n["label"].strip():
                n["label"] += data


def _get(url, tries=3, timeout=90):
    for i in range(tries):
        try:
            r = S.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        time.sleep(1 + i)
    return None


def _script(label):
    if DEVANAGARI.search(label):
        return "hindi"
    if ARABIC.search(label):
        return "urdu"
    return "english"


def _clean(label):
    return re.sub(r"\s+", " ", label).strip()


def _chain(node):
    out = []
    p = node["parent"]
    while p is not None:
        out.append(_clean(p["label"]))
        p = p["parent"]
    return list(reversed(out))


# ---------------------------------------------------------------------- build
def build(verbose=False):
    """Rebuild the course -> block mapping from /community-list (one request)."""
    html = _get(LIST_URL, timeout=180)
    if not html:
        raise RuntimeError("eGyanKosh /community-list unreachable")

    t = _Tree()
    t.feed(html)
    nodes = [n for n in t.nodes if n["href"]]
    for n in nodes:
        n["label"] = _clean(n["label"])

    kids = {}
    for n in nodes:
        p = n["parent"]
        if p is not None:
            kids.setdefault(id(p), []).append(n)

    def leaves(node, depth=0):
        out = []
        for k in kids.get(id(node), []):
            if depth > 6:
                break
            sub = kids.get(id(k), [])
            out.extend(leaves(k, depth + 1) if sub else [k])
        return out

    rows = []
    blocks = []
    for n in nodes:
        m = CODE_RE.match(n["label"])
        if not m:
            continue
        code = f"{m.group(1)}-{m.group(2)}"
        chain = _chain(n)
        school = next((c for c in chain if re.match(r"^\d+\.\s*School", c)), "")
        bl = leaves(n)
        rows.append((n["href"], code, indexer.norm(code), n["label"], _script(n["label"]),
                     school, " > ".join(chain), len(bl), time.strftime("%Y-%m-%d %H:%M:%S")))
        for i, b in enumerate(bl, 1):
            blocks.append((n["href"], i, b["href"], b["label"]))

    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.execute("DELETE FROM book_courses")
    con.execute("DELETE FROM book_blocks")
    con.executemany("INSERT OR REPLACE INTO book_courses VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT OR REPLACE INTO book_blocks VALUES (?,?,?,?)", blocks)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('books_built_at', ?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"),))
    con.commit()
    n_courses = con.execute("SELECT COUNT(DISTINCT key) FROM book_courses").fetchone()[0]
    n_blocks = con.execute("SELECT COUNT(*) FROM book_blocks").fetchone()[0]
    con.close()
    if verbose:
        print(f"books: {n_courses} course codes, {len(rows)} entries, {n_blocks} blocks")
    return n_courses, len(rows), n_blocks


# -------------------------------------------------------------------- resolve
def resolve_course(code, lang="english"):
    """Best course entry for a loose code, preferring the requested language."""
    key = indexer.norm(code)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM book_courses WHERE key = ?", (key,)).fetchall()
    con.close()
    if not rows:
        return None
    want = (lang or "english").strip().lower()
    same = [r for r in rows if r["lang"] == want]
    pool = same or rows
    # most complete copy, then the "Current" syllabus tree, then the shallowest
    pool = sorted(pool, key=lambda r: (-r["nblocks"], "Current" not in (r["chain"] or ""), len(r["chain"] or "")))
    return dict(pool[0])


def blocks_of(course_handle):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM book_blocks WHERE course_handle = ? ORDER BY seq", (course_handle,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _table_items(html, own_handle):
    """Item links from the browse table (not the breadcrumb)."""
    i = html.find('summary="This table browses')
    body = html[i:] if i > 0 else ""
    out, seen = [], set()
    for href, lab in re.findall(r'href="(/handle/[0-9/]+)"[^>]*>([\s\S]{0,300}?)</a>', body):
        lab = _clean(htmlmod.unescape(re.sub(r"<[^>]+>", "", lab)))
        if not lab or href in seen or href.rstrip("/") == own_handle:
            continue
        seen.add(href)
        out.append((href, lab))
    return out


def _is_block_item(title, block_title):
    t = title.strip().lower()
    if t == block_title.strip().lower():
        return True
    return t.startswith(BLOCK_PREFIX)


def _bitstream(item_handle):
    html = _get(BASE + item_handle, timeout=90)
    if not html:
        return []
    urls = sorted({u for u in re.findall(r'href="(/bitstream/[^"]+)"', html) if u.lower().endswith(".pdf")})
    return urls


def resolve_items(block_handle, block_title=""):
    """Items of one block collection, with PDF URLs. Cached in book_items."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cached = con.execute(
        "SELECT * FROM book_items WHERE block_handle = ? ORDER BY title", (block_handle,)
    ).fetchall()
    if cached:
        con.close()
        return [dict(r) for r in cached]

    html = _get(BASE + block_handle, timeout=90)
    if not html:
        con.close()
        return []

    items = _table_items(html, block_handle)
    with ThreadPoolExecutor(max_workers=4) as ex:
        streams = list(ex.map(lambda it: _bitstream(it[0]), items))

    rows = []
    for (ih, title), urls in zip(items, streams):
        for n, url in enumerate(urls):
            t = title if len(urls) == 1 else f"{title} ({n + 1})"
            rows.append((block_handle, ih if len(urls) == 1 else f"{ih}#{n}", t,
                         "block" if _is_block_item(title, block_title) else "unit", url, None, None, None))
    if rows:
        con.executemany("INSERT OR REPLACE INTO book_items VALUES (?,?,?,?,?,?,?,?)", rows)
        con.commit()
    con.close()
    return [dict(zip(("block_handle", "item_handle", "title", "kind", "url", "size", "sha256", "fetched_at"), r))
            for r in rows]


# ------------------------------------------------------------------- download
def _valid(path, min_bytes=10_000):
    try:
        if os.path.getsize(path) < min_bytes:
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


def _save(url, path):
    tmp = path + ".part"
    r = S.get(BASE + url if url.startswith("/") else url, timeout=300, stream=True)
    r.raise_for_status()
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
    if os.path.getsize(tmp) < 10_000:
        os.remove(tmp)
        return 0
    with open(tmp, "rb") as f:
        if f.read(5) != b"%PDF-":
            os.remove(tmp)
            return 0
    os.replace(tmp, path)
    return os.path.getsize(path)


def _nat(s):
    """Natural sort so Unit-2 precedes Unit-10."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s)]


def _safe(name, limit=90):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    name = re.sub(r"\s+", " ", name)
    return (name[:limit] or "untitled").strip()


def _record(block_handle, item_handle, size, sha):
    """Short-lived write so the resolver never blocks on a held lock."""
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        con.execute(
            "UPDATE book_items SET size=?, sha256=?, fetched_at=? WHERE block_handle=? AND item_handle=?",
            (size, sha, time.strftime("%Y-%m-%d %H:%M:%S"), block_handle, item_handle),
        )
        con.commit()
    finally:
        con.close()


def download(code, lang="english", detail="block", dest="", overwrite=False):
    course = resolve_course(code, lang)
    if not course:
        return f"{code}: no SLM in eGyanKosh index (try refresh_index)"

    blocks = blocks_of(course["handle"])
    if not blocks:  # rare: course is a plain collection of items
        if not resolve_items(course["handle"], course["title"]):
            return f"{course['code']}: no SLM files in eGyanKosh for this course"
        blocks = [{"handle": course["handle"], "title": course["title"], "seq": 1}]

    root = dest or os.path.join(paths.documents_dir(), "IGNOU Books",
                                re.sub(r"[^\w.-]", "_", course["code"]))
    os.makedirs(root, exist_ok=True)

    new = skipped = expected = 0
    failed = []

    for b in blocks:
        items = resolve_items(b["handle"], b["title"])
        if not items:
            failed.append(f"{b['title'][:28]} (no files)")
            continue
        full = [i for i in items if i["kind"] == "block"]
        units = [i for i in items if i["kind"] == "unit"]
        targets = (full or units) if detail != "unit" else (units or full)
        targets = sorted(targets, key=lambda i: _nat(i["title"]))
        if not targets:
            failed.append(f"{b['title'][:28]} (no files)")
            continue

        nested = detail == "unit" and len(units) > 1
        folder = os.path.join(root, _safe(b["title"])) if nested else root
        os.makedirs(folder, exist_ok=True)
        expected += len(targets)

        for it in targets:
            path = os.path.join(folder, _safe(it["title"]) + ".pdf")
            if not overwrite and _valid(path):
                skipped += 1
                continue
            try:
                size = _save(it["url"], path)
            except Exception as e:
                failed.append(f"{it['title'][:24]} ({type(e).__name__})")
                continue
            if not size:
                failed.append(it["title"][:28])
                continue
            new += 1
            _record(it["block_handle"], it["item_handle"], size, _sha(path))
            time.sleep(0.3)

    total = new + skipped
    head = (f"{course['code']}: {total}/{max(expected, total + len(failed))} files "
            f"({len(blocks)} blocks, {course['lang']}) \u2192 {root}")
    tail = f"\nnew {new} \u00b7 skipped {skipped} \u00b7 failed {len(failed)}"
    if failed:
        tail += "\nfailed: " + ", ".join(failed[:5])
    return head + tail


if __name__ == "__main__":
    build(verbose=True)
