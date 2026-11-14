"""Plain data containers shared by the scraper, the comparer and the workbook writer."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

# Buckets the existing workbook uses that are NOT room names.
GENERAL = "General Images"
GALLERY_ONLY = "Gallery Only - No Room"
ROOM_DETAIL_NA = "Room Detail - Not Assignable"
SPECIAL_BUCKETS = (GENERAL, GALLERY_ONLY, ROOM_DETAIL_NA)


# ---------------------------------------------------------------- scrape side
@dataclass
class ScrapedImage:
    pid: str
    url: str
    category: Optional[str]  # room name as shown on Booking.com; None = gallery photo not tied to a room
    caption: str = ""
    order: int = 0


@dataclass
class HotelScrape:
    page_url: str
    status: str = "OK"  # OK | NO_PHOTOS | BLOCKED | NOT_FOUND | ERROR
    images: list[ScrapedImage] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)
    scraped_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "HotelScrape":
        d = dict(d)
        d["images"] = [ScrapedImage(**i) for i in d.get("images", [])]
        return HotelScrape(**d)


# ---------------------------------------------------------------- workbook side
@dataclass
class Group:
    """All existing rows of one hotel + room category."""
    category: str
    folder: str
    max_no: int = 0
    pids: set = field(default_factory=set)
    rows: int = 0


@dataclass
class HotelIndex:
    sr: int
    name: str            # exactly as written in the 'Images - Booking.com' tab (not the hotels tab)
    page_url: str
    groups: dict = field(default_factory=dict)   # category -> Group
    hotel_folder: str = ""                       # '<root>/<hotel>' prefix reused for new categories

    def all_pids(self) -> set:
        out: set = set()
        for g in self.groups.values():
            out |= g.pids
        return out

    @property
    def room_groups(self) -> dict:
        return {c: g for c, g in self.groups.items() if c not in SPECIAL_BUCKETS}


@dataclass
class HotelRow:
    """One line of the 'NOVUM-Hotels' tab that has a Booking.com link."""
    sr: int
    name: str
    page_url: str


# ---------------------------------------------------------------- diff side
@dataclass
class NewImage:
    page_url: str
    sr: int
    hotel_name: str
    category: str
    folder: str
    no: int
    pid: str
    url: str
    caption: str = ""
    new_category: bool = False
    kind: str = "room"            # room | untagged
    also_elsewhere: bool = False  # photo id already filed under another category of the same hotel
    # filled in by the downloader:
    file_name: str = ""
    url_used: str = ""

    @property
    def url_large(self) -> str:
        from .util import to_large
        return to_large(self.url)

    def planned_file_name(self, ext: str = "jpg") -> str:
        return f"{self.no:02d}_{self.pid}.{ext}"


@dataclass
class HotelDiff:
    sr: int
    name: str
    page_url: str
    status: str = "OK"
    existing_n: int = 0
    scraped_n: int = 0
    refound_n: int = 0
    coverage: float = 0.0
    missing_n: int = 0
    new_images: list = field(default_factory=list)
    new_categories: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def new_in_rooms(self) -> int:
        return sum(1 for n in self.new_images if n.kind == "room")

    @property
    def new_untagged(self) -> int:
        return sum(1 for n in self.new_images if n.kind == "untagged")
