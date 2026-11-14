#!/usr/bin/env python3
"""Scheduled, unattended run: OneDrive workbook in -> Booking.com check -> new images + updated workbook back to OneDrive.

    python cloud_sync.py [--dry-run] [--max-minutes 300] [--run-dir sync_run] [--env-file cloud.env] \
                         [-- options understood by sync_booking_images.py, e.g. --channel chrome --only 28]

Configuration comes from environment variables (see README_CLOUD.md / cloud.env.example).
Exit codes: 0 ok | 1 workbook verification problem | 2 configuration/auth error
            3 workbook was edited meanwhile -> updated copy saved to the reports folder instead
            4 every processed hotel failed (blocked by Booking.com / errors)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import sync_booking_images as cli
from bsync import graph as G
from bsync.cloud import CloudHooks, StateStore, upload_reports

log = logging.getLogger("sync")


def load_env_file(path: str, env=os.environ) -> None:
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main(argv=None, env=os.environ) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", default="sync_run")
    ap.add_argument("--dry-run", action="store_true", help="scrape + compare + upload reports only")
    ap.add_argument("--max-minutes", type=float, default=None, help="time budget for scraping (default: none)")
    ap.add_argument("--env-file", help="KEY=VALUE file with the settings (alternative to real env vars)")
    ap.add_argument("--lock-retries", type=int, default=3, help="retries when the workbook is locked/open elsewhere")
    ap.add_argument("--lock-wait", type=float, default=120, help="seconds between those retries")
    a, rest = ap.parse_known_args(argv)
    rest = [r for r in rest if r != "--"]
    if a.env_file:
        load_env_file(a.env_file, env)

    run_dir = Path(a.run_dir).resolve()
    cli.setup_logging(run_dir)
    try:
        wb_remote = env["ONEDRIVE_WORKBOOK_PATH"].strip().strip("/")
    except KeyError:
        log.info("ONEDRIVE_WORKBOOK_PATH is not set (e.g. NOVUM/NOVUM_Hospitality_Hotels_with_Booking_Images.xlsx)")
        return 2
    images_folder = env.get("ONEDRIVE_IMAGES_FOLDER", wb_remote.rsplit("/", 1)[0] if "/" in wb_remote else "").strip().strip("/")
    reports_folder = env.get("ONEDRIVE_REPORTS_FOLDER", (images_folder + "/" if images_folder else "") + "sync-reports").strip("/")
    state_path = env.get("ONEDRIVE_STATE_PATH", (images_folder + "/" if images_folder else "") + "sync-state/state.json").strip("/")
    env.setdefault("MS_TOKEN_FILE", str(run_dir / "token.json"))

    try:
        od = G.OneDrive(G.auth_from_env(env), G.drive_spec_from_env(env))
        item = od.get_item(wb_remote)
    except (G.AuthError, G.GraphError) as e:
        log.info(f"cannot reach OneDrive: {e}")
        return 2
    if not item:
        log.info(f"workbook not found in OneDrive at '{wb_remote}'")
        return 2
    etag = item["eTag"]
    name = wb_remote.rsplit("/", 1)[-1]
    work = run_dir / "work"
    local_wb = work / name
    out_wb = work / (Path(name).stem + "_updated.xlsx")
    for f in (local_wb, out_wb):
        f.unlink(missing_ok=True)
    log.info(f"downloading workbook '{wb_remote}' ({item.get('size', 0) / 1e6:.1f} MB)")
    od.download(wb_remote, local_wb)

    state = StateStore(od, state_path)
    state.load()
    hooks = CloudHooks(od, images_folder, run_dir / "staging", state)

    args = ["--workbook", str(local_wb), "--out", str(out_wb), "--run-dir", str(run_dir),
            "--images-root", str(run_dir / "staging")] + rest
    if a.dry_run:
        args.append("--dry-run")
    if a.max_minutes:
        args += ["--max-minutes", str(a.max_minutes)]
    rc = cli.main(args, hooks)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    if rc != 0:
        upload_reports(od, reports_folder, stamp, run_dir)
        return rc

    ctx = hooks.ctx
    rc = 0
    if not a.dry_run:
        for attempt in range(a.lock_retries + 1):
            try:
                od.replace_file(wb_remote, out_wb, if_match=etag)
                log.info(f"workbook updated in OneDrive ({ctx.get('written', 0)} new rows)")
                state.save()
                break
            except G.PreconditionFailed:
                alt = f"{reports_folder}/{stamp}/{Path(name).stem}_updated.xlsx"
                od.replace_file(alt, out_wb)
                log.info("the workbook was changed in OneDrive while this run was working. It was NOT overwritten; "
                         f"the updated copy is at '{alt}'. Images are already uploaded; the next run reuses them.")
                rc = 3
                break
            except G.Locked as e:
                if attempt == a.lock_retries:
                    alt = f"{reports_folder}/{stamp}/{Path(name).stem}_updated.xlsx"
                    od.replace_file(alt, out_wb)
                    log.info(f"workbook stayed locked ({e}); updated copy saved at '{alt}'")
                    rc = 3
                    break
                log.info(f"workbook is locked (open in Excel?) - retrying in {a.lock_wait:.0f}s")
                time.sleep(a.lock_wait)

    counts = ctx.get("status_counts", {})
    processed = sum(counts.values())
    if rc == 0 and processed and not counts.get("OK"):
        log.info("no hotel could be read successfully (blocked / errors) - failing the run so you get notified")
        rc = 4
    upload_reports(od, reports_folder, stamp, run_dir)
    return rc


if __name__ == "__main__":
    sys.exit(main())
