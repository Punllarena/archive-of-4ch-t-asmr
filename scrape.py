#!/usr/bin/env python3
"""Scrape /t/ "ASMR Streams" threads into README.md.

Usage:
    python scrape.py                 # sync thread table, scrape live + uncached threads
    python scrape.py --thread 927218 # (re)scrape one thread
    python scrape.py --all           # (re)scrape every thread in the table
    python scrape.py --table-only    # only sync "thread archives.md"

Live threads come from the 4chan JSON API, archived threads from archiveofsins.com.
Parsed posts are cached in data/<thread>.json; README.md is rebuilt from the cache.
"""
import argparse
import base64
import binascii
import csv
import html
import json
import re
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
TABLE = ROOT / "thread archives.md"
README = ROOT / "README.md"
DATA = ROOT / "data"
PAGES = ROOT / "threads"

CATALOG = "https://a.4cdn.org/t/catalog.json"
LIVE_THREAD = "https://a.4cdn.org/t/thread/{}.json"
ARCHIVE_THREAD = "https://archiveofsins.com/t/thread/{}/"
SUBJECT_RE = re.compile(r"ASMR Streams? (?:Part|Episode) (\d+)", re.I)

MAGNET_RE = re.compile(r"magnet:\?xt=[^\s<>\"']+", re.I)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
B64_RE = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{16,}={0,2}(?![A-Za-z0-9+/=_-])")
INTERNAL_HOSTS = ("archiveofsins.com", "boards.4chan.org", "4chan.org", "4cdn.org")
MEGA_RE = re.compile(r"(?<![\w/])(#F?![\w-]{8}![\w-]{22,43})")
HOST_TYPES = [
    ("youtu", "YouTube"), ("patreon", "Patreon"), ("booth.pm", "Booth"), ("dlsite", "DLsite"),
    ("twitter.com", "Twitter"), ("x.com", "Twitter"), ("mega.", "Mega"), ("baidu", "Baidu"),
    ("drive.google", "Google Drive"), ("mediafire", "MediaFire"), ("pixeldrain", "Pixeldrain"),
    ("catbox", "Catbox"), ("pastebin", "Pastebin"), ("nicovideo", "Niconico"), ("bilibili", "Bilibili"),
    ("fantia", "Fantia"), ("fanbox", "Fanbox"), ("gofile", "Gofile"), ("anonfiles", "Anonfiles"),
]
LINK_HINT = re.compile(r"magnet:\?|https?://|mega\.nz|\.onion", re.I)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf8", "replace")


def clean_html(s):
    s = re.sub(r"<wbr\s*/?>", "", s)
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def try_b64(token):
    token = token.replace("-", "+").replace("_", "/")
    if len(token.rstrip("=")) % 4 == 1:
        return None
    token = token.rstrip("=")
    token += "=" * (-len(token) % 4)
    try:
        return base64.b64decode(token, validate=True).decode("utf8")
    except (binascii.Error, UnicodeDecodeError):
        return None


def decode_b64(text, level=1, max_level=2):
    """Return [(decoded, level)] for base64 strings holding links; level 2 = double encoded."""
    out = []
    for token in B64_RE.findall(text):
        dec = try_b64(token)
        if not dec or not all(c.isprintable() or c in "\n\r\t" for c in dec):

            continue
        if LINK_HINT.search(dec):
            out.append((dec, level))
        elif level < max_level:
            out.extend(decode_b64(dec, level + 1, max_level))
    return out


def host_type(url):
    if url.lower().startswith("magnet:"):
        return "Torrent"
    host = url.split("/")[2].lower().removeprefix("www.")
    for key, name in HOST_TYPES:
        if key in host:
            return name
    return host


def link_type(label, url):
    """Link label shown to viewers: Magnet stays Magnet, direct links show their host type."""
    return label if label == "Magnet" else host_type(url)


def extract_links(text):
    """Return [(label, url, encoding)] from plain (0), base64 (1) and double base64 (2) text."""
    found = []

    def add(src, enc):
        for m in MAGNET_RE.findall(src):
            found.append(("Magnet", m, enc))
        rest = MAGNET_RE.sub(" ", src)
        for frag in MEGA_RE.findall(rest):
            found.append(("Direct", "https://mega.nz/" + frag, enc))
        for u in URL_RE.findall(rest):
            u = u.rstrip(".,;:)]")
            parts = u.split("/")
            if len(parts) > 2 and not any(h in parts[2] for h in INTERNAL_HOSTS):
                found.append(("Direct", u, enc))

    add(text, 0)
    for dec, level in decode_b64(URL_RE.sub(" ", MAGNET_RE.sub(" ", text))):
        add(dec, level)
    seen, links = set(), []
    for label, url, enc in found:
        if url not in seen:
            seen.add(url)
            links.append((label, url, enc))
    return links


