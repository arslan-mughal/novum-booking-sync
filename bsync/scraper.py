"""Booking.com page scraping (Playwright).

IMPORTANT - what is verified and what is not:
  * `extract_embedded()` and `assemble()` are pure functions covered by unit tests.
  * The browser flow below was exercised against a local MOCK page, not against the live site.
    Booking.com changes its markup and sits behind an AWS WAF challenge, so every CSS selector here is a
    best effort. They all live in SELECTORS - adjust them there. Run one hotel with `--only <Sr.> --debug`
    and look at the saved screenshots / HTML in <run-dir>/debug when something does not match.
  * Whatever the scraper finds is cross-checked by diff.py against the ~13.8k images you already have, so a
    broken selector shows up as LOW_COVERAGE / ROOMS_MISSING instead of silently wrong rows.

What counts as a hotel photo on a page:
  * a photo seen in THIS hotel's gallery pop-up (untagged unless it is also tagged to a room), or
  * a photo shown inside a room's own pop-up, or tagged to a room in the page's embedded JSON.
  Photos that merely appear somewhere on the page (e.g. 'similar properties') are ignored.
"""
from __future__ import annotations

import json
import random
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .model import HotelScrape, ScrapedImage
from .util import PHOTO_RE, clean_text_for_urls, norm_category, photo_id, size_score

# ----------------------------------------------------------------------------------------- selectors
SELECTORS = {
    "cookie_accept": ["#onetrust-accept-btn-handler", "button:has-text('Akzeptieren')",
                      "button:has-text('Accept')"],
    "overlay_close": ["[role='dialog'] button[aria-label*='Dismiss' i]",
                      "[role='dialog'] button[aria-label*='schließen' i]",
                      "button[aria-label*='Anmeldeinformationen' i]"],
    # elements that open the hotel's photo gallery pop-up
    "gallery_open": ["[data-testid='GalleryGridViewDesktop-wrapper'] img", "a.bh-photo-grid-item",
                     ".bh-photo-grid img", "#photo_wrapper img", "[data-testid*='gallery' i] img",
                     "[data-testid*='photo' i] img"],
    # the pop-up container
    "dialog": "[role='dialog'], [aria-modal='true'], .bh-photo-modal",
    "dialog_close": ["[role='dialog'] button[aria-label*='close' i]",
                     "[role='dialog'] button[aria-label*='schließen' i]",
                     "[role='dialog'] button[aria-label*='Schließen' i]", ".modal-mask-closeBtn"],
    # the clickable room names in the availability table (each opens that room's photo pop-up)
    # NOTE: the link's whole text is used as the room name, so pick elements that contain only the name.
    "room_links": ["a.hprt-roomtype-link", "[data-testid='room-name-link']", ".hprt-roomtype-icon-link",
                   "a[data-room-id]", "#hprt-table a[data-room-id]"],
}

CHALLENGE_WORDS = ("verify you are human", "confirm you are human", "ich bin kein roboter",
                   "bestätigen sie, dass sie ein mensch", "captcha", "awswaf", "just a moment",
                   "javascript is disabled")

JS_HOTEL_PAGE = """() => {
  const ld = [...document.querySelectorAll('script[type="application/ld+json"]')]
     .some(s => /"@type"\\s*:\\s*"(Hotel|Hostel|Apartment|BedAndBreakfast|LodgingBusiness|Resort|Motel|VacationRental|Accommodation)/.test(s.textContent));
  const dom = !!document.querySelector('#hprt-table, #hp_hotel_name, [data-testid="property-header"], [data-testid="title"]');
  return ld || dom;
}"""

# Collects hotel-photo URLs (+ alt text) inside a scope element: <img>, srcset, <source>, inline backgrounds.
JS_COLLECT = r"""(sel) => {
  const scope = sel ? document.querySelector(sel) : document.body;
  if (!scope) return [];
  const out = [];
  const re = /\/images\/hotel\//;
  const push = (u, alt) => { if (u && re.test(u)) out.push([u, alt || '']); };
  const fromSet = (s) => (s || '').split(',').map(x => x.trim().split(/\s+/)[0]).filter(Boolean);
  scope.querySelectorAll('img').forEach(i => {
    const alt = i.getAttribute('alt') || '';
    [i.currentSrc, i.src, i.getAttribute('data-src'), i.getAttribute('data-lazy')].forEach(u => push(u, alt));
    fromSet(i.getAttribute('srcset')).forEach(u => push(u, alt));
  });
  scope.querySelectorAll('source').forEach(s => fromSet(s.getAttribute('srcset')).forEach(u => push(u, '')));
  scope.querySelectorAll('[style*="bstatic"]').forEach(e => {
    const m = (e.getAttribute('style') || '').match(/url\(["']?([^"')]+)/); if (m) push(m[1], e.getAttribute('aria-label') || '');
  });
  return out;
}"""

