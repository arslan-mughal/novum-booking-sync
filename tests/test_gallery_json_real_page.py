"""End-to-end check of the embedded-gallery path against a REAL saved Booking.com page
and the REAL workbook (skipped unless both env vars are set).

The page must be a --debug save (sync_run/debug/028_page.html) for a hotel that is already
fully filed in the workbook: the gallery JSON must re-find every known photo (coverage 100%)
and find nothing new, with every room resolving onto an existing category.

Run:  NOVUM_XLSX=/path/to/workbook.xlsx NOVUM_DEBUG_HTML=/path/to/028_page.html \\
       python -m unittest tests.test_gallery_json_real_page -v
"""
import os
import unittest

XLSX = os.environ.get("NOVUM_XLSX")
HTML = os.environ.get("NOVUM_DEBUG_HTML")


@unittest.skipUnless(XLSX and HTML, "needs NOVUM_XLSX and NOVUM_DEBUG_HTML")
class GalleryJsonRealPage(unittest.TestCase):
    def test_real_page_matches_real_workbook(self):
        from bsync import workbook as W
        from bsync.diff import DiffConfig, diff_hotel
        from bsync.model import HotelScrape
        from bsync.scraper import assemble_from_gallery, clean_caption, extract_embedded

        wb = W.open_workbook(XLSX)
        hotels = W.read_hotels(wb)
        index = W.read_index(wb, hotels)
        owner = W.owner_map(index)

        with open(HTML, encoding="utf-8", errors="replace") as fh:
            html_text = fh.read()
        emb = extract_embedded(html_text)
        images = assemble_from_gallery(emb)
        for im in images:
            im.caption = clean_caption(im.caption, "")

        # the debug page is Sr 28's Booking.com URL - find its hotel in the workbook
        url = "https://www.booking.com/hotel/de/garner-hotel-berlin-schoeneberg.de.html"
        hotel = next(h for h in hotels if h.page_url == url)
        idx = index[url]

        self.assertEqual(len(emb.gallery_pids), 93, "main gallery must hold all 93 photos")
        self.assertEqual(len(emb.room_details), 11, "11 room types in the gallery JSON")
        self.assertEqual(len({i.pid for i in images}), 93, "scrape must see all 93 photo ids")
        self.assertEqual({i.category for i in images if i.category},
                         {c for c in idx.groups if c not in ("General Images", "Gallery Only - No Room")},
                         "every room must resolve onto an existing workbook category")

        scrape = HotelScrape(page_url=url, status="OK" if images else "NO_PHOTOS", images=images,
                             json_source=True)  # as scrape() sets it for the embedded-JSON path
        d = diff_hotel(idx, scrape, DiffConfig(), owner, {}, "novum-hotels-images")
        self.assertEqual(d.status, "OK", "; ".join(d.notes))
        self.assertEqual(d.coverage, 1.0, f"only {d.refound_n}/{d.existing_n} known photos re-found")
        self.assertEqual(len(d.new_images), 0, f"unexpected new photos: {d.new_images[:3]}")
        self.assertEqual(d.new_categories, [])


if __name__ == "__main__":
    unittest.main()
