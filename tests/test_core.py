import json
import unittest
from collections import OrderedDict

from bsync.diff import DiffConfig, diff_hotel
from bsync.model import Group, HotelIndex, HotelScrape, ScrapedImage
from bsync.scraper import assemble, clean_caption, extract_embedded, with_dates
from bsync.util import norm_category, photo_id, sanitize_segment, size_score, to_large
from bsync.workbook import local_file_link

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

    def test_local_file_link_matches_existing_rows(self):
        # row 2 of the real workbook, verbatim
        self.assertEqual(
            local_file_link("novum-hotels-images/Garner Hotel Berlin - Schöneberg/General Images",
                            "01_702878002.jpg"),
            "image/Garner%20Hotel%20Berlin%20-%20Sch%C3%B6neberg/General%20Images/01_702878002.jpg")
        # legacy folder name with size token, and ² encoded
        self.assertEqual(
            local_file_link("novum-hotels-images/Hotel X/Standardzimmer - 12 m²", "03_10.jpg"),
            "image/Hotel%20X/Standardzimmer%20-%2012%20m%C2%B2/03_10.jpg")

    def test_disk_folder_matches_the_hyperlink_target(self):
        # files must land where the 'Local File' hyperlink points: 'image/<hotel>/<room>'
        # relative to the workbook's folder, not under the legacy 'novum-hotels-images/' root.
        from bsync.workbook import disk_folder
        self.assertEqual(disk_folder("novum-hotels-images/Garner Hotel Berlin - Schöneberg/General Images"),
                         "image/Garner Hotel Berlin - Schöneberg/General Images")
        self.assertEqual(disk_folder("novum-hotels-images/Hotel X/Standardzimmer - 12 m²"),
                         "image/Hotel X/Standardzimmer - 12 m²")
        self.assertEqual(disk_folder("image/Hotel X/General Images"),
                         "image/Hotel X/General Images")

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

    def test_json_source_never_refiles_a_photo_the_workbook_already_holds(self):
        # the embedded-gallery JSON lists rooms by Booking's current assignment, which disagrees
        # with the workbook's historical lightbox filing for some known photos: those must not
        # come back as "new" rows under another room. A genuinely new photo still gets a row
        # under each of its rooms.
        sc = scrape(("1", None), ("2", None), ("3", None), ("10", "Standardzimmer (12 m²)"),
                    ("11", "Standardzimmer (12 m²)"), ("20", "Premium Zimmer"),
                    ("10", "Premium Zimmer"), ("200", "Premium Zimmer"))
        sc.json_source = True
        d = run(make_index(), sc)
        self.assertEqual([(n.pid, n.category) for n in d.new_images], [("200", "Premium Zimmer")])

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
        self.assertTrue(any("renamed" in n or "size suffix differs" in n for n in d.notes))

    def test_sized_scraped_room_maps_onto_unsized_existing_category(self):
        # the workbook filed this hotel's rooms without sizes; the JSON path reports them with
        # one. Most of the room's photos are new, so the overlap-based rename rule cannot see
        # it - the base name alone must map it, or a duplicate room folder is created.
        idx = make_index()
        idx.groups["Standardzimmer"] = Group("Standardzimmer", "novum-hotels-images/Hotel X/Standardzimmer",
                                             max_no=2, pids={"10", "11"}, rows=2)
        del idx.groups["Standardzimmer (12 m²)"]
        d = run(idx, scrape(("1", None), ("2", None), ("3", None), ("10", "Standardzimmer (12 m²)"),
                            ("11", "Standardzimmer (12 m²)"), ("500", "Standardzimmer (12 m²)"),
                            ("501", "Standardzimmer (12 m²)"), ("502", "Standardzimmer (12 m²)")))
        self.assertEqual(d.new_categories, [])
        self.assertEqual({(n.pid, n.category) for n in d.new_images},
                         {("500", "Standardzimmer"), ("501", "Standardzimmer"), ("502", "Standardzimmer")})

    def test_same_base_with_two_sized_existing_categories_uses_size(self):
        idx = make_index()
        idx.groups["Standardzimmer (23 m²)"] = Group(
            "Standardzimmer (23 m²)", "novum-hotels-images/Hotel X/Standardzimmer - 23 m²",
            max_no=1, pids={"30"}, rows=1)
        d = run(idx, scrape(("1", None), ("2", None), ("3", None), ("10", "Standardzimmer (12 m²)"),
                            ("11", "Standardzimmer (12 m²)"), ("30", "Standardzimmer (23 m²)"),
                            ("600", "Standardzimmer (23 m²)")))
        self.assertEqual(d.new_categories, [])
        self.assertEqual([(n.pid, n.category) for n in d.new_images], [("600", "Standardzimmer (23 m²)")])

    def test_bis_zu_personen_suffix_does_not_create_a_new_category(self):
        idx = make_index()
        idx.groups["Premium Zweibettzimmer"] = Group(
            "Premium Zweibettzimmer", "novum-hotels-images/Hotel X/Premium Zweibettzimmer",
            max_no=1, pids={"40"}, rows=1)
        d = run(idx, scrape(("1", None), ("2", None), ("3", None),
                            ("40", "Premium Zweibettzimmer – bis zu 3 Personen (28 m²)"),
                            ("700", "Premium Zweibettzimmer – bis zu 3 Personen (28 m²)")))
        self.assertEqual(d.new_categories, [])
        self.assertEqual([(n.pid, n.category) for n in d.new_images], [("700", "Premium Zweibettzimmer")])

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