# Scrolls the largest scrollable element inside the scope. Returns true if it moved.
JS_SCROLL = r"""(sel) => {
  const scope = sel ? document.querySelector(sel) : document.scrollingElement;
  if (!scope) return false;
  const cands = [scope, ...scope.querySelectorAll('*')].filter(e =>
      e.scrollHeight > e.clientHeight + 40 && /(auto|scroll)/.test(getComputedStyle(e).overflowY));
  if (!cands.length) return false;
  cands.sort((a, b) => b.scrollHeight - a.scrollHeight);
  const el = cands[0], before = el.scrollTop;
  el.scrollTop = before + Math.max(300, el.clientHeight * 0.9);
  return el.scrollTop > before + 1;
}"""


# ----------------------------------------------------------------------------------------- embedded JSON
@dataclass
class PhotoRec:
    pid: str
    url: str = ""
    caption: str = ""
    room_refs: list = field(default_factory=list)

    def add_url(self, u: str):
        if not self.url or size_score(u) > size_score(self.url):
            self.url = u


@dataclass
class Embedded:
    photos: dict = field(default_factory=dict)     # pid -> PhotoRec
    rooms: dict = field(default_factory=dict)      # room id -> room name
    stats: dict = field(default_factory=dict)

    def room_names_for(self, rec: PhotoRec) -> list:
        names = []
        for ref in rec.room_refs:
            if ref in self.rooms:
                names.append(self.rooms[ref])
            elif not ref.isdigit() and re.search(r"[A-Za-zÄÖÜäöüß]{3}", ref) and ref.count(":") == 0:
                names.append(ref)                    # tags given directly as names
        return list(OrderedDict.fromkeys(names))


_ROOM_ID_KEYS = ("roomId", "room_id", "b_room_id", "b_id", "id")
_ROOM_NAME_KEYS = ("roomName", "room_name", "b_name", "name", "title")
_CAPTION_KEYS = ("caption", "alt", "alt_text", "altText", "description", "title")


def _scalar_refs(v) -> list:
    out = []
    if isinstance(v, (str, int)) and not isinstance(v, bool):
        out.append(str(v).split(":")[-1] if (isinstance(v, str) and v.startswith(("Room", "room")) and ":" in v)
                   else str(v))
    elif isinstance(v, dict):
        if "__ref" in v:
            out.append(str(v["__ref"]).split(":")[-1])
        else:
            for k in ("id", "roomId", "room_id", "name"):
                if k in v and isinstance(v[k], (str, int)):
                    out.append(str(v[k]))
                    break
    elif isinstance(v, list):
        for x in v:
            out.extend(_scalar_refs(x))
    return out


def _walk(node, path: tuple, emb: Embedded):
    if isinstance(node, dict):
        typename = str(node.get("__typename", ""))
        urls = [v for v in node.values() if isinstance(v, str) and PHOTO_RE.search(v)]
        if urls:
            best = max((PHOTO_RE.search(u).group(0) for u in urls), key=size_score)
            pid = photo_id(best)
            rec = emb.photos.setdefault(pid, PhotoRec(pid=pid))
            rec.add_url(best)
            for k in _CAPTION_KEYS:
                if isinstance(node.get(k), str) and node[k].strip() and not rec.caption:
                    rec.caption = node[k].strip()
            for k, v in node.items():
                if "room" in k.lower():
                    rec.room_refs.extend(_scalar_refs(v))
        else:
            in_room_ctx = "room" in typename.lower() or any("room" in str(p).lower() for p in path[-3:])
            rid = next((node[k] for k in _ROOM_ID_KEYS if isinstance(node.get(k), (str, int))
                        and not isinstance(node.get(k), bool)), None)
            rname = next((node[k] for k in _ROOM_NAME_KEYS if isinstance(node.get(k), str)), None)
            if in_room_ctx and rid is not None and rname and 2 < len(rname) < 160 and not rname.startswith("http"):
                emb.rooms[str(rid).split(":")[-1]] = rname.strip()
        for k, v in node.items():
            _walk(v, path + (k,), emb)
    elif isinstance(node, list):
        for x in node:
            _walk(x, path, emb)


