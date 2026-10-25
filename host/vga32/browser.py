"""A text-mode web browser: headings, paragraphs, lists, links, images, search.

Pages are fetched and parsed on the host, then laid out as monospace text so they
stay readable in 16 colors. Images load in the background and are dithered onto the
palette; icons and tiny images fall back to their alt text. No JavaScript or CSS.
"""
import bisect
import concurrent.futures
import math
import os
import pathlib
import queue
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

from . import images
from .palette import C
from .ui import CH, CW, FONT, App, bevel

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VGA32TextBrowser/1.0"
SEARCH = "https://lite.duckduckgo.com/lite/?q="
SEARCH_FALLBACK = "https://www.bing.com/search?q="   # used when DuckDuckGo answers with its bot check
MAX_BYTES = 4 << 20
TOOLBAR_H = 24
MAX_IMAGES = 40          # per page; the rest show their alt text
IMAGE_WORKERS = 4
ICON_SIZE = 32           # images declared or decoded at or below this (both sides) stay as alt text

HOME_HTML = """<html><head><title>Start</title></head><body>
<h1>VGA32 Browser</h1>
<p>Type an address or a search in the bar above (click it, or press Ctrl+L).</p>
<h2>Keys</h2>
<ul><li>Up/Down, PgUp/PgDn, Space, wheel: scroll</li>
<li>Tab / Shift+Tab: select link, Enter: open it</li>
<li>Alt+Left / Backspace: back, Alt+Right: forward, F5: reload</li>
<li>Ctrl+F: find in page, Ctrl+I: images on/off</li>
<li>Click an image to open it in the image viewer</li>
<li>Local files work too: type a path like C:\\Users\\you\\Pictures\\photo.jpg</li></ul>
<h2>Try</h2>
<ul><li><a href="https://en.wikipedia.org/wiki/ESP32">Wikipedia: ESP32</a></li>
<li><a href="https://news.ycombinator.com/">Hacker News</a></li>
<li><a href="https://lite.cnn.com/">CNN Lite</a></li>
<li><a href="https://text.npr.org/">NPR Text</a></li></ul>
</body></html>"""

BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "nav", "aside", "ul", "ol", "table",
         "tr", "form", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "dt", "dd", "figure",
         "figcaption", "address", "details", "summary", "dl", "fieldset", "center", "hr", "body"}
SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head", "select", "textarea", "canvas", "object"}


