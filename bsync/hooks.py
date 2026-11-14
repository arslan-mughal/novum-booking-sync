"""Extension points used by cloud_sync.py (all optional; the plain CLI ignores them)."""
from .model import HotelScrape


class Hooks:
    """Extension points used by cloud_sync.py. All optional; the plain CLI ignores them."""
    def order_hotels(self, hotels: list) -> list:             # choose processing order (stalest first)
        return hotels

    def after_scrape(self, hotel, scrape: HotelScrape) -> None:   # e.g. remember when a hotel was checked
        pass

    def after_download(self, items: list) -> list:              # e.g. upload to OneDrive; return the items to record
        return items

    def finish(self, ctx: dict) -> None:                        # called after the workbook has been saved
        pass