def extract_embedded(html_text: str) -> Embedded:
    """Parse every JSON <script> block of the page source (Apollo state, JSON-LD, ...)."""
    emb = Embedded()
    n_blocks = 0
    for m in re.finditer(r"<script([^>]*)>(.*?)</script>", html_text, re.S | re.I):
        attrs, body = m.group(1), m.group(2).strip()
        if "json" not in attrs.lower() or not body or body[0] not in "{[":
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue
        n_blocks += 1
        _walk(data, (), emb)
    tagged = sum(1 for r in emb.photos.values() if emb.room_names_for(r))
    emb.stats = {"json_blocks": n_blocks, "photos": len(emb.photos), "rooms": len(emb.rooms),
                 "photos_tagged_to_rooms": tagged}
    return emb


# ----------------------------------------------------------------------------------------- assembling
def _best(a: str, b: str) -> str:
    if not a:
        return b
    if not b:
        return a
    return a if size_score(a) >= size_score(b) else b


def assemble(emb: Embedded, gallery: "OrderedDict", rooms_dom: list) -> list:
    """Combine the three sources into ScrapedImage rows.

    gallery:   OrderedDict pid -> (url, alt)            photos seen in the hotel's gallery pop-up
    rooms_dom: [(room name, OrderedDict pid -> (url, alt)), ...]   photos seen in each room pop-up
    A photo that belongs to any room is NOT also emitted as an untagged gallery photo.
    """
    tagged: "OrderedDict[tuple, ScrapedImage]" = OrderedDict()
    canon = {norm_category(name): name for name, _ in rooms_dom}
    order = 0
    for room, photos in rooms_dom:
        for pid, (url, alt) in photos.items():
            url = _best(url, emb.photos[pid].url if pid in emb.photos else "")
            tagged[(room, pid)] = ScrapedImage(pid=pid, url=url, category=room, caption=alt, order=order)
            order += 1
    for rec in emb.photos.values():
        for name in emb.room_names_for(rec):
            room = canon.get(norm_category(name), name)
            if (room, rec.pid) not in tagged:
                tagged[(room, rec.pid)] = ScrapedImage(pid=rec.pid, url=rec.url, category=room,
                                                       caption=rec.caption, order=order)
                order += 1
    tagged_pids = {pid for _, pid in tagged}
    out = []
    for i, (pid, (url, alt)) in enumerate(gallery.items()):
        if pid in tagged_pids:
            continue
        out.append(ScrapedImage(pid=pid, url=_best(url, emb.photos[pid].url if pid in emb.photos else ""),
                                category=None, caption=alt, order=-1000000 + i))
    return out + list(tagged.values())


def clean_caption(alt: str, hotel_hint: str = "") -> str:
    """Booking alt texts are often just the hotel name; only keep ones that carry information."""
    a = re.sub(r"\s+", " ", alt or "").strip()
    if len(a) < 4 or re.match(r"^(photo|image|bild|foto)\b", a, re.I):
        return ""
    if hotel_hint and hotel_hint.casefold() in a.casefold():
        return ""
    return a


def with_dates(url: str, checkin: Optional[str], checkout: Optional[str]) -> str:
    if not checkin:
        return url
    p = urlparse(url)
    q = dict(parse_qsl(p.query))
    q.update({"checkin": checkin, "checkout": checkout, "group_adults": "2", "no_rooms": "1",
              "group_children": "0"})
    return urlunparse(p._replace(query=urlencode(q)))


def default_dates(days_ahead: int = 45) -> tuple:
    d = date.today() + timedelta(days=days_ahead)
    return d.isoformat(), (d + timedelta(days=1)).isoformat()