class _Parser(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.title = ""
        self.blocks = []          # {"kind", "runs": [(text, style, link)], "depth"}
        self.links = []           # absolute hrefs
        self.link_text = []
        self.skip = 0
        self.in_title = False
        self.link = None
        self.bold = 0
        self.code = 0
        self.pre = 0
        self.depth = 0
        self.kind = "p"
        self.runs = []
        self.main = None          # block index where the main content starts
        self.first_h1 = None
        self.images = []          # {"src", "alt", "link"}; blocks of kind "img" point here

    def _image(self, a):
        src = a.get("src") or a.get("data-src") or a.get("data-lazy-src") or ""
        srcset = a.get("srcset") or a.get("data-srcset")
        if srcset:
            src = _pick_srcset(srcset) or src
        alt = re.sub(r"\s+", " ", a.get("alt") or "").strip()
        dims = [int(m.group()) if (m := re.match(r"\d+", a.get(k) or "")) else 0 for k in ("width", "height")]
        icon = dims[0] and dims[1] and dims[0] <= ICON_SIZE and dims[1] <= ICON_SIZE
        if not src or icon or re.search(r"\.svg(\?|$)", src, re.I):
            if alt:
                self.runs.append((f"[{alt}]", "dim", self.link))
            return
        self.flush()
        self.images.append({"src": urljoin(self.base, src), "alt": alt, "link": self.link})
        self.blocks.append({"kind": "img", "runs": [], "depth": 0, "img": len(self.images) - 1})

    def flush(self, next_kind=None):
        if any(t.strip() for t, _, _ in self.runs):
            self.blocks.append({"kind": self.kind, "runs": self.runs, "depth": self.depth})
        self.runs = []
        if next_kind:
            self.kind = next_kind

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "base" and a.get("href"):
            self.base = urljoin(self.base, a["href"])
        if tag in SKIP:
            if tag != "head":
                self.skip += 1
            return
        if self.skip:
            return
        if self.main is None and (tag in ("main", "article") or a.get("role") == "main"):
            self.flush()
            self.main = len(self.blocks)
        if tag == "h1" and self.first_h1 is None:
            self.flush()
            self.first_h1 = len(self.blocks)
        if tag in ("ul", "ol", "dl"):
            self.depth += 1
        if tag in BLOCK or tag == "br":
            kind = tag if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "blockquote", "dt", "dd") else "p"
            if tag == "br":
                kind = self.kind
            self.flush(kind)
            if tag == "hr":
                self.blocks.append({"kind": "hr", "runs": [], "depth": 0})
            if tag == "pre":
                self.pre += 1
        elif tag in ("td", "th"):
            if self.runs:
                self.runs.append((" | ", "dim", None))
        if tag == "a" and a.get("href") and not a["href"].startswith(("javascript:", "#")):
            self.links.append(urljoin(self.base, a["href"]))
            self.link_text.append("")
            self.link = len(self.links) - 1
        elif tag in ("b", "strong", "h1", "h2", "h3", "th"):
            self.bold += 1
        elif tag in ("code", "kbd", "samp", "tt"):
            self.code += 1
        elif tag == "img":
            self._image(a)
        elif tag == "input" and a.get("type") in ("submit", "button") and a.get("value"):
            self.runs.append((f"[{a['value']}]", "dim", None))

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in SKIP:
            if tag != "head":
                self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "a":
            self.link = None
        elif tag in ("b", "strong", "h1", "h2", "h3", "th"):
            self.bold = max(0, self.bold - 1)
        elif tag in ("code", "kbd", "samp", "tt"):
            self.code = max(0, self.code - 1)
        if tag in ("ul", "ol", "dl"):
            self.depth = max(0, self.depth - 1)
        if tag in BLOCK:
            self.flush("p")
            if tag == "pre":
                self.pre = max(0, self.pre - 1)

    def handle_data(self, data):
        if self.in_title:
            self.title += data
            return
        if self.skip:
            return
        if not self.pre:
            data = re.sub(r"\s+", " ", data)
            if not self.runs or self.runs[-1][0].endswith(" "):
                data = data.lstrip()
            if not data:
                return
        style = "code" if self.code else "bold" if self.bold else "text"
        if self.link is not None:
            style = "link"
            self.link_text[self.link] += data
        self.runs.append((data, style, self.link))

    def close(self):
        super().close()
        self.flush()


def _pick_srcset(srcset):
    """Choose a candidate near screen size from 'url 480w, url 2x, ...'."""
    cands = []
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        d = bits[1] if len(bits) > 1 else "1x"
        try:
            n = float(d[:-1]) * (1 if d.endswith("w") else 640)
        except ValueError:
            continue
        cands.append((n, bits[0]))
    if not cands:
        return None
    cands.sort()
    good = [c for c in cands if c[0] >= 480]
    return (good[0] if good else cands[-1])[1]


class Page:
    def __init__(self, url, title="", blocks=(), links=(), link_text=(), error=None, start=0, images=()):
        self.url, self.title, self.error = url, title, error
        self.blocks, self.links, self.link_text = list(blocks), list(links), list(link_text)
        self.start = start        # block to open at, skipping site navigation
        # {"src", "alt", "link", "state": None|loading|ok|icon|failed|skipped, "img": PIL image}
        self.images = list(images)

    def plain_text(self):
        out = []
        for b in self.blocks:
            t = "".join(r[0] for r in b["runs"]).strip() if b["kind"] != "pre" else "".join(r[0] for r in b["runs"])
            if b["kind"] == "img":
                im = self.images[b["img"]]
                if im.get("state") == "icon" and not im["alt"]:
                    continue
                t = f"[image {b['img']}: {im['alt'] or 'no description'}]"
            elif b["kind"] == "hr":
                t = "----"
            elif b["kind"].startswith("h"):
                t = "#" * int(b["kind"][1]) + " " + t
            elif b["kind"] == "li":
                t = "  " * max(0, b["depth"] - 1) + "- " + t
            out.append(t)
        return "\n".join(out)


