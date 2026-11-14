import json
import unittest
from collections import OrderedDict

from bsync.diff import DiffConfig, diff_hotel
from bsync.model import Group, HotelIndex, HotelScrape, ScrapedImage
from bsync.scraper import assemble, clean_caption, extract_embedded, with_dates
from bsync.util import norm_category, photo_id, sanitize_segment, size_score, to_large

URL = "https://www.booking.com/hotel/de/x.de.html"
OTHER = "https://www.booking.com/hotel/de/y.de.html"
CDN = "https://cf.bstatic.com/xdata/images/hotel/{size}/{pid}.jpg?k=abc123&o="


def cdn(pid, size="max1024x768"):
    return CDN.format(size=size, pid=pid)


def make_index():
    idx = HotelIndex(sr=7, name="Hotel X", page_url=URL, hotel_folder="novum-hotels-images/Hotel X")
    idx.groups["General Images"] = Group("General Images", "novum-hotels-images/Hotel X/General Images",
                                         max_no=3, pids={"1", "2", "3"}, rows=3)
    idx.groups["Standardzimmer (12 m²)"] = Group(
        "Standardzimmer (12 m²)", "novum-hotels-images/Hotel X/Standardzimmer - 12 m²",   # legacy folder name
        max_no=2, pids={"10", "11"}, rows=2)
    idx.groups["Premium Zimmer"] = Group("Premium Zimmer", "novum-hotels-images/Hotel X/Premium Zimmer",
                                         max_no=2, pids={"20", "11"}, rows=2)
    return idx


def scrape(*imgs, status="OK"):
    s = HotelScrape(page_url=URL, status=status)
    s.images = [ScrapedImage(pid=p, url=cdn(p, "max300"), category=c, order=i) for i, (p, c) in enumerate(imgs)]
    return s


def run(idx, sc, cfg=None, owner=None, claimed=None):
    return diff_hotel(idx, sc, cfg or DiffConfig(), owner or {}, claimed if claimed is not None else {}, "novum-hotels-images")


class UtilTests(unittest.TestCase):
    def test_photo_id_and_escaping(self):
        self.assertEqual(photo_id(cdn("702878002")), "702878002")
        self.assertEqual(photo_id("https://q-xx.bstatic.com/xdata/images/hotel/square600/55.webp"), "55")
        self.assertIsNone(photo_id("https://cf.bstatic.com/xdata/images/xphoto/300x240/55.jpg"))

    def test_to_large_keeps_key(self):
        self.assertEqual(to_large(cdn("9", "max300")), cdn("9"))
        self.assertIn("/max1024x768/9.jpg", to_large("https://x/xdata/images/hotel/square600/9.webp?k=1"))

    def test_size_ranking(self):
        self.assertGreater(size_score(cdn("1")), size_score(cdn("1", "max300")))
        self.assertGreater(size_score(cdn("1", "max1280x900")), size_score(cdn("1")))

    def test_sanitize_matches_existing_workbook_convention(self):
        self.assertEqual(sanitize_segment("Premium Zimmer mit Queensize-Bett – Hohe Etage/Stadtblick"),
                         "Premium Zimmer mit Queensize-Bett – Hohe Etage-Stadtblick")
        self.assertEqual(sanitize_segment("Standard Queen Room with Bath/Shower"), "Standard Queen Room with Bath-Shower")
        self.assertEqual(sanitize_segment('A: "B"? '), "A- -B--")
        self.assertEqual(sanitize_segment("Studio-Apartment (2 Erwachsene)"), "Studio-Apartment (2 Erwachsene)")

    def test_norm_category_keeps_size(self):
        self.assertEqual(norm_category("Standardzimmer – Nebengebäude"), norm_category("standardzimmer - nebengebäude"))
        self.assertNotEqual(norm_category("Standardzimmer (12 m²)"), norm_category("Standardzimmer (15 m²)"))


