"""cloud_sync.py end-to-end: real workbook in a FAKE OneDrive, pre-seeded scrape cache, local image server.
Run:  NOVUM_XLSX=/path/book.xlsx python -m unittest tests.test_cloud_e2e -v"""
import csv
import io
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

from openpyxl import load_workbook

import cloud_sync
import login_device
import sync_booking_images as cli
from bsync import cloud as C
from bsync import workbook as W
from bsync.model import SPECIAL_BUCKETS, HotelRow, HotelScrape, ScrapedImage
from tests import mock_graph as M
from tests.test_e2e_real_workbook import Handler as ImgHandler

XLSX = os.environ.get("NOVUM_XLSX")
REMOTE_WB = "NOVUM/NOVUM_book.xlsx"


class OrderAndLoginTests(unittest.TestCase):
    def test_stalest_hotels_first(self):
        hooks = C.CloudHooks(None, "NOVUM", Path("x"), type("S", (), {"data": {"last_checked": {
            "1": "2026-11-01T00:00:00", "2": "2026-01-01T00:00:00"}}})())
        hs = [HotelRow(1, "a", "u1"), HotelRow(2, "b", "u2"), HotelRow(3, "c", "u3")]
        self.assertEqual([h.sr for h in hooks.order_hotels(hs)], [3, 2, 1])      # never-checked, oldest, newest

    def test_remote_folder_mirrors_the_workbook_hyperlinks(self):
        # Local Folder values start with the images root; on OneDrive the hotel folders live
        # under 'image/...' next to the workbook, exactly where the 'Local File' hyperlinks point.
        self.assertEqual(C.remote_folder("novum-hotels-images/Garner Hotel Berlin - Schöneberg/General Images"),
                         "image/Garner Hotel Berlin - Schöneberg/General Images")
        self.assertEqual(C.remote_folder("novum-hotels-images/Hotel X/Suite - 19 m²"),
                         "image/Hotel X/Suite - 19 m²")
        self.assertEqual(C.remote_folder("image/Hotel X/General Images"),
                         "image/Hotel X/General Images")

    def test_device_login_flow(self):
        cloud = M.FakeCloud()
        srv, url = M.start(cloud)
        os.environ["LOGIN_BASE"] = url
        try:
            out = Path(tempfile.mkdtemp()) / "t.json"
            self.assertEqual(login_device.main(["--client-id", "cid", "--tenant", "consumers", "--out", str(out)]), 0)
            self.assertEqual(json.loads(out.read_text())["refresh_token"], "RT-device")
        finally:
            srv.shutdown()
            os.environ.pop("LOGIN_BASE", None)


