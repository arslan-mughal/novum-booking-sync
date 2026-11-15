# NOVUM Booking.com image sync

> **Automatic runs with OneDrive (free scheduler): see `README_CLOUD.md`.** This file covers the checker itself and running it by hand.

Visits every Booking.com hotel link in the **NOVUM-Hotels** tab, finds photos that are **not yet** in
**Images - Booking.com** (hotel gallery photos *and* each room's photos), downloads them into your
`novum-hotels-images` folder using the same naming scheme, and appends them to the workbook.

> **Read this first - what is and isn't verified**
> The comparison, numbering, folder naming, downloading and workbook update are tested end-to-end against
> your real workbook. **v1.0.4 (15 Nov 2026):** new photos are written under `image/<hotel>/<room>/` next to the workbook -
both locally and on OneDrive - because that is where the workbook's 'Local File' hyperlinks point; the
'Local Folder' column still names the legacy `novum-hotels-images/` root. **v1.0.3 (15 Nov 2026):** the room-name mapping now also matches on the base name
> when only the size suffix differs ('Standardzimmer' in the workbook = 'Standardzimmer (12 m²)' on the
> page) - the workbook filed some hotels' rooms without sizes, and without this the sync created a
> duplicate room folder next to the existing one. **v1.0.2:** the scraper reads the gallery from the data
> embedded in the page (Booking's own gallery JSON: room names + sizes + photos), because the photo
> pop-ups stopped opening in the browser - the gallery viewer is now a fullscreen "cinema" overlay, not
> a `role="dialog"` pop-up, so the old click-and-wait approach found nothing. Verified against a real
> saved page (Garner Hotel Berlin - Schöneberg): the JSON re-finds all 93 known photos (coverage 100%)
> with every room resolving onto its existing workbook category. If a page carries no gallery JSON, the
> tool falls back to clicking the pop-ups (selectors in `bsync\scraper.py` -> `SELECTORS`). The live
> browser run itself (all 126 hotels) is still untested - run one hotel first, as below.

## Setup (PowerShell)

```powershell
cd novum-booking-sync
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Uses your installed Google Chrome. If you don't have it:  python -m playwright install chromium  (+ --channel chromium)
```

## Recommended workflow

**1. Try ONE hotel, visibly, with debug output** (browser window opens; solve Booking's check if it appears -
your solved session is kept in `sync_run\browser-profile`):

```powershell
python sync_booking_images.py --workbook "C:\path\NOVUM_Hospitality_Hotels_with_Booking_Images.xlsx" --only 28 --dry-run --debug
```

Open `sync_run\hotels_report.csv`. A healthy hotel shows **Status OK, Coverage near 100%** (the scraper
re-found the photos you already have) and a small number of New photos. The console line also shows
`gallery N, room popups X/Y`. If something is off, look at `sync_run\debug` (screenshots + page HTML) and fix
the selectors in `bsync\scraper.py` -> `SELECTORS` (one dictionary, commented).

**2. Dry-run everything** (about 1-2 min per hotel -> a few hours; it is **resumable**: finished hotels are
cached in `sync_run\snapshots`, so re-running skips them):

```powershell
python sync_booking_images.py --workbook "C:\path\...xlsx" --dry-run
```

Review `hotels_report.csv` and `new_images.csv`. Nothing has been changed yet.

**3. Apply** (re-uses the cached scrapes, so no second scrape; downloads images, writes the workbook):

```powershell
python sync_booking_images.py --workbook "C:\path\...xlsx"
```

Output: `NOVUM_..._updated.xlsx` next to the original (**the original is never modified**). Images go to
`<folder of the workbook>\image\<hotel>\<room>\` - the same place the workbook's 'Local File'
hyperlinks point (change with `--images-root`). After saving, the tool
re-opens both files and proves no existing cell, hyperlink or comment changed.

## How "new" is decided
Identity = Booking **photo id** (the number in `01_702878002.jpg` and in the URL).
* Room photo is new if that room category does not yet contain that photo id (same photo in another room = new row for that room, as your sheet already does). **Exception for the embedded-gallery path:** Booking's gallery JSON lists rooms by its current assignment, which disagrees with the workbook's historical lightbox filing for some photos that are already filed somewhere for the hotel - those are *not* re-filed under another room. Only a photo id the hotel doesn't hold at all gets new rows (under every room the JSON lists it in). Your original filing stands.
* A gallery photo that belongs to no room is new if the id isn't filed **anywhere** for that hotel. It is filed under `General Images` (change with `--untagged-category "Gallery Only - No Room"`).
* Existing rows/files are never renamed or renumbered; new images continue each group's `Image No.`.
* New folders: `image/<hotel>/<room>` next to the workbook, with `/` -> `-` (your existing rule). Existing groups reuse the folder already in the sheet (so legacy names like `... - 12 m²` keep working). The workbook's 'Local Folder' column still shows the legacy `novum-hotels-images/<hotel>/<room>` value; the file itself is written under `image/`, where the 'Local File' hyperlink points.
* Renamed rooms (same photos, similar name) are mapped onto the existing category and noted in the log instead of creating a duplicate.
* Photos that Booking removed are only counted ("Existing Not On Page"); rows are never deleted.

## What you get in the workbook
* New rows appended at the bottom of **Images - Booking.com**, same styles/hyperlinks, **highlighted light-yellow** (`--no-highlight` to disable). `Image Description` is filled only when Booking provides an informative caption; otherwise blank (your descriptions look hand/AI-written).
* The filter range is extended to cover all rows.
* A new **Update Log** tab (one row per hotel per run: status, coverage, new counts, notes).

## Statuses (hotels_report.csv / Update Log)
| Status | Meaning | Applied? |
|---|---|---|
| OK | trusted scrape | yes |
| LOW_COVERAGE | re-found < 50% of photos you already have (partial load / page changed) | no |
| ROOMS_MISSING | your sheet has room categories but no room photos were scraped | no |
| NO_PHOTOS / NOT_FOUND | page loaded but nothing / not a hotel page | no |
| BLOCKED / ERROR | bot check not solved / exception (see Notes) | no |

Override gates with `--force-low-coverage` / `--allow-missing-rooms`. Three BLOCKED hotels in a row stop the run.
Failed downloads are left out of the workbook and retried next time (`failed_downloads.csv`).

## Other options
`--only 28,45,"Garner Hotel Kiel"` · `--limit 5` · `--refresh` (ignore cache) · `--no-dates` · `--checkin/--checkout` ·
`--delay 6 12` · `--audit-local` (lists sheet rows whose file is missing on disk) · `--no-download` · `--headless` (usually blocked).

By default pages are opened with check-in/out dates ~45 days ahead so the room table is populated.
Keep the pace polite; Booking's terms restrict automated access and the photos are the properties' content.

## Tests
```powershell
python -m unittest tests.test_core tests.test_browser_mock
$env:NOVUM_XLSX="C:\path\...xlsx"; python -m unittest tests.test_e2e_real_workbook   # full pipeline, fake scrape + local image server
```