class DiffTests(unittest.TestCase):
    def test_nothing_new(self):
        d = run(make_index(), scrape(("1", None), ("2", None), ("3", None), ("10", "Standardzimmer (12 m²)"),
                                     ("11", "Standardzimmer (12 m²)"), ("20", "Premium Zimmer")))
        self.assertEqual(d.status, "OK")
        self.assertEqual(d.new_images, [])
        self.assertEqual(d.coverage, 1.0)

    def test_new_general_and_room_images_continue_numbering_and_reuse_folder(self):
        d = run(make_index(), scrape(("1", None), ("2", None), ("3", None), ("100", None),
                                     ("10", "Standardzimmer (12 m²)"), ("11", "Standardzimmer (12 m²)"),
                                     ("200", "Standardzimmer (12 m²)"), ("20", "Premium Zimmer")))
        by = {n.pid: n for n in d.new_images}
        self.assertEqual(set(by), {"100", "200"})
        self.assertEqual((by["100"].category, by["100"].no, by["100"].kind), ("General Images", 4, "untagged"))
        self.assertEqual(by["100"].folder, "novum-hotels-images/Hotel X/General Images")
        self.assertEqual((by["200"].no, by["200"].folder), (3, "novum-hotels-images/Hotel X/Standardzimmer - 12 m²"))
        self.assertEqual(by["200"].planned_file_name(), "03_200.jpg")
        self.assertTrue(by["100"].url_large.endswith("max1024x768/100.jpg?k=abc123&o="))
        self.assertIn("max300/100.jpg", by["100"].url)   # scraped URL kept for download fallback

    def test_photo_shared_with_another_room_is_new_for_that_room(self):
        d = run(make_index(), scrape(("1", None), ("2", None), ("3", None), ("10", "Standardzimmer (12 m²)"),
                                     ("11", "Standardzimmer (12 m²)"), ("20", "Premium Zimmer"),
                                     ("10", "Premium Zimmer")))
        self.assertEqual([(n.pid, n.category, n.also_elsewhere) for n in d.new_images],
                         [("10", "Premium Zimmer", True)])

    def test_new_room_category_gets_sanitised_folder_and_numbering_from_one(self):
        d = run(make_index(), scrape(("1", None), ("2", None), ("3", None), ("10", "Standardzimmer (12 m²)"),
                                     ("11", "Standardzimmer (12 m²)"), ("20", "Premium Zimmer"),
                                     ("300", "Suite mit Bad/Dusche"), ("301", "Suite mit Bad/Dusche")))
        self.assertEqual(d.new_categories, ["Suite mit Bad/Dusche"])
        self.assertEqual([(n.no, n.folder) for n in d.new_images],
                         [(1, "novum-hotels-images/Hotel X/Suite mit Bad-Dusche"),
                          (2, "novum-hotels-images/Hotel X/Suite mit Bad-Dusche")])

    def test_renamed_room_is_mapped_not_duplicated(self):
        # same photos, name only differs by size suffix -> must not create a new category
        d = run(make_index(), scrape(("1", None), ("2", None), ("3", None), ("10", "Standardzimmer"),
                                     ("11", "Standardzimmer"), ("20", "Premium Zimmer"), ("500", "Standardzimmer")))
        self.assertEqual(d.new_categories, [])
        self.assertEqual([(n.pid, n.category) for n in d.new_images], [("500", "Standardzimmer (12 m²)")])
        self.assertTrue(any("renamed" in n for n in d.notes))

    def test_two_new_rooms_do_not_both_claim_one_existing_category(self):
        d = run(make_index(), scrape(("1", None), ("2", None), ("3", None), ("10", "Standardzimmer (12 m²)"),
                                     ("11", "Standardzimmer (12 m²)"), ("20", "Premium Zimmer"),
                                     ("10", "Standardzimmer (12 m²)"),  # exact match claims the existing one
                                     ("10", "Standardzimmer (15 m²)"), ("11", "Standardzimmer (15 m²)")))
        self.assertEqual(d.new_categories, ["Standardzimmer (15 m²)"])

    def test_photo_tagged_to_room_is_not_also_filed_as_general(self):
        d = run(make_index(), scrape(("1", None), ("2", None), ("3", None), ("10", "Standardzimmer (12 m²)"),
                                     ("11", "Standardzimmer (12 m²)"), ("20", "Premium Zimmer"),
                                     ("700", None), ("700", "Premium Zimmer")))
        self.assertEqual([(n.pid, n.category) for n in d.new_images], [("700", "Premium Zimmer")])

    def test_cross_hotel_leak_is_skipped(self):
        d = run(make_index(), scrape(("1", None), ("2", None), ("3", None), ("10", "Standardzimmer (12 m²)"),
                                     ("11", "Standardzimmer (12 m²)"), ("20", "Premium Zimmer"), ("900", None)),
                owner={"900": OTHER})
        self.assertEqual(d.new_images, [])
        self.assertTrue(any("another hotel" in n for n in d.notes))

    def test_low_coverage_applies_nothing(self):
        d = run(make_index(), scrape(("1", None), ("999", None), ("998", None)))
        self.assertEqual(d.status, "LOW_COVERAGE")
        self.assertEqual(d.new_images, [])
        d2 = run(make_index(), scrape(("1", None), ("999", None)), DiffConfig(force_low_coverage=True, allow_missing_rooms=True))
        self.assertEqual(d2.status, "OK")
        self.assertEqual(len(d2.new_images), 1)

    def test_rooms_missing_gate(self):
        d = run(make_index(), scrape(("1", None), ("2", None), ("3", None), ("10", None), ("11", None), ("20", None), ("55", None)))
        self.assertEqual(d.status, "ROOMS_MISSING")
        self.assertEqual(d.new_images, [])

    def test_failed_scrape_statuses_pass_through(self):
        for st in ("BLOCKED", "ERROR", "NOT_FOUND"):
            self.assertEqual(run(make_index(), scrape(status=st)).status, st)
        self.assertEqual(run(make_index(), scrape()).status, "NO_PHOTOS")

    def test_index_is_not_mutated_and_run_is_repeatable(self):
        idx = make_index()
        sc = scrape(("1", None), ("2", None), ("3", None), ("10", "Standardzimmer (12 m²)"),
                    ("11", "Standardzimmer (12 m²)"), ("20", "Premium Zimmer"), ("300", "Brand New Room"))
        a = run(idx, sc)
        b = run(idx, sc)
        self.assertEqual([(n.category, n.no) for n in a.new_images], [(n.category, n.no) for n in b.new_images])
        self.assertNotIn("Brand New Room", idx.groups)

    def test_claimed_across_hotels_in_one_run(self):
        claimed = {"3000": OTHER}
        d = run(make_index(), scrape(("1", None), ("2", None), ("3", None), ("10", "Standardzimmer (12 m²)"),
                                     ("11", "Standardzimmer (12 m²)"), ("20", "Premium Zimmer"), ("3000", None)),
                claimed=claimed)
        self.assertEqual(d.new_images, [])