@unittest.skipUnless(XLSX and Path(XLSX).exists(), "set NOVUM_XLSX to the real workbook")
class CloudEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.orig = Path(XLSX).read_bytes()
        cls.cloud = M.FakeCloud()
        cls.gsrv, cls.gurl = M.start(cls.cloud)
        os.environ["GRAPH_BASE"], os.environ["LOGIN_BASE"] = cls.gurl + "/v1.0", cls.gurl
        cls.isrv = ThreadingHTTPServer(("127.0.0.1", 0), ImgHandler)
        cls.base = f"http://127.0.0.1:{cls.isrv.server_address[1]}/xdata/images/hotel"
        threading.Thread(target=cls.isrv.serve_forever, daemon=True).start()
        local = cls.tmp / "orig.xlsx"
        local.write_bytes(cls.orig)
        wb = W.open_workbook(str(local))
        hotels = W.read_hotels(wb)
        cls.index = W.read_index(wb, hotels)
        cls.hotels = {h.sr: h for h in hotels}
        cls.run_dir = cls.tmp / "run"
        cls.env = {"MS_CLIENT_ID": "cid", "MS_TENANT_ID": "t", "MS_CLIENT_SECRET": "sek",
                   "ONEDRIVE_WORKBOOK_PATH": REMOTE_WB}

    @classmethod
    def tearDownClass(cls):
        cls.gsrv.shutdown()
        cls.isrv.shutdown()
        os.environ.pop("GRAPH_BASE", None)
        os.environ.pop("LOGIN_BASE", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def seed(self, sr, new_untagged=(), new_in_room=(), new_room=None):
        h = self.hotels[sr]
        hi = self.index[h.page_url]
        imgs, order = [], 0
        for cat, g in hi.groups.items():
            for pid in g.pids:
                imgs.append(ScrapedImage(pid=pid, url=f"{self.base}/max300/{pid}.jpg",
                                         category=None if cat in SPECIAL_BUCKETS else cat, order=order)); order += 1
        for pid in new_untagged:
            imgs.append(ScrapedImage(pid=str(pid), url=f"{self.base}/max300/{pid}.jpg", category=None, order=order)); order += 1
        for cat, pid in new_in_room:
            imgs.append(ScrapedImage(pid=str(pid), url=f"{self.base}/max300/{pid}.jpg", category=cat, order=order)); order += 1
        if new_room:
            for pid in new_room[1]:
                imgs.append(ScrapedImage(pid=str(pid), url=f"{self.base}/max300/{pid}.jpg", category=new_room[0], order=order)); order += 1
        cli.save_snapshot(self.run_dir, h, HotelScrape(page_url=h.page_url, images=imgs,
                          scraped_at=datetime.now().isoformat(timespec="seconds")))

    def remote_wb(self):
        return load_workbook(io.BytesIO(self.cloud.files[REMOTE_WB]["data"]))

    def test_cloud_runs(self):
        legacy_cat = "Standard Zimmer mit Queensize-Bett (12 m²)"
        h28 = self.index[self.hotels[28].page_url]
        other = next(s for s in self.hotels if s != 28 and self.index[self.hotels[s].page_url].room_groups)
        self.seed(28, new_untagged=[9100001], new_in_room=[(legacy_cat, 9100002)])
        self.seed(other, new_room=("Suite mit Bad/Dusche (35 m²)", [9100010, 9100011]))
        only = ["--only", f"28,{other}"]
        self.cloud.put(REMOTE_WB, self.orig)

        # ------------------------------------------------ run 1: normal
        rc = cloud_sync.main(["--run-dir", str(self.run_dir)] + only, env=dict(self.env))
        self.assertEqual(rc, 0)
        files = self.cloud.files
        gen = h28.groups["General Images"]
        p_gen = f"NOVUM/{C.remote_folder(gen.folder)}/{gen.max_no + 1:02d}_9100001.jpg"
        p_leg = f"NOVUM/{C.remote_folder(h28.groups[legacy_cat].folder)}/{h28.groups[legacy_cat].max_no + 1:02d}_9100002.jpg"
        oi = self.index[self.hotels[other].page_url]
        p_new = [f"NOVUM/{C.remote_folder(oi.hotel_folder)}/Suite mit Bad-Dusche (35 m²)/{n}" for n in ("01_9100010.jpg", "02_9100011.jpg")]
        for p in [p_gen, p_leg] + p_new:
            self.assertIn(p, files, p)
            self.assertGreater(len(files[p]["data"]), 1500)
        self.assertTrue(p_leg.split("/")[-2].endswith("- 12 m²"), "legacy folder reused in the cloud too")
        wb = self.remote_wb()
        ws = wb[W.IMAGES_SHEET]
        new_rows = [r for r in ws.iter_rows(min_row=13853, values_only=True) if r[1]]
        self.assertEqual(sorted(r[8] for r in new_rows),
                         sorted([p_gen.split("/")[-1], p_leg.split("/")[-1], "01_9100010.jpg", "02_9100011.jpg"]))
        self.assertEqual(len(list(wb[W.LOG_SHEET].iter_rows(min_row=2))), 2)
        # reports + state landed in OneDrive
        reports = [p for p in files if p.startswith("NOVUM/sync-reports/")]
        self.assertTrue(any(p.endswith("/new_images.csv") for p in reports))
        self.assertTrue(any(p.endswith("/sync.log") for p in reports))
        state = json.loads(files["NOVUM/sync-state/state.json"]["data"])
        self.assertEqual(set(state["last_checked"]), {"28", str(other)})
        # no leftovers in staging
        self.assertFalse([p for p in (self.run_dir / "staging").rglob("*.jpg")])

        # ------------------------------------------------ run 2: leftovers from a failed run + concurrent edit
        # an earlier run uploaded 9100001 as number 05 but never managed to write the workbook:
        self.cloud.put(REMOTE_WB, self.orig)
        for p in [p_gen] + [p_leg] + p_new:
            files.pop(p, None)
        left = f"NOVUM/{C.remote_folder(gen.folder)}/05_9100001.jpg"
        self.cloud.put(left, b"x" * 3000)
        before = {p for p in files if p.startswith(f"NOVUM/{C.remote_folder(gen.folder)}/")}

        orig_after = C.CloudHooks.after_download
        cloud = self.cloud

        def edit_during_run(self_, items):           # somebody saves the workbook while we are busy
            out = orig_after(self_, items)
            cloud.bump_etag(REMOTE_WB)
            return out

        C.CloudHooks.after_download = edit_during_run
        try:
            rc = cloud_sync.main(["--run-dir", str(self.run_dir)] + only, env=dict(self.env))
        finally:
            C.CloudHooks.after_download = orig_after
        self.assertEqual(rc, 3, "concurrent edit must not be overwritten")
        self.assertEqual(files[REMOTE_WB]["data"], self.orig, "OneDrive workbook left exactly as the other editor saved it")
        alt = [p for p in files if p.endswith("_updated.xlsx") and "/sync-reports/" in p]
        self.assertEqual(len(alt), 1, alt)
        ws2 = load_workbook(io.BytesIO(files[alt[0]]["data"]))[W.IMAGES_SHEET]
        rows2 = {r[8]: r for r in ws2.iter_rows(min_row=13853, values_only=True) if r[1]}
        self.assertIn("05_9100001.jpg", rows2, "leftover file adopted with its existing number")
        self.assertEqual(rows2["05_9100001.jpg"][4], 5)
        after = {p for p in files if p.startswith(f"NOVUM/{C.remote_folder(gen.folder)}/")}
        self.assertEqual(after, before, "no duplicate copy of the adopted image was uploaded")

    def test_bad_config_and_missing_workbook(self):
        self.assertEqual(cloud_sync.main(["--run-dir", str(self.tmp / "r2")], env={}), 2)
        env = dict(self.env, ONEDRIVE_WORKBOOK_PATH="Nope/missing.xlsx")
        self.assertEqual(cloud_sync.main(["--run-dir", str(self.tmp / "r3")], env=env), 2)
        env = dict(self.env, MS_CLIENT_SECRET="wrong")
        self.assertEqual(cloud_sync.main(["--run-dir", str(self.tmp / "r4")], env=env), 2)


if __name__ == "__main__":
    unittest.main()
