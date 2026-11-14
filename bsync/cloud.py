"""Glue between the sync pipeline and OneDrive: hooks, state file, report upload."""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .graph import GraphError, OneDrive
from .hooks import Hooks

log = logging.getLogger("sync")
_NAME_RE = re.compile(r"^(\d+)_(\d+)\.\w+$")


class StateStore:
    """Tiny JSON file in OneDrive: when was each hotel last checked (so every run starts with the stalest)."""

    def __init__(self, od: OneDrive, path: str):
        self.od, self.path = od, path
        self.data: dict = {"last_checked": {}}

    def load(self) -> dict:
        raw = self.od.read_bytes(self.path)
        if raw:
            try:
                self.data = json.loads(raw.decode("utf-8"))
                self.data.setdefault("last_checked", {})
            except ValueError:
                log.info("state file unreadable - starting with an empty state")
        return self.data

    def save(self) -> None:
        self.od.upload_bytes(self.path, json.dumps(self.data, indent=1).encode("utf-8"), replace=True)


class CloudHooks(Hooks):
    def __init__(self, od: OneDrive, images_folder: str, staging: Path, state: StateStore):
        self.od, self.images_folder, self.staging, self.state = od, images_folder.strip("/"), staging, state
        self.ctx: dict = {}
        self.adopted = self.uploaded = 0

    def remote(self, rel: str) -> str:
        return f"{self.images_folder}/{rel}" if self.images_folder else rel

    # stalest first: hotels never checked, then oldest check
    def order_hotels(self, hotels: list) -> list:
        lc = self.state.data["last_checked"]
        return sorted(hotels, key=lambda h: lc.get(str(h.sr), ""))

    def after_scrape(self, hotel, scrape) -> None:
        when = scrape.scraped_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.state.data["last_checked"][str(hotel.sr)] = when

    def after_download(self, items: list) -> list:
        by_folder: dict = defaultdict(list)
        for it in items:
            by_folder[it.folder].append(it)
        kept: list = []
        for folder, its in by_folder.items():
            try:
                names = self.od.list_names(self.remote(folder))
            except GraphError:
                names = []
            existing = {}
            for n in names:
                m = _NAME_RE.match(n)
                if m:
                    existing[m.group(2)] = (n, int(m.group(1)))
            for it in its:
                local = self.staging / Path(*folder.split("/")) / it.file_name
                if it.pid in existing:      # left behind by an earlier run whose workbook upload failed
                    it.file_name, it.no = existing[it.pid][0], existing[it.pid][1]
                    self.adopted += 1
                    kept.append(it)
                    local.unlink(missing_ok=True)
                    continue
                try:
                    self.od.upload_bytes(self.remote(f"{folder}/{it.file_name}"), local.read_bytes())
                except (GraphError, OSError) as e:
                    log.info(f"  upload FAILED {it.hotel_name} / {it.category} / {it.file_name}: {e}")
                    continue
                self.uploaded += 1
                kept.append(it)
                local.unlink(missing_ok=True)
        log.info(f"OneDrive: {self.uploaded} image(s) uploaded, {self.adopted} already there")
        return kept

    def finish(self, ctx: dict) -> None:
        self.ctx = ctx


def upload_reports(od: OneDrive, reports_folder: str, stamp: str, run_dir: Path) -> list:
    done = []
    for name in ("hotels_report.csv", "new_images.csv", "failed_downloads.csv", "sync.log"):
        f = run_dir / name
        if f.exists():
            try:
                od.upload_bytes(f"{reports_folder}/{stamp}/{name}", f.read_bytes(), replace=True)
                done.append(name)
            except (GraphError, OSError) as e:
                log.info(f"could not upload report {name}: {e}")
    return done