# ----------------------------------------------------------------------------------------- browser
class BookingBrowser:
    def __init__(self, profile_dir: Path, debug_dir: Optional[Path] = None, headed: bool = True,
                 channel: str = "chrome", locale: str = "de-DE", challenge_wait: int = 300,
                 pace: tuple = (0.7, 1.5), log=print):
        self.profile_dir, self.debug_dir = profile_dir, debug_dir
        self.headed, self.channel, self.locale = headed, channel, locale
        self.challenge_wait, self.pace, self.log = challenge_wait, pace, log
        self._pw = self._ctx = None

    # -- lifecycle
    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        kw = dict(user_data_dir=str(self.profile_dir), headless=not self.headed, locale=self.locale,
                  timezone_id="Europe/Berlin", viewport={"width": 1440, "height": 900},
                  args=["--disable-blink-features=AutomationControlled"])
        try:
            if self.channel and self.channel != "chromium":
                self._ctx = self._pw.chromium.launch_persistent_context(channel=self.channel, **kw)
            else:
                raise RuntimeError("bundled chromium requested")
        except Exception as e:
            if self.channel and self.channel != "chromium":
                self.log(f"  could not start '{self.channel}' ({str(e).splitlines()[0][:90]}); "
                         f"falling back to bundled Chromium")
            self._ctx = self._pw.chromium.launch_persistent_context(**kw)
        return self

    def __exit__(self, *exc):
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    # -- helpers
    def _pause(self, mult: float = 1.0):
        time.sleep(random.uniform(*self.pace) * mult)

    def _shot(self, page, sr, label: str):
        if not self.debug_dir:
            return
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(self.debug_dir / f"{sr:03d}_{label}.png"), full_page=False)
        except Exception:
            pass

    def _click_first(self, page, selectors, timeout=1500) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if loc.count():
                    loc.first.click(timeout=timeout)
                    return True
            except Exception:
                continue
        return False

    def _is_challenge(self, page) -> bool:
        try:
            blob = (page.title() + " " + page.inner_text("body", timeout=1500)[:1500]).lower()
        except Exception:
            return True
        try:
            is_hotel = page.evaluate(JS_HOTEL_PAGE)
        except Exception:
            is_hotel = False
        return any(w in blob for w in CHALLENGE_WORDS) and not is_hotel

    def _await_hotel_page(self, page, notes: list) -> str:
        deadline, warned = time.time() + self.challenge_wait, False
        while time.time() < deadline:
            try:
                if page.evaluate(JS_HOTEL_PAGE):
                    return "OK"
            except Exception:
                pass                                    # navigation in progress (challenge reload)
            if self._is_challenge(page):
                if not self.headed:
                    notes.append("bot challenge in headless mode - run without --headless and solve it once")
                    return "BLOCKED"
                if not warned:
                    self.log("  >>> Booking.com is showing a verification page. Solve it in the browser "
                             "window - the run continues automatically.")
                    warned = True
            elif time.time() > deadline - self.challenge_wait + 25:
                notes.append("page loaded but does not look like a hotel page (moved/removed?)")
                return "NOT_FOUND"
            time.sleep(2)
        notes.append("timed out waiting for the hotel page / verification")
        return "BLOCKED"

    def _dismiss_overlays(self, page):
        self._click_first(page, SELECTORS["cookie_accept"], 2500)
        page.wait_for_timeout(500)
        self._click_first(page, SELECTORS["overlay_close"], 800)

    def _wait_dialog(self, page, timeout=4500) -> bool:
        try:
            page.wait_for_selector(SELECTORS["dialog"], state="visible", timeout=timeout)
            return True
        except Exception:
            return False

    def _close_dialog(self, page):
        if self._click_first(page, SELECTORS["dialog_close"], 1200):
            page.wait_for_timeout(250)
        for _ in range(3):
            try:
                if not page.locator(SELECTORS["dialog"]).filter(visible=True).count():
                    return
            except Exception:
                return
            page.keyboard.press("Escape")
            page.wait_for_timeout(350)

    def _collect(self, page, scope_sel: Optional[str], max_rounds: int = 60) -> "OrderedDict":
        """Scroll the scope until nothing new appears; return OrderedDict pid -> (best url, alt)."""
        found: "OrderedDict[str, tuple]" = OrderedDict()
        idle = 0
        for _ in range(max_rounds):
            before = len(found)
            for url, alt in page.evaluate(JS_COLLECT, scope_sel):
                pid = photo_id(clean_text_for_urls(url))
                if not pid:
                    continue
                url = PHOTO_RE.search(clean_text_for_urls(url)).group(0)
                if pid not in found or size_score(url) > size_score(found[pid][0]):
                    found[pid] = (url, found.get(pid, ("", alt))[1] or alt)
            moved = page.evaluate(JS_SCROLL, scope_sel)
            page.wait_for_timeout(450)
            idle = idle + 1 if (len(found) == before and not moved) else 0
            if idle >= 2:
                break
        return found

    def _dialog_scope(self, page) -> str:
        """Tag the last visible dialog with a data attribute so JS can address exactly that element."""
        page.evaluate("""(sel) => {
            document.querySelectorAll('[data-sync-scope]').forEach(e => e.removeAttribute('data-sync-scope'));
            const all = [...document.querySelectorAll(sel)].filter(e => e.offsetParent !== null || getComputedStyle(e).position === 'fixed');
            const el = all[all.length - 1]; if (el) el.setAttribute('data-sync-scope', '1'); }""",
                      SELECTORS["dialog"])
        return "[data-sync-scope='1']"

    # -- the three harvesting steps
    def _harvest_gallery(self, page, sr, diag) -> "OrderedDict":
        opened = False
        for sel in SELECTORS["gallery_open"]:
            try:
                loc = page.locator(sel)
                n = loc.count()
                if not n:
                    continue
                loc.first.scroll_into_view_if_needed(timeout=2000)
                loc.first.click(timeout=3000)
            except Exception:
                continue
            if self._wait_dialog(page):
                opened = True
                diag["gallery_selector"] = sel
                break
        if not opened:                                   # generic fallback: click the first big hotel photo
            try:
                handle = page.evaluate_handle("""() => [...document.images].find(i =>
                    /\\/images\\/hotel\\//.test(i.currentSrc || i.src) && i.getBoundingClientRect().width > 150 &&
                    i.getBoundingClientRect().top < 900)""")
                el = handle.as_element()
                if el:
                    el.click(timeout=3000)
                    opened = self._wait_dialog(page)
                    diag["gallery_selector"] = "<generic first photo>"
            except Exception:
                pass
        diag["gallery_opened"] = opened
        if not opened:
            return OrderedDict()
        page.wait_for_timeout(800)
        self._shot(page, sr, "gallery_open")
        photos = self._collect(page, self._dialog_scope(page))
        self._close_dialog(page)
        return photos

    def _room_link_selector(self, page) -> Optional[str]:
        for sel in SELECTORS["room_links"]:
            try:
                if page.locator(sel).count():
                    return sel
            except Exception:
                continue
        return None

    def _harvest_rooms(self, page, sr, diag) -> list:
        sel = self._room_link_selector(page)
        diag["room_link_selector"] = sel
        if not sel:
            diag["room_links"] = 0
            return []
        n = page.locator(sel).count()
        diag["room_links"] = n
        out, done, opened = [], set(), 0
        for i in range(n):
            try:
                link = page.locator(sel).nth(i)
                name = re.sub(r"\s+", " ", link.inner_text(timeout=2000) or "").strip()
                if not name or name in done:
                    continue
                done.add(name)
                link.scroll_into_view_if_needed(timeout=2500)
                link.click(timeout=3500)
            except Exception:
                continue
            self._pause()
            if not self._wait_dialog(page, 4000):
                continue
            opened += 1
            if opened == 1:
                self._shot(page, sr, "room_open")
            photos = self._collect(page, self._dialog_scope(page), max_rounds=25)
            out.append((name, photos))
            self._close_dialog(page)
        diag["room_popups_opened"] = opened
        diag["room_photos"] = sum(len(p) for _, p in out)
        return out

    # -- public
    def scrape(self, sr: int, url: str, hotel_name: str, dates: tuple) -> HotelScrape:
        res = HotelScrape(page_url=url, scraped_at=datetime.now().isoformat(timespec="seconds"))
        page = self._ctx.new_page()
        try:
            page.goto(with_dates(url, *dates), wait_until="domcontentloaded", timeout=60000)
            status = self._await_hotel_page(page, res.notes)
            if status != "OK":
                res.status = status
                self._shot(page, sr, f"status_{status}")
                return res
            self._dismiss_overlays(page)
            page.wait_for_timeout(1500)
            html_text = page.content()
            if self.debug_dir:
                self.debug_dir.mkdir(parents=True, exist_ok=True)
                (self.debug_dir / f"{sr:03d}_page.html").write_text(html_text, encoding="utf-8")
            emb = extract_embedded(html_text)
            diag = {"embedded": emb.stats}
            gallery = self._harvest_gallery(page, sr, diag)
            diag["gallery_photos"] = len(gallery)
            rooms = self._harvest_rooms(page, sr, diag)
            res.diagnostics = diag
            res.images = assemble(emb, gallery, rooms)
            for im in res.images:
                im.caption = clean_caption(im.caption, hotel_name)
            res.status = "OK" if res.images else "NO_PHOTOS"
            if not res.images:
                res.notes.append("no hotel photos found - selectors probably need adjusting (see --debug)")
        except Exception as e:                                   # keep the batch going
            res.status = "ERROR"
            res.notes.append(f"{type(e).__name__}: {str(e).splitlines()[0][:200]}")
        finally:
            try:
                page.close()
            except Exception:
                pass
        return res