class GalleryJsonTests(unittest.TestCase):
    """The embedded-gallery path (v1.0.2): RoomDetails + propertyGallery Apollo JSON."""

    def page(self):
        # minimal Apollo-style cache: 2 rooms (one name carries the 'bis zu N Erwachsene'
        # suffix Booking appends in the room table) + a 3-photo main gallery
        data = {
            "RoomDetails:11": {"__typename": "RoomDetails", "id": 11,
                               "translations": {"__typename": "RoomTranslation",
                                                "name": "Standard Zimmer mit Queensize-Bett"},
                               "roomSizeM2": 12,
                               "roomPhotos": [{"__ref": "RDSRoomPhoto:101"},
                                              {"__ref": "RDSRoomPhoto:102"}]},
            "RoomDetails:22": {"__typename": "RoomDetails", "id": 22,
                               "translations": {"__typename": "RoomTranslation",
                                                "name": "Premium-Zimmer mit 3 Einzelbetten – bis zu 3 Erwachsene"},
                               "roomSizeM2": 27,
                               "roomPhotos": [{"__ref": "RDSRoomPhoto:102"},
                                              {"__ref": "RDSRoomPhoto:103"}]},
            "Property:1": {"propertyGallery({\"limit\":500})": {
                "__typename": "PropertyGallery",
                "mainPhoto": {"__ref": "AccommodationPhoto:101"},
                "mainGalleryPhotos": [{"__ref": "AccommodationPhoto:101"},
                                      {"__ref": "AccommodationPhoto:102"},
                                      {"__ref": "AccommodationPhoto:103"}]}},
            "AccommodationPhoto:101": {"__typename": "AccommodationPhoto", "id": 101,
                                       "altText": "Lobby",
                                       "resource({\"size\":\"max1024x768\"})": {
                                           "__typename": "PhotoResource",
                                           "absoluteUrl": cdn("101")}},
            "AccommodationPhoto:102": {"__typename": "AccommodationPhoto", "id": 102,
                                       "altText": "Zimmer",
                                       "resource({\"size\":\"max300\"})": {
                                           "__typename": "PhotoResource",
                                           "absoluteUrl": cdn("102", "max300")}},
            "AccommodationPhoto:103": {"__typename": "AccommodationPhoto", "id": 103,
                                       "altText": "Bad",
                                       "resource({\"size\":\"max1024x768\"})": {
                                           "__typename": "PhotoResource",
                                           "absoluteUrl": cdn("103")}},
        }
        import json as _json
        return '<script type="application/json">' + _json.dumps(data) + "</script>"

    def test_room_details_give_sized_names_and_gallery_order(self):
        emb = extract_embedded(self.page())
        self.assertEqual(emb.gallery_pids, ["101", "102", "103"])
        self.assertEqual(emb.room_details,
                         {"11": "Standard Zimmer mit Queensize-Bett (12 m²)",
                          "22": "Premium-Zimmer mit 3 Einzelbetten (27 m²)"})
        self.assertEqual(emb.room_photos, {"11": ["101", "102"], "22": ["102", "103"]})

    def test_assemble_from_gallery_tags_rooms_and_leaves_the_rest_untagged(self):
        from bsync.scraper import assemble_from_gallery
        emb = extract_embedded(self.page())
        out = assemble_from_gallery(emb)
        got = sorted((i.category or "-", i.pid) for i in out)
        # 102 belongs to both rooms -> one row per room; nothing is left untagged here
        self.assertEqual(got, [("Premium-Zimmer mit 3 Einzelbetten (27 m²)", "102"),
                               ("Premium-Zimmer mit 3 Einzelbetten (27 m²)", "103"),
                               ("Standard Zimmer mit Queensize-Bett (12 m²)", "101"),
                               ("Standard Zimmer mit Queensize-Bett (12 m²)", "102")])
        self.assertTrue(next(i for i in out if i.pid == "102").url.endswith("max300/102.jpg?k=abc123&o="))

    def test_no_gallery_json_means_empty_so_the_dom_fallback_runs(self):
        from bsync.scraper import assemble_from_gallery
        emb = extract_embedded('<script type="application/json">{"a": 1}</script>')
        self.assertEqual(assemble_from_gallery(emb), [])

    def test_sized_room_name_strips_bis_zu_suffixes(self):
        from bsync.scraper import _sized_room_name
        self.assertEqual(_sized_room_name("Premium Zweibettzimmer – bis zu 3 Personen", 28),
                         "Premium Zweibettzimmer (28 m²)")
        self.assertEqual(_sized_room_name("Premium-Zimmer mit 3 Einzelbetten – bis zu 3 Erwachsene", 27),
                         "Premium-Zimmer mit 3 Einzelbetten (27 m²)")
        self.assertEqual(_sized_room_name("Standardzimmer", None), "Standardzimmer")


if __name__ == "__main__":
    unittest.main()