class EmbeddedTests(unittest.TestCase):
    def page(self, data, typ="application/json"):
        return f'<html><script type="{typ}">{json.dumps(data)}</script><script>var x = 1;</script></html>'

    def test_photos_rooms_and_tags_from_nested_json(self):
        data = {"props": {"hotelPhotos": [
            {"id": 1, "large_url": cdn("111"), "thumb_url": cdn("111", "max300"), "alt": "Bett im Zimmer",
             "associated_rooms": [5001]},
            {"id": 2, "large_url": cdn("222"), "associated_rooms": []},
            {"id": 3, "large_url": cdn("333"), "associated_rooms": [5001, 5002]}],
            "rooms": [{"id": 5001, "name": "Standardzimmer"}, {"id": 5002, "name": "Suite"}]}}
        emb = extract_embedded(self.page(data))
        self.assertEqual(set(emb.photos), {"111", "222", "333"})
        self.assertEqual(emb.rooms, {"5001": "Standardzimmer", "5002": "Suite"})
        self.assertEqual(emb.room_names_for(emb.photos["333"]), ["Standardzimmer", "Suite"])
        self.assertEqual(emb.room_names_for(emb.photos["222"]), [])
        self.assertTrue(emb.photos["111"].url.endswith("max1024x768/111.jpg?k=abc123&o="))   # largest URL kept
        self.assertEqual(emb.stats["photos_tagged_to_rooms"], 2)

    def test_apollo_style_refs_and_ldjson_ignored_safely(self):
        data = {"Room:77": {"__typename": "Room", "id": "77", "name": "Familienzimmer"},
                "Photo:9": {"__typename": "Photo", "url": cdn("999"), "rooms": [{"__ref": "Room:77"}]}}
        emb = extract_embedded(self.page(data) + self.page({"@type": "Hotel", "name": "X"}, "application/ld+json"))
        self.assertEqual(emb.room_names_for(emb.photos["999"]), ["Familienzimmer"])

    def test_broken_json_is_skipped(self):
        emb = extract_embedded('<script type="application/json">{not json</script>')
        self.assertEqual(emb.photos, {})

    def test_assemble_merges_sources_and_removes_room_photos_from_untagged(self):
        emb = extract_embedded(self.page({"photos": [
            {"id": 1, "url": cdn("111"), "rooms": ["Standardzimmer (12 m²)"]},
            {"id": 2, "url": cdn("444"), "rooms": ["standardzimmer (12 m²)"]},
            {"id": 3, "url": cdn("555"), "rooms": ["Nur im JSON"]}]}))
        gallery = OrderedDict([("100", (cdn("100", "max300"), "")), ("111", (cdn("111", "max300"), ""))])
        rooms = [("Standardzimmer (12 m²)", OrderedDict([("111", (cdn("111", "max300"), "")), ("200", (cdn("200"), ""))]))]
        out = assemble(emb, gallery, rooms)
        got = sorted((i.category or "-", i.pid) for i in out)
        self.assertEqual(got, [("-", "100"), ("Nur im JSON", "555"), ("Standardzimmer (12 m²)", "111"),
                               ("Standardzimmer (12 m²)", "200"), ("Standardzimmer (12 m²)", "444")])
        self.assertTrue(next(i for i in out if i.pid == "111").url.endswith("max1024x768/111.jpg?k=abc123&o="))

    def test_caption_cleaning_and_dates(self):
        self.assertEqual(clean_caption("Hotel X - Foto 3", "Hotel X"), "")
        self.assertEqual(clean_caption("Photo 12"), "")
        self.assertEqual(clean_caption("Lobby mit Rezeption", "Hotel X"), "Lobby mit Rezeption")
        self.assertIn("checkin=2027-01-10", with_dates(URL, "2027-01-10", "2027-01-11"))
        self.assertEqual(with_dates(URL, None, None), URL)


if __name__ == "__main__":
    unittest.main()
