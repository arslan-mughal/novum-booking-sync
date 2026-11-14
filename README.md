# NOVUM Booking.com image sync

> **Automatic runs with OneDrive (free scheduler): see `README_CLOUD.md`.** This file covers the checker itself and running it by hand.

Visits every Booking.com hotel link in the **NOVUM-Hotels** tab, finds photos that are **not yet** in
**Images - Booking.com** (hotel gallery photos *and* each room's photos), downloads them into your
`novum-hotels-images` folder using the same naming scheme, and appends them to the workbook.

> **Read this first - what is and isn't verified**
> The comparison, numbering, folder naming, downloading and workbook update are tested end-to-end against
> your real workbook. The **Booking.com browser layer could not be tested against the live site** (it was
> built where booking.com is unreachable); its mechanics are tested on a mock page only. Booking changes its
> markup and uses a bot check, so expect to adjust selectors once - that is what the workflow below is for,
> and the safety gates stop a mis-reading from touching your workbook.

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
`<folder of the workbook>\novum-hotels-images\...` (change with `--images-root`). After saving, the tool
re-opens both files and proves no existing cell, hyperlink or comment changed.

## How "new" is decided
Identity = Booking **photo id** (the number in `01_702878002.jpg` and in the URL).
* Room photo is new if that room category does not yet contain that photo id (same photo in another room = new row for that room, as your sheet already does).
* A gallery photo that belongs to no room is new if the id isn't filed **anywhere** for that hotel. It is filed under `General Images` (change with `--untagged-category "Gallery Only - No Room"`).
* Existing rows/files are never renamed or renumbered; new images continue each group's `Image No.`.
* New folders: `novum-hotels-images/<hotel>/<room>` with `/` -> `-` (your existing rule). Existing groups reuse the folder already in the sheet (so legacy names like `... - 12 m²` keep working).
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