def parse_html(html, url):
    p = _Parser(url)
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    # Reader-style start: the first substantial paragraph after <main>/<h1>, plus its heading.
    start = p.main if p.main is not None else p.first_h1 or 0
    for i in range(start, len(p.blocks)):
        b = p.blocks[i]
        if b["kind"] == "p" and len("".join(r[0] for r in b["runs"])) >= 100:
            j = i
            while j > max(start, i - 3) and not p.blocks[j]["kind"].startswith("h"):
                j -= 1
            start = j if p.blocks[j]["kind"].startswith("h") else i
            break
    if start >= len(p.blocks) - 1:
        start = 0
    return Page(url, re.sub(r"\s+", " ", p.title).strip(), p.blocks, p.links, p.link_text, start=start,
                images=p.images)


def image_page(url, data):
    """A page that is just one image (a direct link to a .png/.jpg/...)."""
    name = urllib.parse.unquote(urlparse(url).path.rsplit("/", 1)[-1]) or url
    try:
        img = images.decode(data)
    except Exception as e:
        return Page(url, name, error=f"can't decode image: {e}")
    info = {"src": url, "alt": name, "link": None, "state": "ok", "img": img}
    return Page(url, f"{name} ({img.width}x{img.height})", [{"kind": "img", "runs": [], "depth": 0, "img": 0}],
                images=[info])


def clean_href(href):
    """Unwrap DuckDuckGo result redirects to the real URL."""
    u = urlparse(href)
    if u.netloc.endswith("duckduckgo.com") and u.path.startswith("/l/"):
        q = parse_qs(u.query).get("uddg")
        if q:
            return q[0]
    return href


def fetch_any(url):
    page = fetch(url)
    if url.startswith(SEARCH) and not page.error and "bots use DuckDuckGo" in page.plain_text():
        page = fetch(SEARCH_FALLBACK + url[len(SEARCH):])
    return page