def make_context(text):
    """Post text without quotes, links and base64 blobs, shortened for a table cell."""
    text = MAGNET_RE.sub(" ", text)
    text = URL_RE.sub(" ", text)
    text = re.sub(r">>\d+", " ", text)
    text = B64_RE.sub(lambda m: " " if try_b64(m.group()) else m.group(), text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:80] + ("…" if len(text) > 80 else "")


def reply_targets(text):
    return list(dict.fromkeys(int(n) for n in re.findall(r">>(\d+)", text)))


def fmt_size(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}".replace(".0", "")
        n /= 1024


def post_row(thread, post, text, date, image=None, filename=None, size=None):
    return {"thread": thread, "post": post, "date": date, "reply_to": reply_targets(text),
            "links": extract_links(text), "context": make_context(text),
            "image": image, "filename": filename, "size": size}


def parse_live(thread):
    posts = json.loads(fetch(LIVE_THREAD.format(thread)))["posts"]
    rows = []
    for p in posts:
        date = time.strftime("%Y-%m-%d %H:%M", time.gmtime(p["time"]))
        name = p["filename"] + p["ext"] if "filename" in p else None
        size = f"{fmt_size(p['fsize'])}, {p['w']}x{p['h']}" if "fsize" in p else None
        rows.append(post_row(thread, p["no"], clean_html(p.get("com", "")), date, None, name, size))
    return rows


def parse_archive(thread):
    page = fetch(ARCHIVE_THREAD.format(thread))
    rows = []
    for art in re.split(r"(?=<article )", page)[1:]:
        m = re.match(r'<article[^>]*\sid="(\d+)"', art)
        if not m:
            continue
        art = re.sub(r"<script.*?</script>", "", art, flags=re.S)
        t = re.search(r'<div class="text">(.*?)</div>', art, re.S)
        img = re.search(r'<a[^>]*class="post_file_filename"[^>]*>', art)
        href = name = size = None
        if img:
            tag = img.group()
            href = re.search(r'href="([^"]+)"', tag).group(1)
            title = re.search(r'title="([^"]*)"', tag)
            name = html.unescape(title.group(1)) if title else clean_html(art[img.end():art.index("</a>", img.end())])
            meta = re.search(r"(\d[\d.]*\s?[KMG]?i?B),\s*(\d+x\d+)", art[max(0, img.start() - 300):img.end() + 300])
            size = f"{meta.group(1)}, {meta.group(2)}" if meta else None
        dt = re.search(r'<time datetime="(\d{4}-\d\d-\d\d)T(\d\d:\d\d)', art)
        rows.append(post_row(thread, int(m.group(1)), clean_html(t.group(1)) if t else "",
                             f"{dt.group(1)} {dt.group(2)}" if dt else None, href, name, size))
    return rows


# ---- thread table ----------------------------------------------------------

def read_table():
    """Parse "thread archives.md" in old ("Part N: id") or table format -> {part: thread}."""
    parts = {}
    if not TABLE.exists():
        return parts
    for line in TABLE.read_text(encoding="utf8").splitlines():
        m = re.match(r"Part (\d+):\s*(\d+)", line) or re.match(r"\|\s*(\d+)\s*\|\s*(\d+)\s*\|", line)
        if m:
            parts[int(m.group(1))] = int(m.group(2))
    return parts


def write_table(parts, live):
    lines = ["| Part | Thread | 4chan | Archive | Status |", "|---|---|---|---|---|"]
    for part in sorted(parts):
        t = parts[part]
        status = "live" if t in live else "archived"
        lines.append(f"| {part} | {t} | [link](https://boards.4chan.org/t/thread/{t}) | "
                     f"[link](https://archiveofsins.com/t/thread/{t}/) | {status} |")
    TABLE.write_text("\n".join(lines) + "\n", encoding="utf8")


def find_live():
    """Live ASMR Streams threads on /t/ -> {thread: part}."""
    out = {}
    for page in json.loads(fetch(CATALOG)):
        for t in page["threads"]:
            m = SUBJECT_RE.search(t.get("sub", "") or "")
            if m:
                out[t["no"]] = int(m.group(1))
    return out


# ---- README ----------------------------------------------------------------

def md_cell(s):
    return s.replace("|", "&#124;")


NL = chr(10)
DEAD_NOTE = ("> **Note:** links were scraped as posted and are not checked. "
             "Many are old and may be dead: torrents with no seeders, deleted or expired file-host links, "
             "removed videos, and unarchived images.")


