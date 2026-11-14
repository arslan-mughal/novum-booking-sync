#!/usr/bin/env python3
"""Check every Booking.com hotel page in the workbook for new photos and add them to 'Images - Booking.com'.

    python sync_booking_images.py --workbook NOVUM_Hospitality_Hotels_with_Booking_Images.xlsx --only 28 --dry-run

See README.md for the recommended step-by-step workflow.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from bsync import __version__
from bsync import workbook as W
from bsync.diff import DiffConfig, diff_hotel
from bsync.downloader import download_all
from bsync.hooks import Hooks
from bsync.model import GENERAL, HotelDiff, HotelScrape
from bsync.util import sanitize_segment

log = logging.getLogger("sync")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workbook", required=True, help="the .xlsx with tabs 'NOVUM-Hotels' and 'Images - Booking.com'")
    p.add_argument("--images-root", help="folder that CONTAINS 'novum-hotels-images' (default: the workbook's folder)")
    p.add_argument("--out", help="output workbook (default: <name>_updated.xlsx next to the input; never overwrites the input)")
    p.add_argument("--run-dir", default="sync_run", help="scrape cache, reports, log, browser profile (default ./sync_run)")
    p.add_argument("--only", action="append", default=[],
                   help="limit to hotels by Sr. number or part of the name; repeatable or comma separated")
    p.add_argument("--limit", type=int, help="process at most N hotels (after --only)")
    p.add_argument("--dry-run", action="store_true", help="scrape + compare + write reports; change nothing else")
    p.add_argument("--refresh", action="store_true", help="ignore cached scrapes in the run dir")
    p.add_argument("--max-age-hours", type=float, default=72, help="reuse cached scrapes younger than this (default 72)")
    # browser
    p.add_argument("--headless", action="store_true", help="hide the browser (Booking's bot check usually needs it visible)")
    p.add_argument("--channel", default="chrome", help="chrome | msedge | chromium (default chrome = your installed Chrome)")
    p.add_argument("--no-dates", action="store_true", help="do not add check-in/out dates to the page URL")
    p.add_argument("--checkin", help="YYYY-MM-DD (default: 45 days ahead)")
    p.add_argument("--checkout", help="YYYY-MM-DD (required together with --checkin)")
    p.add_argument("--delay", type=float, nargs=2, default=(4.0, 9.0), metavar=("MIN", "MAX"),
                   help="seconds to wait between hotels (default 4 9)")
    p.add_argument("--challenge-wait", type=int, default=300, help="seconds to wait for you to solve a bot check")
    p.add_argument("--debug", action="store_true", help="save screenshots + page HTML per hotel to <run-dir>/debug")
    # comparison
    p.add_argument("--untagged-category", default=GENERAL,
                   help=f"category for new gallery photos that belong to no room (default '{GENERAL}')")
    p.add_argument("--min-coverage", type=float, default=0.5)
    p.add_argument("--force-low-coverage", action="store_true")
    p.add_argument("--allow-missing-rooms", action="store_true")
    # output
    p.add_argument("--no-download", action="store_true", help="add workbook rows without downloading the files")
    p.add_argument("--no-highlight", action="store_true", help="do not colour new rows yellow")
    p.add_argument("--no-verify", action="store_true", help="skip the post-save integrity check")
    p.add_argument("--max-minutes", type=float, help="stop scraping new hotels after this many minutes "
                   "(remaining hotels are simply left for the next run)")
    p.add_argument("--audit-local", action="store_true", help="also report existing rows whose file is missing on disk")
    return p.parse_args(argv)


def setup_logging(run_dir: Path):
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
    root = logging.getLogger("sync")
    root.setLevel(logging.INFO)
    for old in list(root.handlers):
        old.close()
        root.removeHandler(old)
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(run_dir / "sync.log", encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)


def select_hotels(hotels: list, only: list, limit):
    tokens = [t.strip() for chunk in only for t in chunk.split(",") if t.strip()]
    if tokens:
        def match(h):
            for t in tokens:
                if t.isdigit() and int(t) == h.sr:
                    return True
                if not t.isdigit() and t.casefold() in h.name.casefold():
                    return True
            return False
        hotels = [h for h in hotels if match(h)]
    return hotels[:limit] if limit else hotels


def snapshot_path(run_dir: Path, sr: int) -> Path:
    return run_dir / "snapshots" / f"{sr:03d}.json"


def load_snapshot(run_dir: Path, h, max_age_h: float):
    p = snapshot_path(run_dir, h.sr)
    if not p.exists():
        return None
    try:
        s = HotelScrape.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return None
    if s.page_url != h.page_url or s.status not in ("OK", "NO_PHOTOS", "NOT_FOUND"):
        return None
    try:
        age = (datetime.now() - datetime.fromisoformat(s.scraped_at)).total_seconds() / 3600
    except ValueError:
        return None
    return s if age <= max_age_h else None


def save_snapshot(run_dir: Path, h, s: HotelScrape):
    p = snapshot_path(run_dir, h.sr)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")


def write_csv(path: Path, header: list, rows: list):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def main(argv=None, hooks: Hooks = None) -> int:
    hooks = hooks or Hooks()
    a = parse_args(argv)
    wb_path = Path(a.workbook).resolve()
    run_dir = Path(a.run_dir).resolve()
    setup_logging(run_dir)
    log.info(f"novum-booking-sync {__version__}")
    if not wb_path.exists():
        log.info(f"workbook not found: {wb_path}")
        return 2
    root = Path(a.images_root).resolve() if a.images_root else wb_path.parent
    out_path = Path(a.out).resolve() if a.out else wb_path.with_name(wb_path.stem + "_updated.xlsx")
    if out_path == wb_path:
        log.info("--out must differ from the input workbook (the original is never overwritten)")
        return 2

    run_started = time.time()
    log.info(f"loading workbook (takes ~30 s): {wb_path.name}")
    wb = W.open_workbook(str(wb_path))
    hotels_all = W.read_hotels(wb)
    index = W.read_index(wb, hotels_all)
    owner = W.owner_map(index)
    images_root_name = W.detect_root(index)
    n_known = sum(len(h.all_pids()) for h in index.values())
    hotels = hooks.order_hotels(select_hotels(hotels_all, a.only, None))
    if a.limit:
        hotels = hotels[:a.limit]
    log.info(f"{len(hotels_all)} hotels with a Booking.com link, {n_known} known photos; "
             f"processing {len(hotels)} hotel(s)")
    if not hotels:
        log.info("nothing selected - check --only")
        return 2

    # ---------------------------------------------------------------- 1. scrape (or reuse cached scrapes)
    scrapes: dict = {}
    todo = []
    for h in hotels:
        s = None if a.refresh else load_snapshot(run_dir, h, a.max_age_hours)
        if s:
            scrapes[h.page_url] = s
            hooks.after_scrape(h, s)
        else:
            todo.append(h)
    log.info(f"cached scrapes reused: {len(scrapes)}; to scrape now: {len(todo)}")
    if todo:
        from bsync.scraper import BookingBrowser, default_dates
        dates = (None, None) if a.no_dates else ((a.checkin, a.checkout) if a.checkin else default_dates())
        blocked_streak = 0
        with BookingBrowser(run_dir / "browser-profile", run_dir / "debug" if a.debug else None,
                            headed=not a.headless, channel=a.channel, challenge_wait=a.challenge_wait,
                            log=log.info) as br:
            for n, h in enumerate(todo, 1):
                if a.max_minutes and (time.time() - run_started) / 60 > a.max_minutes:
                    log.info(f"time budget of {a.max_minutes:.0f} min reached - {len(todo) - n + 1} hotel(s) "
                             f"deferred to the next run")
                    break
                t0 = time.time()
                s = br.scrape(h.sr, h.page_url, index[h.page_url].name or h.name, dates)
                scrapes[h.page_url] = s
                if s.status in ("OK", "NO_PHOTOS", "NOT_FOUND"):
                    save_snapshot(run_dir, h, s)
                    hooks.after_scrape(h, s)
                d = s.diagnostics
                log.info(f"[{n}/{len(todo)}] Sr.{h.sr} {h.name}: {s.status}, {len(s.images)} photo rows "
                         f"(gallery {d.get('gallery_photos', '-')}, room popups {d.get('room_popups_opened', '-')}"
                         f"/{d.get('room_links', '-')}) in {time.time() - t0:.0f}s "
                         + ("; " + "; ".join(s.notes) if s.notes else ""))
                blocked_streak = blocked_streak + 1 if s.status == "BLOCKED" else 0
                if blocked_streak >= 3:
                    log.info("3 hotels in a row were blocked - stopping the scrape so the IP is not hammered. "
                             "Wait a while / solve the check in the visible browser and re-run (finished hotels are cached).")
                    break
                time.sleep(random.uniform(*a.delay))

    # ---------------------------------------------------------------- 2. compare
    cfg = DiffConfig(untagged_category=a.untagged_category, min_coverage=a.min_coverage,
                     force_low_coverage=a.force_low_coverage, allow_missing_rooms=a.allow_missing_rooms)
    diffs: list = []
    claimed: dict = {}
    deferred = 0
    for h in hotels:
        s = scrapes.get(h.page_url)
        if s is None:                      # not reached (time budget / blocked streak): leave for next run
            deferred += 1
            continue
        diffs.append(diff_hotel(index[h.page_url], s, cfg, owner, claimed, images_root_name))

    if deferred:
        log.info(f"{deferred} hotel(s) were not reached in this run")
    all_new = [n for d in diffs for n in d.new_images]
    write_csv(run_dir / "hotels_report.csv",
              ["Sr.", "Hotel", "Status", "Existing", "On page", "Re-found", "Coverage", "Not on page",
               "New", "New in rooms", "New untagged", "New categories", "Notes"],
              [[d.sr, d.name, d.status, d.existing_n, d.scraped_n, d.refound_n,
                f"{d.coverage:.1%}" if d.scraped_n else "", d.missing_n if d.scraped_n else "",
                len(d.new_images), d.new_in_rooms, d.new_untagged, "; ".join(d.new_categories),
                " | ".join(d.notes)] for d in diffs])
    write_csv(run_dir / "new_images.csv",
              ["Sr.", "Hotel", "Category", "New category?", "Kind", "Image No.", "Photo id", "Also filed elsewhere",
               "Local folder", "URL"],
              [[n.sr, n.hotel_name, n.category, n.new_category, n.kind, n.no, n.pid, n.also_elsewhere, n.folder,
                n.url_large] for n in all_new])
    by_status: dict = {}
    for d in diffs:
        by_status[d.status] = by_status.get(d.status, 0) + 1
    log.info("status per hotel: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    log.info(f"NEW photos found: {len(all_new)} "
             f"(in rooms {sum(d.new_in_rooms for d in diffs)}, untagged {sum(d.new_untagged for d in diffs)}, "
             f"new room categories {sum(len(d.new_categories) for d in diffs)})")
    log.info(f"reports: {run_dir / 'hotels_report.csv'}  and  {run_dir / 'new_images.csv'}")

    if a.audit_local:
        miss = []
        ws = wb[W.IMAGES_SHEET]
        h = W._header_map(ws, W.IMG_COLS, W.IMAGES_SHEET)
        for row in ws.iter_rows(min_row=2, values_only=True):
            f, fo = row[h["Local File"] - 1], row[h["Local Folder"] - 1]
            if f and fo and not (root / Path(*str(fo).split("/")) / str(f)).exists():
                miss.append([row[h["Sr."] - 1], row[h["Hotel Name"] - 1], fo, f])
        write_csv(run_dir / "missing_local_files.csv", ["Sr.", "Hotel", "Local Folder", "Local File"], miss)
        log.info(f"audit: {len(miss)} existing rows have no file under {root} -> missing_local_files.csv")

    if a.dry_run:
        log.info("dry run - workbook and image folders untouched.")
        hooks.finish({"diffs": diffs, "new": [], "written": 0, "out_path": None, "run_dir": run_dir,
                      "status_counts": by_status, "dry_run": True})
        return 0
    if not all_new:
        log.info("no new photos; still writing the log tab so this run is recorded.")

    # ---------------------------------------------------------------- 3. download
    applied: dict = {}
    failed: dict = {}
    to_write = all_new
    if all_new and not a.no_download:
        log.info(f"downloading {len(all_new)} images into {root / images_root_name}")
        results = download_all(all_new, root, log=log.info)
        to_write = [n for n in all_new if results[id(n)][0]]
        for n in all_new:
            if not results[id(n)][0]:
                failed[n.page_url] = failed.get(n.page_url, 0) + 1
        if failed:
            write_csv(run_dir / "failed_downloads.csv", ["Sr.", "Hotel", "Category", "Photo id", "URL", "Reason"],
                      [[n.sr, n.hotel_name, n.category, n.pid, n.url_large, results[id(n)][1]]
                       for n in all_new if not results[id(n)][0]])
            log.info(f"{sum(failed.values())} downloads failed - not added to the workbook, they will be "
                     f"picked up again next run (failed_downloads.csv)")
    else:
        for n in to_write:
            n.file_name, n.url_used = n.planned_file_name(), n.url_large
    if to_write:
        kept = hooks.after_download(to_write)
        kept_ids = {id(k) for k in kept}
        for n in to_write:
            if id(n) not in kept_ids:
                failed[n.page_url] = failed.get(n.page_url, 0) + 1
        to_write = kept

    # ---------------------------------------------------------------- 4. update workbook
    for n in to_write:
        applied[n.page_url] = applied.get(n.page_url, 0) + 1
    written = W.append_new_rows(wb, to_write, highlight=not a.no_highlight)
    W.append_log(wb, datetime.now().strftime("%Y-%m-%d %H:%M"), diffs, applied, failed)
    wb.save(str(out_path))
    log.info(f"saved {out_path}  (+{written} rows)")

    if not a.no_verify:
        log.info("verifying that no existing cell changed ...")
        problems = W.verify(str(wb_path), str(out_path), written)
        if problems:
            log.info("VERIFICATION PROBLEMS:\n  " + "\n  ".join(problems))
            return 1
        log.info("verification OK: all existing rows, links and comments unchanged.")
    hooks.finish({"diffs": diffs, "new": to_write, "written": written, "out_path": out_path,
                  "run_dir": run_dir, "status_counts": by_status, "dry_run": False})
    return 0


if __name__ == "__main__":
    sys.exit(main())