def fetch(url, timeout=20):
    if url == "about:home":
        return parse_html(HOME_HTML, url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5",
                                               "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            final = r.geturl()
            ctype = r.headers.get("Content-Type", "")
            raw = r.read(MAX_BYTES)
            if ctype.startswith("image/"):
                raw += r.read(images.MAX_BYTES - len(raw))
                return image_page(final, raw)
    except urllib.error.HTTPError as e:
        final, ctype, raw = url, e.headers.get("Content-Type", "") if e.headers else "", e.read(MAX_BYTES)
        if not raw:
            return Page(url, f"HTTP {e.code}", error=f"HTTP {e.code} {e.reason}")
    except Exception as e:
        return Page(url, "Error", error=str(getattr(e, "reason", e)))
    m = re.search(r"charset=([\w-]+)", ctype) or re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', raw[:4096], re.I)
    enc = m.group(1) if m else "utf-8"
    if isinstance(enc, bytes):
        enc = enc.decode()
    try:
        text = raw.decode(enc, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    if "html" in ctype or "<html" in text[:2000].lower() or not ctype:
        return parse_html(text, final)
    if ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
        return Page(final, final.rsplit("/", 1)[-1], [{"kind": "pre", "runs": [(text, "text", None)], "depth": 0}])
    return Page(final, "Unsupported", error=f"can't display {ctype.split(';')[0]} ({len(raw)} bytes)")


def to_url(s):
    s = s.strip().strip('"')
    if not s:
        return "about:home"
    if re.match(r"^[a-z]:[\\/]", s, re.I) or s.startswith(("~", "\\\\")) or (s.startswith("/") and os.path.exists(s)):
        return pathlib.Path(os.path.expanduser(s)).resolve().as_uri()      # local file
    if re.match(r"^[a-z][a-z0-9+.-]*:", s, re.I):
        return s
    if " " not in s and ("." in s or s.startswith("localhost")):
        return ("http://" if s.startswith(("localhost", "127.", "192.168.", "10.")) else "https://") + s
    return SEARCH + quote_plus(s)


STYLE_COLORS = {"text": C.black, "bold": C.black, "code": C.teal, "link": C.blue, "dim": C.darkgray}
HEAD_COLORS = {"h1": C.navy, "h2": C.blue, "h3": C.teal}


class Browser(App):
    title = "Browser"
    icon = "browser"
    size = (560, 330)

    def __init__(self, desk, url=None, **kw):
        super().__init__(desk)
        self.page = Page("about:blank")
        self.history, self.hpos = [], -1
        self.scroll = 0
        self.loading = False
        self.inbox = queue.Queue()
        self.layout_cols = None
        self.lines = []
        self.sel = None                # selected link index (keyboard)
        self.editing, self.addr, self.addr_fresh = False, "", False
        self.finding, self.find_text = False, ""
        self.status = ""
        self.visited = set()
        self.load_id = 0
        self.block_line = []
        self.show_images = True
        self.scaled = {}               # (image index, max w, max h) -> palette-ready image
        self.img_box = (0, 0)          # max image size used by the current layout
        self.pool = None
        self.navigate(url or "about:home")

    # ---------- navigation ----------
    def navigate(self, url, push=True):
        url = clean_href(to_url(url))
        self.loading, self.status = True, "Loading " + url
        self.load_id += 1
        lid = self.load_id
        threading.Thread(target=lambda: self.inbox.put((lid, url, push, fetch_any(url))), daemon=True).start()

    def back(self):
        if self.hpos > 0:
            self.hpos -= 1
            self.navigate(self.history[self.hpos][0], push=False)

    def forward(self):
        if self.hpos < len(self.history) - 1:
            self.hpos += 1
            self.navigate(self.history[self.hpos][0], push=False)

    def reload(self):
        self.navigate(self.page.url, push=False)

    def follow(self, i):
        if 0 <= i < len(self.page.links):
            self.visited.add(self.page.links[i])
            self.navigate(self.page.links[i])

    # ---------- images ----------
    def _load_images(self, page, lid):
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)
        self.pool = concurrent.futures.ThreadPoolExecutor(IMAGE_WORKERS, thread_name_prefix="img")
        pending = [i for i, im in enumerate(page.images) if im.get("state") is None]
        for n, i in enumerate(pending):
            if n >= MAX_IMAGES:
                page.images[i]["state"] = "skipped"
                continue
            page.images[i]["state"] = "loading"
            self.pool.submit(self._load_one, page, lid, i)

    def _load_one(self, page, lid, i):
        if lid != self.load_id:
            return
        im = page.images[i]
        try:
            img = images.load(im["src"], referer=page.url)
            im["img"] = img
            im["state"] = "icon" if img.width <= ICON_SIZE and img.height <= ICON_SIZE else "ok"
        except Exception as e:
            im["error"] = str(e)[:120]
            im["state"] = "failed"
        self.inbox.put(("img", lid, i))

    def close(self):
        self.load_id += 1                  # drop any results still in flight
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def set_images(self, on):
        self.show_images = bool(on)
        if self.show_images:
            self._load_images(self.page, self.load_id)
        self.status = "images on" if self.show_images else "images off"
        self._relayout()

    def _scaled(self, i):
        """Image i scaled to the current layout's box and snapped to the palette (cached)."""
        key = (i,) + self.img_box
        if key not in self.scaled:
            if len(self.scaled) > 80:
                self.scaled.clear()
            src = self.page.images[i]["img"]
            self.scaled[key] = images.to_palette(images.fit(src, *self.img_box))
        return self.scaled[key]

    def _relayout(self):
        """Re-wrap after an image arrives without moving what's on screen."""
        if self.layout_cols is None or not self.block_line:
            return
        b = bisect.bisect_right(self.block_line, self.scroll) - 1
        off = self.scroll - self.block_line[b] if b >= 0 else 0
        cols, self.layout_cols = self.layout_cols, None
        self.layout(cols)
        if b >= 0:
            self.scroll = self.block_line[b] + off

    def tick(self):
        changed = False
        while True:
            try:
                msg = self.inbox.get_nowait()
            except queue.Empty:
                return changed
            if msg[0] == "img":
                if msg[1] == self.load_id:
                    self._relayout()
                    changed = True
                continue
            lid, url, push, page = msg
            if lid != self.load_id:
                continue                       # superseded by a newer navigation
            changed = True
            self.scaled.clear()
            if self.hpos >= 0 and push:
                self.history[self.hpos] = (self.history[self.hpos][0], self.scroll)
            self.page, self.loading = page, False
            if push:
                del self.history[self.hpos + 1:]
                self.history.append((page.url, 0))
                self.hpos = len(self.history) - 1
                self.scroll = 0
                if page.start:
                    self.layout_cols = None
                    self.layout(self._geom()[2])
                    self.scroll = self.block_line[page.start]
            else:
                self.history[self.hpos] = (page.url, self.history[self.hpos][1])
                self.scroll = self.history[self.hpos][1]
            self.layout_cols, self.sel = None, None
            self.title = (page.title or page.url)[:40] + " - Browser"
            self.status = page.error or f"{len(page.links)} links, {len(page.images)} images"
            if self.show_images:
                self._load_images(page, lid)
            if page.error:
                self.page.blocks = [{"kind": "h2", "runs": [("Can't load page", "bold", None)], "depth": 0},
                                    {"kind": "p", "runs": [(page.error, "text", None)], "depth": 0},
                                    {"kind": "p", "runs": [(page.url, "dim", None)], "depth": 0}]

    # ---------- layout ----------
    def layout(self, cols):
        """Wrap blocks to `cols` columns -> list of lines; each line = (kind, [(col, text, style, link)])."""
        if cols == self.layout_cols:
            return self.lines
        lines = []
        self.block_line = []          # first line of each block
        rows = self._geom()[3]
        self.img_box = (max(16, cols * CW), max(CH * 2, rows * CH * 3 // 4))
        for b in self.page.blocks:
            self.block_line.append(len(lines))
            kind = b["kind"]
            if kind == "img":
                # A loaded image spans as many text rows as it needs; each row draws its own slice,
                # so scrolling and clipping work line by line like text. kind = ("img", index, row).
                i = b["img"]
                im = self.page.images[i]
                state = im.get("state")
                if state == "ok" and self.show_images:
                    n = math.ceil(self._scaled(i).height / CH)
                    lines += [(("img", i, k), []) for k in range(n)]
                    lines.append(("gap", []))
                elif state != "icon" or im["alt"]:
                    label = im["alt"] or "image"
                    text = {"loading": f"[loading: {label}]", "failed": f"[image failed: {label}]"}.get(
                        state, f"[image: {label}]" if state != "icon" else f"[{label}]")
                    lines.append(("p", [(0, text[:cols], "dim" if im["link"] is None else "link", im["link"])]))
                continue
            if kind == "hr":
                lines.append(("hr", []))
                continue
            if kind == "pre":
                text = "".join(r[0] for r in b["runs"])
                for ln in text.expandtabs(4).split("\n"):
                    while True:
                        lines.append(("pre", [(0, ln[:cols], "code", None)]))
                        ln = ln[cols:]
                        if not ln:
                            break
                lines.append(("gap", []))
                continue
            indent = ""
            if kind == "li":
                indent = "  " * max(0, b["depth"] - 1)
                first_prefix = indent + "• "
                indent += "  "
            elif kind == "blockquote":
                first_prefix = indent = "│ "
            elif kind == "dd":
                first_prefix = indent = "    "
            else:
                first_prefix = ""
            first_prefix, indent = first_prefix[:cols // 3], indent[:cols // 3]
            # Split runs into words, keeping each word's style and link.
            words = []
            for text, style, link in b["runs"]:
                for m in re.finditer(r"\S+\s*|\s+", text):
                    words.append((m.group(0), style, link))
            cur, col, prefix = [], len(first_prefix), first_prefix
            def emit():
                lines.append((kind, [(0, prefix, "dim", None)] + cur if prefix else cur))
            for word, style, link in words:
                bare = word.rstrip()
                if col + len(bare) > cols and col > len(prefix):
                    emit()
                    cur, prefix = [], indent
                    col = len(prefix)
                    word = word.lstrip()
                while len(word.rstrip()) > cols - len(prefix) and cols - len(prefix) > 4:
                    take = cols - col
                    cur.append((col, word[:take], style, link))
                    emit()
                    cur, prefix, word = [], indent, word[take:]
                    col = len(prefix)
                if not word:
                    continue
                if cur and cur[-1][2] == style and cur[-1][3] == link:
                    c0, t0, _, _ = cur[-1]
                    cur[-1] = (c0, t0 + word, style, link)
                else:
                    cur.append((col, word, style, link))
                col += len(word)
            if cur:
                emit()
            if kind != "li" and kind != "dt":
                lines.append(("gap", []))
        self.lines, self.layout_cols = lines, cols
        return lines

    def _geom(self):
        w, h = self.desk.client_size(self)
        cols = max(10, (w - 12) // CW)
        rows = max(1, (h - TOOLBAR_H - CH - 6) // CH)
        return w, h, cols, rows

    def scroll_by(self, n):
        _, _, cols, rows = self._geom()
        lines = self.layout(cols)
        self.scroll = max(0, min(max(0, len(lines) - rows), self.scroll + n))

    def find(self, text, start=None):
        _, _, cols, rows = self._geom()
        text = text.lower()
        lines = self.layout(cols)
        begin = self.scroll + 1 if start is None else start
        order = list(range(begin, len(lines))) + list(range(0, begin))
        for i in order:
            if text in "".join(s[1] for s in lines[i][1]).lower():
                self.scroll = max(0, min(i - 2, max(0, len(lines) - rows)))
                self.status = f"found '{text}' at line {i + 1}"
                return i
        self.status = f"'{text}' not found"
        return None

    def _select_link(self, step):
        _, _, cols, rows = self._geom()
        lines = self.layout(cols)
        visible = []
        for i in range(self.scroll, min(len(lines), self.scroll + rows)):
            for seg in lines[i][1]:
                if seg[3] is not None and seg[3] not in visible:
                    visible.append(seg[3])
        if not visible:
            self.sel = None
            return
        if self.sel in visible:
            self.sel = visible[(visible.index(self.sel) + step) % len(visible)]
        else:
            self.sel = visible[0 if step > 0 else -1]
        self.status = self.page.links[self.sel]

    # ---------- input ----------
    def key(self, k):
        if not k.down:
            return
        if self.editing or self.finding:
            buf = self.addr if self.editing else self.find_text
            if k.name == "RETURN":
                if self.editing:
                    self.editing = False
                    self.navigate(buf)
                else:
                    self.finding = False
                    self.find(buf)
                return
            if k.name == "ESCAPE":
                self.editing = self.finding = False
                return
            if k.name == "BACKSPACE":
                buf = "" if self.addr_fresh else buf[:-1]
            elif k.char:
                buf = (k.char if self.addr_fresh else buf + k.char)
            self.addr_fresh = False
            if self.editing:
                self.addr = buf
            else:
                self.find_text = buf
            return
        _, _, cols, rows = self._geom()
        if k.ctrl and k.name in ("l", "L") or k.name == "F6":
            self.editing, self.addr, self.addr_fresh = True, self.page.url, True
        elif k.ctrl and k.name in ("f", "F"):
            self.finding, self.find_text = True, ""
        elif k.name == "F5" or (k.ctrl and k.name in ("r", "R")):
            self.reload()
        elif (k.alt and k.name == "LEFT") or k.name == "BACKSPACE":
            self.back()
        elif k.alt and k.name == "RIGHT":
            self.forward()
        elif k.name == "UP":
            self.scroll_by(-1)
        elif k.name == "DOWN":
            self.scroll_by(1)
        elif k.name == "PAGEUP" or (k.name == "SPACE" and k.shift):
            self.scroll_by(-(rows - 1))
        elif k.name in ("PAGEDOWN", "SPACE"):
            self.scroll_by(rows - 1)
        elif k.name == "HOME":
            self.scroll = 0
        elif k.name == "END":
            self.scroll_by(1 << 30)
        elif k.ctrl and k.name in ("i", "I"):
            self.set_images(not self.show_images)
        elif k.name == "TAB":
            self._select_link(-1 if k.shift else 1)
        elif k.name == "RETURN" and self.sel is not None:
            self.follow(self.sel)

    def _buttons(self, w):
        return [("back", 4, 24), ("fwd", 30, 24), ("reload", 56, 24), ("home", 82, 24)], (110, w - 114)

    def mouse(self, x, y, buttons, pressed, released, wheel):
        w, h, cols, rows = self._geom()
        if wheel:
            self.scroll_by(-wheel * 3)
        if not pressed & 1:
            return
        if y < TOOLBAR_H:
            btns, (ax, aw) = self._buttons(w)
            for name, bx, bw in btns:
                if bx <= x < bx + bw:
                    {"back": self.back, "fwd": self.forward, "reload": self.reload,
                     "home": lambda: self.navigate("about:home")}[name]()
                    return
            if ax <= x < ax + aw:
                self.editing, self.addr, self.addr_fresh = True, self.page.url, True
            return
        self.editing = self.finding = False
        row = self.scroll + (y - TOOLBAR_H - 2) // CH
        col = (x - 6) // CW
        lines = self.layout(cols)
        if 0 <= row < len(lines):
            kind = lines[row][0]
            if isinstance(kind, tuple):
                im = self.page.images[kind[1]]
                if x - 6 < self._scaled(kind[1]).width:
                    if im["link"] is not None:
                        self.follow(im["link"])
                    else:                       # an unlinked image opens full size in the viewer
                        self.desk.open("viewer", url=im["src"], image=im["img"])
                return
            for c0, text, style, link in lines[row][1]:
                if link is not None and c0 <= col < c0 + len(text):
                    self.follow(link)
                    return

    # ---------- drawing ----------
    def draw(self, d, x, y, w, h):
        _, _, cols, rows = self._geom()
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.white)
        # toolbar
        d.rectangle([x, y, x + w - 1, y + TOOLBAR_H - 1], fill=C.gray)
        btns, (ax, aw) = self._buttons(w)
        glyph = {"back": "<", "fwd": ">", "reload": "R", "home": "H"}
        for name, bx, bw in btns:
            enabled = {"back": self.hpos > 0, "fwd": self.hpos < len(self.history) - 1}.get(name, True)
            bevel(d, x + bx, y + 2, bw, TOOLBAR_H - 4)
            d.text((x + bx + 8, y + 5), glyph[name], font=FONT, fill=C.black if enabled else C.darkgray)
        bevel(d, x + ax, y + 2, aw, TOOLBAR_H - 4, raised=False, face=C.white)
        if self.editing or self.finding:
            label = self.addr if self.editing else "Find: " + self.find_text
            maxc = max(1, (aw - 10) // CW)
            shown = label[-maxc + 1:] + "_"
            if self.addr_fresh and self.editing:
                d.rectangle([x + ax + 3, y + 5, x + ax + 4 + min(len(label), maxc) * CW, y + 5 + CH - 3], fill=C.sky)
                shown = label[:maxc]
            d.text((x + ax + 4, y + 5), shown, font=FONT, fill=C.black)
        else:
            maxc = max(1, (aw - 10) // CW)
            d.text((x + ax + 4, y + 5), self.page.url[:maxc], font=FONT, fill=C.darkgray if self.loading else C.black)
        # content
        lines = self.layout(cols)
        self.scroll = min(self.scroll, max(0, len(lines) - rows))
        top = y + TOOLBAR_H + 2
        for i, (kind, segs) in enumerate(lines[self.scroll:self.scroll + rows]):
            ly = top + i * CH
            if kind == "hr":
                d.line([x + 6, ly + CH // 2, x + w - 8, ly + CH // 2], fill=C.gray)
                continue
            if isinstance(kind, tuple):                 # one CH-tall slice of an image
                _, ii, k = kind
                im = self._scaled(ii)
                piece = im.crop((0, k * CH, im.width, min(im.height, (k + 1) * CH)))
                self.desk.target.paste(piece, (x + 6, ly))
                link = self.page.images[ii]["link"]
                if link is not None and link == self.sel:
                    d.rectangle([x + 5, ly, x + 6 + im.width, ly + piece.height - 1], outline=C.yellow, width=2)
                continue
            head = HEAD_COLORS.get(kind) or (C.teal if kind in ("h4", "h5", "h6") else None)
            for c0, text, style, link in segs:
                px = x + 6 + c0 * CW
                col = head if head and style != "link" else STYLE_COLORS.get(style, C.black)
                if link is not None and self.page.links[link] in self.visited:
                    col = C.purple
                if link is not None and link == self.sel:
                    d.rectangle([px - 1, ly, px + len(text.rstrip()) * CW, ly + CH - 2], fill=C.yellow)
                d.text((px, ly), text, font=FONT, fill=col)
                if style == "bold" or (head and kind in ("h1", "h2")):
                    d.text((px + 1, ly), text, font=FONT, fill=col)
                if style == "link":
                    tw = len(text.rstrip()) * CW
                    d.line([px, ly + CH - 3, px + tw, ly + CH - 3], fill=col)
        # scrollbar
        if len(lines) > rows:
            th = max(10, (h - TOOLBAR_H - CH - 4) * rows // len(lines))
            ty = top + (h - TOOLBAR_H - CH - 4 - th) * self.scroll // max(1, len(lines) - rows)
            d.rectangle([x + w - 5, ty, x + w - 2, ty + th], fill=C.darkgray)
        # status bar
        sy = y + h - CH - 2
        d.rectangle([x, sy, x + w - 1, y + h - 1], fill=C.gray)
        pct = 100 if len(lines) <= rows else 100 * self.scroll // max(1, len(lines) - rows)
        status = ("Loading..." if self.loading else self.status)
        right = f"{pct}%"
        d.text((x + 4, sy + 1), status[:max(1, cols - len(right) - 1)], font=FONT, fill=C.black)
        d.text((x + w - 6 - len(right) * CW, sy + 1), right, font=FONT, fill=C.black)

    # ---------- agent ----------
    def read(self, max_chars=6000, links=True, visible_only=False):
        _, _, cols, rows = self._geom()
        if visible_only:
            lines = self.layout(cols)[self.scroll:self.scroll + rows]
            text = "\n".join("".join(s[1] for s in segs) for _, segs in lines)
        else:
            text = self.page.plain_text()
        out = {"url": self.page.url, "title": self.page.title, "loading": self.loading, "error": self.page.error,
               "scroll_line": self.scroll, "total_lines": len(self.layout(cols)),
               "text": text[:max_chars], "truncated": len(text) > max_chars}
        if links:
            out["links"] = [{"index": i, "text": re.sub(r"\s+", " ", t).strip()[:80], "href": clean_href(h)}
                            for i, (t, h) in enumerate(zip(self.page.link_text, self.page.links))][:300]
            out["images"] = [{"index": i, "alt": im["alt"], "src": im["src"][:300], "state": im.get("state"),
                              "size": list(im["img"].size) if im.get("img") else None, "link": im["link"]}
                             for i, im in enumerate(self.page.images)][:100]
        return out

    def state(self):
        return {"url": self.page.url, "title": self.page.title, "loading": self.loading,
                "links": len(self.page.links), "images": len(self.page.images),
                "images_loaded": sum(im.get("state") == "ok" for im in self.page.images),
                "show_images": self.show_images, "can_back": self.hpos > 0,
                "can_forward": self.hpos < len(self.history) - 1}
