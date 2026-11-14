"""Small pure helpers: photo-id parsing, URL upgrading, folder-name sanitising, category matching."""
from __future__ import annotations

import difflib
import html
import re
from typing import Iterator, Optional

# Booking hotel-photo CDN URLs look like
#   https://cf.bstatic.com/xdata/images/hotel/max1024x768/702878002.jpg?k=<hash>&o=
# The host varies (cf., q-xx., r-xx.), so match on the path only.
PHOTO_RE = re.compile(
    r"https?://[^\s\"'<>\\)]+?/images/hotel/(?P<size>[A-Za-z0-9_]+)/(?P<pid>\d+)\.(?P<ext>jpe?g|png|webp)"
    r"(?P<query>\?[^\s\"'<>\\)]*)?",
    re.I,
)

LARGE_SIZE = "max1024x768"


def clean_text_for_urls(text: str) -> str:
    """Undo the escaping that hides URLs inside JSON / HTML attribute text."""
    text = html.unescape(text)
    return text.replace("\\u0026", "&").replace("\\/", "/")


def iter_photo_urls(text: str) -> Iterator[str]:
    for m in PHOTO_RE.finditer(clean_text_for_urls(text)):
        yield m.group(0)


def photo_id(url: str) -> Optional[str]:
    m = PHOTO_RE.search(url or "")
    return m.group("pid") if m else None


def url_size(url: str) -> str:
    m = PHOTO_RE.search(url or "")
    return m.group("size") if m else ""


def size_score(url_or_size: str) -> int:
    """Rough pixel-area ranking of a size token such as max1024x768, max300, square600, 608x352."""
    s = url_size(url_or_size) or url_or_size
    m = re.search(r"(\d+)x(\d+)", s)
    if m:
        return int(m.group(1)) * int(m.group(2))
    m = re.search(r"(\d+)", s)
    if m:
        return int(m.group(1)) ** 2
    return 0


def to_large(url: str) -> str:
    """Rewrite a Booking photo URL to the 1024x768 JPEG variant (what the workbook already stores)."""
    return re.sub(
        r"(/images/hotel/)[^/]+(/\d+)\.(?:jpe?g|png|webp)",
        rf"\g<1>{LARGE_SIZE}\g<2>.jpg",
        url,
        count=1,
        flags=re.I,
    )


def url_ext(url: str) -> str:
    m = PHOTO_RE.search(url or "")
    if not m:
        return "jpg"
    ext = m.group("ext").lower()
    return "jpg" if ext == "jpeg" else ext


_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_segment(name: str, max_len: int = 120) -> str:
    """Folder-name rule observed in the existing workbook: '/' (and other Windows-illegal characters)
    become '-', everything else (umlauts, en-dashes, parentheses, m²) is kept as-is."""
    s = _ILLEGAL.sub("-", name or "")
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    return (s[:max_len].rstrip(". ")) or "_"


def norm_category(name: str) -> str:
    """Loose comparison key for room names. Deliberately KEEPS size tokens such as '(12 m²)': in this data
    two rooms can differ only by size, so stripping it would merge different rooms."""
    s = (name or "").casefold()
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"\s+", " ", s).strip(" -.,;:")
    return s


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()
