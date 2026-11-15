"""Download new images into <images_root>/image/<hotel>/<room>/<NN>_<photoId>.<ext> - the
'Local Folder' value still names the legacy 'novum-hotels-images/' root, so it is translated
with workbook.disk_folder first (the workbook's hyperlinks point at 'image/...')."""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from .util import url_ext
from .workbook import disk_folder

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Referer": "https://www.booking.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}
MIN_BYTES = 1500


def fs_path(p: Path) -> str:
    """Windows: use the \\\\?\\ prefix so folders longer than 260 characters still work."""
    s = str(p.resolve())
    if sys.platform.startswith("win") and not s.startswith("\\\\?\\"):
        s = "\\\\?\\" + s
    return s


def _fetch(session: requests.Session, url: str, retries: int = 3, timeout: int = 30):
    last = ""
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=timeout, headers=HEADERS)
            ctype = r.headers.get("Content-Type", "")
            if r.status_code == 200 and ctype.startswith("image") and len(r.content) >= MIN_BYTES:
                return r.content, ""
            last = f"HTTP {r.status_code} {ctype} {len(r.content)}B"
            if r.status_code in (403, 404, 410):     # will not get better by retrying
                break
        except requests.RequestException as e:
            last = type(e).__name__
        time.sleep(1.5 * (attempt + 1))
    return None, last


def download_one(item, root: Path, session: requests.Session, force: bool = False) -> tuple[bool, str]:
    """Fill item.file_name / item.url_used. Tries the 1024px JPEG first, then the URL as scraped."""
    candidates = []
    for u in (item.url_large, item.url):
        if u not in candidates:
            candidates.append(u)
    folder = root / Path(*disk_folder(item.folder).split("/"))
    errors = []
    for u in candidates:
        name = item.planned_file_name(url_ext(u))
        target = folder / name
        if target.exists() and not force:
            item.file_name, item.url_used = name, u
            return True, "already on disk"
        data, err = _fetch(session, u)
        if data is None:
            errors.append(f"{u[:90]}... {err}")
            continue
        try:
            os.makedirs(fs_path(folder), exist_ok=True)
            tmp = fs_path(target) + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, fs_path(target))
        except OSError as e:
            return False, f"cannot write {target}: {e}"
        item.file_name, item.url_used = name, u
        return True, "downloaded"
    return False, " | ".join(errors)


def download_all(items: list, root: Path, workers: int = 4, force: bool = False, log=print) -> dict:
    """Returns {id(item): (ok, message)}."""
    results: dict = {}
    if not items:
        return results
    session = requests.Session()
    done = 0

    def work(it):
        ok, msg = download_one(it, root, session, force)
        time.sleep(0.15)
        return it, ok, msg

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for it, ok, msg in ex.map(work, items):
            results[id(it)] = (ok, msg)
            done += 1
            if not ok:
                log(f"  download FAILED {it.hotel_name} / {it.category} / {it.pid}: {msg}")
            if done % 25 == 0 or done == len(items):
                log(f"  downloaded {done}/{len(items)}")
    return results