def write(path, lines):
    path.write_text(NL.join(lines) + NL, encoding="utf8")


def build_pages(parts, live):
    """Write threads/part-NN.md per cached thread, README.md index and all-links.csv."""
    PAGES.mkdir(exist_ok=True)
    index = ["# /t/ ASMR Streams archive", "",
             "Generated by `scrape.py`. Each part page lists posts with links or an archived image.",
             "Full list for spreadsheets: [all-links.csv](all-links.csv).", "",
             DEAD_NOTE, "",
             "| Part | Thread | Dates (UTC) | Posts | Link posts | Images | Status |",
             "|---|---|---|---|---|---|---|"]
    csv_file = (ROOT / "all-links.csv").open("w", encoding="utf8", newline="")
    out = csv.writer(csv_file)
    out.writerow(["part", "thread", "post", "date", "type", "url", "reply_to",
                  "image", "filename", "size", "context"])
    for part in sorted(parts):
        thread = parts[part]
        cache = DATA / f"{thread}.json"
        if not cache.exists():
            index.append(f"| {part} | {thread} | | | | | not scraped |")
            continue
        rows = json.loads(cache.read_text(encoding="utf8"))
        shown = [r for r in rows if r["links"] or r["image"]]
        page = [f"# Part {part} - thread {thread}", "",
                f"[4chan](https://boards.4chan.org/t/thread/{thread}) | "
                f"[archive](https://archiveofsins.com/t/thread/{thread}/) | [back to index](../README.md)", "",
                DEAD_NOTE, "",
                "| Post | Links | Image | Date (UTC) | Reply to | Context | File | Size |",
                "|---|---|---|---|---|---|---|---|"]
        for r in shown:
            links = " ".join(f"[{link_type(l, u)}]({u.replace('|', '%7C')})" for l, u, _ in r["links"])
            img = f"[image]({r['image']})" if r["image"] else ""
            cells = [r["post"], links, img, r["date"] or "", ", ".join(map(str, r["reply_to"])),
                     md_cell(r["context"]), md_cell(r["filename"] or ""), r["size"] or ""]
            page.append("| " + " | ".join(map(str, cells)) + " |")
            for label, url, enc in r["links"]:
                out.writerow([part, thread, r["post"], r["date"], link_type(label, url), url,
                              " ".join(map(str, r["reply_to"])),
                              r["image"] or "", r["filename"] or "", r["size"] or "", r["context"]])
            if not r["links"]:
                out.writerow([part, thread, r["post"], r["date"], "", "", " ".join(map(str, r["reply_to"])),
                              r["image"] or "", r["filename"] or "", r["size"] or "", r["context"]])
        write(PAGES / f"part-{part:02d}.md", page)
        dates = [r["date"][:10] for r in rows if r["date"]]
        span = f"{min(dates)} to {max(dates)}" if dates else ""
        status = "live" if thread in live else "archived"
        index.append(f"| [{part}](threads/part-{part:02d}.md) | {thread} | {span} | {len(rows)} | "
                     f"{sum(1 for r in rows if r['links'])} | {sum(1 for r in rows if r['image'])} | {status} |")
    csv_file.close()
    write(README, index)


def scrape(thread, live):
    print(f"scraping {thread} ({'4chan' if thread in live else 'archive'})")
    rows = parse_live(thread) if thread in live else parse_archive(thread)
    DATA.mkdir(exist_ok=True)
    (DATA / f"{thread}.json").write_text(json.dumps(rows), encoding="utf8")
    print(f"  {len(rows)} posts, {sum(1 for r in rows if r['links'])} with links")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--thread", type=int, action="append", help="scrape this thread number")
    ap.add_argument("--all", action="store_true", help="rescrape every thread in the table")
    ap.add_argument("--table-only", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="rebuild pages from cache, no scraping")
    args = ap.parse_args()

    parts = read_table()
    live = find_live()
    if args.rebuild:
        build_pages(parts, live)
        return
    for thread, part in live.items():
        if parts.get(part) != thread:
            print(f"new part {part}: {thread}")
            parts[part] = thread
    write_table(parts, live)
    if args.table_only:
        return

    if args.thread:
        todo = args.thread
    elif args.all:
        todo = list(parts.values())
    else:
        todo = [t for t in parts.values() if t in live or not (DATA / f"{t}.json").exists()]
    for i, thread in enumerate(todo):
        if i:
            time.sleep(1)
        scrape(thread, live)
    build_pages(parts, live)


if __name__ == "__main__":
    main()
