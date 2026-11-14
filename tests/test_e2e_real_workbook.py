"""End-to-end test of the real CLI against the real workbook (skipped unless NOVUM_XLSX is set).

The browser is replaced by a pre-seeded scrape cache; "Booking" images are served by a local HTTP server.
Run:  NOVUM_XLSX=/path/to/workbook.xlsx python -m unittest tests.test_e2e_real_workbook -v
"""
import csv
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from openpyxl import load_workbook

import sync_booking_images as cli
from bsync import workbook as W
from bsync.model import SPECIAL_BUCKETS, HotelScrape, ScrapedImage

XLSX = os.environ.get("NOVUM_XLSX")


def jpeg_bytes(seed: int) -> bytes:
    from PIL import Image
    import random
    rnd = random.Random(seed)
    im = Image.new("RGB", (48, 48))
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(48 * 48)])
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    only_small = set()     # photo ids that exist ONLY at /max300/ (forces the fallback path)
    missing = set()        # photo ids that 404 everywhere (forces a failed download)

    def log_message(self, *a):
        pass

    def do_GET(self):
        parts = self.path.split("?")[0].split("/")
        size, name = parts[-2], parts[-1]
        pid = name.split(".")[0]
        if pid in self.missing or (pid in self.only_small and size != "max300"):
            self.send_response(404); self.end_headers(); return
        body = jpeg_bytes(int(pid))
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(XLSX and Path(XLSX).exists(), "set NOVUM_XLSX to the real workbook")
class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.wb_path = cls.tmp / "book.xlsx"
        shutil.copy(XLSX, cls.wb_path)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}/xdata/images/hotel"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

        wb = W.open_workbook(str(cls.wb_path))
        hotels = W.read_hotels(wb)
        cls.index = W.read_index(wb, hotels)
        cls.hotels = {h.sr: h for h in hotels}
        cls.run_dir = cls.tmp / "run"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def seed(self, sr, new_untagged=(), new_in_room=(), new_room=None, partial=False, status="OK"):
        h = self.hotels[sr]
        hi = self.index[h.page_url]
        imgs, order = [], 0
        pids = sorted(hi.all_pids(), key=int)
        if partial:
            pids = pids[: max(1, len(pids) // 10)]
        keep = set(pids)
        for cat, g in hi.groups.items():
            for pid in g.pids & keep:
                imgs.append(ScrapedImage(pid=pid, url=f"{self.base}/max300/{pid}.jpg",
                                         category=None if cat in SPECIAL_BUCKETS else cat, order=order))
                order += 1
        for pid in new_untagged:
            imgs.append(ScrapedImage(pid=str(pid), url=f"{self.base}/max300/{pid}.jpg", category=None, order=order)); order += 1
        for cat, pid in new_in_room:
            imgs.append(ScrapedImage(pid=str(pid), url=f"{self.base}/max300/{pid}.jpg", category=cat, order=order)); order += 1
        if new_room:
            for pid in new_room[1]:
                imgs.append(ScrapedImage(pid=str(pid), url=f"{self.base}/max300/{pid}.jpg", category=new_room[0], order=order)); order += 1
        s = HotelScrape(page_url=h.page_url, status=status, images=imgs,
                        scraped_at=datetime.now().isoformat(timespec="seconds"))
        cli.save_snapshot(self.run_dir, h, s)

    def test_full_run(self):
        # --- choose 5 hotels: legacy-folder hotel (28), a normal one, one with '/' new room, blocked, partial
        srs = list(self.hotels)
        normal = next(s for s in srs if s not in (28,) and len(self.index[self.hotels[s].page_url].room_groups) > 2)
        slash = next(s for s in srs if s not in (28, normal) and self.index[self.hotels[s].page_url].room_groups)
        blocked = next(s for s in srs if s not in (28, normal, slash))
        partial = next(s for s in srs if s not in (28, normal, slash, blocked))

        legacy_cat = "Standard Zimmer mit Queensize-Bett (12 m²)"
        self.assertIn(legacy_cat, self.index[self.hotels[28].page_url].groups)
        nrm_room = next(iter(self.index[self.hotels[normal].page_url].room_groups))

        Handler.only_small = {"9000003"}          # needs the max300 fallback
        Handler.missing = {"9000099"}             # must fail and stay out of the workbook
        self.seed(28, new_untagged=[9000001, 9000002], new_in_room=[(legacy_cat, 9000003)])
        self.seed(normal, new_in_room=[(nrm_room, 9000010), (nrm_room, 9000011)], new_untagged=[9000099])
        self.seed(slash, new_room=("Suite mit Bad/Dusche (35 m²)", [9000020, 9000021]))
        self.seed(blocked, status="NOT_FOUND")     # a hotel whose page is gone
        self.seed(partial, new_untagged=[9000030], partial=True)

        common = ["--workbook", str(self.wb_path), "--run-dir", str(self.run_dir),
                  "--only", ",".join(str(s) for s in (28, normal, slash, blocked, partial))]
        out_path = self.wb_path.with_name("book_updated.xlsx")

        # ---------------- dry run: reports only
        rc = cli.main(common + ["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertFalse(out_path.exists(), "dry run must not write a workbook")
        rep = list(csv.DictReader(open(self.run_dir / "new_images.csv", encoding="utf-8-sig")))
        self.assertEqual(len(rep), 2 + 1 + 2 + 1 + 2)       # 28:3, normal:3 (incl. one that will 404), slash:2 -> 8
        hrep = {r["Status"] for r in csv.DictReader(open(self.run_dir / "hotels_report.csv", encoding="utf-8-sig"))}
        self.assertEqual(hrep, {"OK", "NOT_FOUND", "LOW_COVERAGE"})

        # ---------------- real run
        rc = cli.main(common)
        self.assertEqual(rc, 0, "workbook verification must pass (return code 0)")
        self.assertTrue(out_path.exists())

        root = self.wb_path.parent
        h28 = self.index[self.hotels[28].page_url]
        legacy_folder = root / Path(*h28.groups[legacy_cat].folder.split("/"))
        self.assertTrue((legacy_folder / f"{h28.groups[legacy_cat].max_no + 1:02d}_9000003.jpg").exists(),
                        "fallback to max300 + legacy folder reuse")
        self.assertTrue(legacy_folder.name.endswith("- 12 m²"))
        gen = h28.groups["General Images"]
        for k, pid in enumerate((9000001, 9000002), start=1):
            self.assertTrue((root / Path(*gen.folder.split("/")) / f"{gen.max_no + k:02d}_{pid}.jpg").exists())
        # new room category with slash -> sanitised folder
        sl = self.index[self.hotels[slash].page_url]
        new_folder = root / Path(*sl.hotel_folder.split("/")) / "Suite mit Bad-Dusche (35 m²)"
        self.assertEqual(sorted(p.name for p in new_folder.iterdir()), ["01_9000020.jpg", "02_9000021.jpg"])
        # failed download stays out
        self.assertFalse(list(root.rglob("*9000099*")))
        # partial-load + blocked hotels: nothing written for them
        self.assertFalse(list(root.rglob("*9000030*")))

        # ---------------- workbook content
        wb = load_workbook(out_path)
        ws = wb[W.IMAGES_SHEET]
        old_last = W._last_data_row(load_workbook(self.wb_path)[W.IMAGES_SHEET])
        new_rows = [[c.value for c in r] for r in ws.iter_rows(min_row=old_last + 1, max_row=ws.max_row)]
        self.assertEqual(len(new_rows), 7)                       # 8 found - 1 failed download
        pids = sorted(r[8].split("_")[1].split(".")[0] for r in new_rows)
        self.assertEqual(pids, ["9000001", "9000002", "9000003", "9000010", "9000011", "9000020", "9000021"])
        r3 = next(r for r in new_rows if "9000003" in r[8])
        self.assertEqual((r3[3], r3[4], r3[0]), (legacy_cat, h28.groups[legacy_cat].max_no + 1, 28))
        self.assertTrue(r3[6].endswith("/max300/9000003.jpg") or "max300" in r3[6], "URL actually used is stored")
        r1 = next(r for r in new_rows if "9000001" in r[8])
        self.assertIn("max1024x768/9000001.jpg", r1[6])
        self.assertEqual(ws.auto_filter.ref, f"A1:I{ws.max_row}")
        row_no = next(r for r in range(old_last + 1, ws.max_row + 1) if "9000001" in str(ws.cell(r, 9).value))
        self.assertEqual(ws.cell(row_no, 3).hyperlink.target, h28.page_url)
        self.assertIn("9000001", ws.cell(row_no, 7).hyperlink.target)
        self.assertEqual(ws.cell(row_no, 1).fill.fgColor.rgb[-6:], "FFF2CC")
        # style copied from the last existing row (column I is blue in the original)
        self.assertEqual(ws.cell(row_no, 9).font.color.rgb, ws.cell(old_last, 9).font.color.rgb)
        self.assertEqual(ws.cell(row_no, 9).font.name, "Calibri")
        # log tab
        log = wb[W.LOG_SHEET]
        rows = list(log.iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(rows), 5)
        self.assertEqual({r[4] for r in rows}, {"OK", "NOT_FOUND", "LOW_COVERAGE"})
        self.assertEqual(sum(r[13] for r in rows), 7)            # added
        self.assertEqual(sum(r[14] for r in rows), 1)            # failed
        # comments in the hotels tab survive
        self.assertEqual(sum(1 for r in wb[W.HOTELS_SHEET].iter_rows() for c in r if c.comment), 24)

        # ---------------- second run is idempotent: the 7 now-known photos are no longer "new"
        shutil.rmtree(self.run_dir / "snapshots")
        Handler.missing = set()
        # re-seed against the UPDATED workbook as the baseline
        wb2 = W.open_workbook(str(out_path))
        self.index = W.read_index(wb2, W.read_hotels(wb2))
        self.seed(28, new_untagged=[9000001, 9000002], new_in_room=[(legacy_cat, 9000003)])
        rc = cli.main(["--workbook", str(out_path), "--run-dir", str(self.run_dir), "--only", "28", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(list(csv.DictReader(open(self.run_dir / "new_images.csv", encoding="utf-8-sig")))), 0)


if __name__ == "__main__":
    unittest.main()
