"""Compare what a Booking.com page shows against what the workbook already holds.

Identity of an image  = Booking photo id (the number inside the URL and in the file name, e.g. 01_702878002.jpg).
"New" means:
  * room photo     -> (hotel, room category, photo id) is not in the workbook yet
  * untagged photo -> the photo id is not filed ANYWHERE for that hotel yet
Existing files/rows are never renamed or renumbered; new images continue each group's numbering.
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import (
    GENERAL, SPECIAL_BUCKETS, HotelDiff, HotelIndex, HotelScrape, NewImage,
)
from .util import norm_category, sanitize_segment, similarity


@dataclass
class DiffConfig:
    untagged_category: str = GENERAL   # where new gallery photos that belong to no room are filed
    min_coverage: float = 0.50         # share of already-known photos a scrape must re-find to be trusted
    rename_overlap: float = 0.60       # photo overlap needed to treat an unknown room name as a renamed one
    rename_similarity: float = 0.55    # ...and name similarity
    force_low_coverage: bool = False
    allow_missing_rooms: bool = False


def resolve_category(name: str, scraped_pids: set, index: HotelIndex, cfg: DiffConfig,
                     taken: set) -> tuple[str, str]:
    """Map a room name from the page onto an existing category of the hotel.
    Returns (final_category, note). note is '' for an exact match."""
    rooms = {c: g for c, g in index.groups.items() if c not in SPECIAL_BUCKETS and c not in taken}
    if name in index.groups and name not in SPECIAL_BUCKETS:
        return name, ""
    n = norm_category(name)
    for c in rooms:
        if norm_category(c) == n:
            return c, f"room '{name}' matched existing '{c}' (spelling/dash/case difference)"
    best, best_score = None, 0.0
    if scraped_pids:
        for c, g in rooms.items():
            overlap = len(scraped_pids & g.pids) / len(scraped_pids)
            sim = similarity(n, norm_category(c))
            if overlap >= cfg.rename_overlap and sim >= cfg.rename_similarity and overlap + sim > best_score:
                best, best_score = c, overlap + sim
    if best:
        return best, f"room '{name}' treated as renamed '{best}' (same photos, similar name)"
    return name, ""


def diff_hotel(index: HotelIndex, scrape: HotelScrape, cfg: DiffConfig,
               owner_by_pid: dict, claimed_new: dict, images_root_name: str) -> HotelDiff:
    d = HotelDiff(sr=index.sr, name=index.name, page_url=index.page_url, status=scrape.status)
    existing = index.all_pids()
    d.existing_n = len(existing)

    if scrape.status != "OK":
        d.notes.append(f"scrape status {scrape.status}: " + "; ".join(scrape.notes[:3]))
        return d

    # --- de-duplicate the scrape; a photo tagged to a room is not also an 'untagged' gallery photo
    tagged_pids = {i.pid for i in scrape.images if i.category}
    seen, imgs = set(), []
    for im in sorted(scrape.images, key=lambda i: i.order):
        key = (im.category, im.pid)
        if key in seen or (im.category is None and im.pid in tagged_pids):
            continue
        seen.add(key)
        imgs.append(im)
    scraped_pids = {i.pid for i in imgs}
    d.scraped_n = len(scraped_pids)
    d.refound_n = len(existing & scraped_pids)
    d.missing_n = len(existing - scraped_pids)
    d.coverage = (d.refound_n / d.existing_n) if d.existing_n else 1.0

    if d.scraped_n == 0:
        d.status = "NO_PHOTOS"
        return d
    if d.existing_n and d.coverage < cfg.min_coverage and not cfg.force_low_coverage:
        d.status = "LOW_COVERAGE"
        d.notes.append(
            f"only {d.refound_n}/{d.existing_n} known photos re-found ({d.coverage:.0%}) - page probably "
            f"loaded partially or changed; nothing applied (use --force-low-coverage to override)")
        return d

    # --- map scraped room names onto the workbook's categories
    cat_pids: dict = {}
    for im in imgs:
        if im.category:
            cat_pids.setdefault(im.category, set()).add(im.pid)
    cat_map, taken = {}, set()
    for name, pids in cat_pids.items():          # insertion order = page order
        final, note = resolve_category(name, pids, index, cfg, taken)
        cat_map[name] = final
        if final in index.groups:
            taken.add(final)
        if note:
            d.notes.append(note)

    if (index.room_groups and not cat_pids and not cfg.allow_missing_rooms):
        d.status = "ROOMS_MISSING"
        d.notes.append(
            f"the workbook has {len(index.room_groups)} room categories but the scrape found no room-tagged "
            f"photos - room attribution probably failed, nothing applied (--allow-missing-rooms to override)")
        return d

    # --- decide what is new (local state only: the index is never mutated)
    folders = {c: g.folder for c, g in index.groups.items()}
    counters = {c: g.max_no for c, g in index.groups.items()}
    new_cats: list = []
    for im in imgs:
        if im.category:
            cat, kind = cat_map[im.category], "room"
            grp = index.groups.get(cat)
            if grp and im.pid in grp.pids:
                continue
            if scrape.json_source and im.pid in existing:
                # embedded-JSON scrape: the photo is already filed somewhere for this hotel,
                # only under a different room than Booking's gallery JSON now lists it. The
                # workbook's original filing stands; a sync adds genuinely new photos only.
                continue
        else:
            cat, kind = cfg.untagged_category, "untagged"
            if im.pid in existing:
                continue
        owner = owner_by_pid.get(im.pid)
        if owner and owner != index.page_url:
            d.notes.append(f"photo {im.pid} already belongs to another hotel in the workbook - skipped")
            continue
        claimed_by = claimed_new.get(im.pid)
        if claimed_by and claimed_by != index.page_url:
            d.notes.append(f"photo {im.pid} was already claimed as new by another hotel in this run - skipped")
            continue
        claimed_new[im.pid] = index.page_url

        if cat not in folders:                       # a room category the workbook has not seen before
            folders[cat] = f"{index.hotel_folder}/{sanitize_segment(cat)}"
            counters[cat] = 0
            new_cats.append(cat)
        counters[cat] += 1
        d.new_images.append(NewImage(
            page_url=index.page_url, sr=index.sr, hotel_name=index.name, category=cat, folder=folders[cat],
            no=counters[cat], pid=im.pid, url=im.url, caption=im.caption,
            new_category=cat in new_cats, kind=kind, also_elsewhere=(im.pid in existing),
        ))
    d.new_categories = new_cats
    return d
