"""Everything that touches the .xlsx file.

Design rules (derived from inspecting the real workbook):
  * columns are found by HEADER NAME, not position;
  * the hotel's Booking.com URL is the join key between the two tabs (hotel names differ in places,
    e.g. '-' vs en-dash), and names / folders for existing hotels are always taken from the images tab;
  * new rows are APPENDED, copying the cell styles of the last existing row; no existing cell is changed;
  * the file is written to a new path - the original is never overwritten.
"""
from __future__ import annotations

from copy import copy
from typing import Iterable
from urllib.parse import quote

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from .model import Group, HotelDiff, HotelIndex, HotelRow, NewImage
from .util import sanitize_segment

HOTELS_SHEET = "NOVUM-Hotels"
IMAGES_SHEET = "Images - Booking.com"
LOG_SHEET = "Update Log"

IMG_COLS = ["Sr.", "Hotel Name", "Booking.com Hotel Page", "Room Category", "Image No.",
            "Image Description", "Booking.com Image URL", "Local Folder", "Local File"]
DEFAULT_ROOT = "novum-hotels-images"
NEW_ROW_FILL = PatternFill("solid", start_color="FFF2CC", end_color="FFF2CC")

LOG_HEADERS = ["Run", "Sr.", "Hotel Name", "Booking.com Hotel Page", "Status", "Existing Images",
               "Found On Page", "Existing Re-found", "Coverage", "Existing Not On Page",
               "New Found", "New In Rooms", "New Untagged", "Added To Workbook", "Download Failed",
               "New Room Categories", "Notes"]


class WorkbookError(RuntimeError):
    pass


def open_workbook(path: str) -> Workbook:
    return load_workbook(path)


def _header_map(ws, needed: Iterable[str], sheet_name: str) -> dict:
    hdr = {}
    for idx, cell in enumerate(ws[1], start=1):
        if cell.value is not None:
            hdr[str(cell.value).strip()] = idx
    missing = [h for h in needed if h not in hdr]
    if missing:
        raise WorkbookError(f"Sheet '{sheet_name}' is missing column(s): {', '.join(missing)}. "
                            f"Found: {', '.join(hdr)}")
    return hdr


# ------------------------------------------------------------------ reading
def read_hotels(wb: Workbook) -> list:
    if HOTELS_SHEET not in wb.sheetnames:
        raise WorkbookError(f"Workbook has no '{HOTELS_SHEET}' sheet (found: {wb.sheetnames})")
    ws = wb[HOTELS_SHEET]
    h = _header_map(ws, ["Sr.", "Hotel Name", "Booking.com"], HOTELS_SHEET)
    out, seen = [], set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        url = row[h["Booking.com"] - 1]
        if not isinstance(url, str):
            continue
        url = url.strip()
        if not url.lower().startswith("http") or "booking.com" not in url.lower() or url in seen:
            continue
        seen.add(url)
        sr = row[h["Sr."] - 1]
        out.append(HotelRow(sr=int(sr) if sr is not None else 0,
                            name=str(row[h["Hotel Name"] - 1] or "").strip(), page_url=url))
    return out


def read_index(wb: Workbook, hotels: list) -> dict:
    """page_url -> HotelIndex built from the existing image rows."""
    if IMAGES_SHEET not in wb.sheetnames:
        raise WorkbookError(f"Workbook has no '{IMAGES_SHEET}' sheet (found: {wb.sheetnames})")
    ws = wb[IMAGES_SHEET]
    h = _header_map(ws, IMG_COLS, IMAGES_SHEET)
    c = {k: v - 1 for k, v in h.items()}
    index: dict = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        url = row[c["Booking.com Hotel Page"]]
        cat = row[c["Room Category"]]
        if not url or not cat:
            continue
        url = str(url).strip()
        hi = index.get(url)
        folder = str(row[c["Local Folder"]] or "")
        if hi is None:
            parts = folder.split("/")
            hi = HotelIndex(sr=int(row[c["Sr."]] or 0), name=str(row[c["Hotel Name"]] or ""), page_url=url,
                            hotel_folder="/".join(parts[:2]) if len(parts) >= 2 else folder)
            index[url] = hi
        g = hi.groups.get(cat)
        if g is None:
            g = hi.groups[cat] = Group(category=str(cat), folder=folder)
        g.rows += 1
        try:
            g.max_no = max(g.max_no, int(row[c["Image No."]] or 0))
        except (TypeError, ValueError):
            pass
        fname = str(row[c["Local File"]] or "")
        pid = fname.split("_", 1)[1].rsplit(".", 1)[0] if "_" in fname else ""
        if not pid.isdigit():                       # fall back to the URL
            from .util import photo_id
            pid = photo_id(str(row[c["Booking.com Image URL"]] or "")) or ""
        if pid:
            g.pids.add(pid)

    root = detect_root(index)
    for hr in hotels:                               # hotels with no rows yet get an empty index
        if hr.page_url not in index:
            index[hr.page_url] = HotelIndex(sr=hr.sr, name=hr.name, page_url=hr.page_url,
                                            hotel_folder=f"{root}/{sanitize_segment(hr.name)}")
    return index


def detect_root(index: dict) -> str:
    for hi in index.values():
        if hi.hotel_folder:
            return hi.hotel_folder.split("/")[0]
    return DEFAULT_ROOT


def owner_map(index: dict) -> dict:
    """photo id -> page_url of the hotel that owns it (ids are unique per hotel in the workbook)."""
    out: dict = {}
    for url, hi in index.items():
        for pid in hi.all_pids():
            out.setdefault(pid, url)
    return out


# ------------------------------------------------------------------ writing
def _last_data_row(ws) -> int:
    r = ws.max_row
    while r > 1 and ws.cell(r, 2).value in (None, ""):
        r -= 1
    return r


def local_file_link(folder: str, file_name: str) -> str:
    """Relative link for a new row's 'Local File' cell, matching the existing rows exactly:
    ``image/<hotel>/<room>/<file>`` with every segment URL-encoded (spaces, ö, ², ...).
    The workbook sits next to the extracted ``image`` folder, so the link is relative to the
    workbook - it is NOT the 'Local Folder' value (which starts with 'novum-hotels-images/')."""
    parts = [p for p in (folder or "").split("/") if p]
    if parts and parts[0] == DEFAULT_ROOT:
        parts = parts[1:]
    parts.append(file_name)
    return "image/" + "/".join(quote(p, safe="") for p in parts)


def append_new_rows(wb: Workbook, items: list, highlight: bool = True) -> int:
    """Append rows for NewImage items (file_name / url_used must be set). Returns rows written."""
    if not items:
        return 0
    ws = wb[IMAGES_SHEET]
    h = _header_map(ws, IMG_COLS, IMAGES_SHEET)
    last = _last_data_row(ws)
    styles = {name: copy(ws.cell(last, col)._style) for name, col in h.items()}
    r = last
    for it in items:
        r += 1
        values = {
            "Sr.": it.sr, "Hotel Name": it.hotel_name, "Booking.com Hotel Page": it.page_url,
            "Room Category": it.category, "Image No.": it.no, "Image Description": it.caption or None,
            "Booking.com Image URL": it.url_used or it.url, "Local Folder": it.folder,
            "Local File": it.file_name,
        }
        for name, col in h.items():
            cell = ws.cell(r, col, values.get(name))
            cell._style = copy(styles[name])
            if highlight and name in IMG_COLS:
                cell.fill = NEW_ROW_FILL
        # the existing rows link the hotel page, the image URL and the local file - keep that behaviour
        ws.cell(r, h["Booking.com Hotel Page"]).hyperlink = it.page_url
        ws.cell(r, h["Booking.com Image URL"]).hyperlink = it.url_used or it.url
        ws.cell(r, h["Local File"]).hyperlink = local_file_link(it.folder, it.file_name)
    if ws.auto_filter and ws.auto_filter.ref:        # the original filter stops mid-table; cover all rows
        ws.auto_filter.ref = f"A1:{ws.cell(1, ws.max_column).column_letter}{r}"
    return r - last


def append_log(wb: Workbook, run_label: str, diffs: list, applied: dict, failed: dict) -> None:
    if LOG_SHEET in wb.sheetnames:
        ws = wb[LOG_SHEET]
    else:
        ws = wb.create_sheet(LOG_SHEET)
        ws.append(LOG_HEADERS)
        for cell in ws[1]:
            cell.font = Font(name="Calibri", size=11, bold=True)
            cell.fill = PatternFill("solid", start_color="D9D9D9", end_color="D9D9D9")
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.freeze_panes = "A2"
        for col, w in zip("ABCDEFGHIJKLMNOPQ", [17, 6, 44, 52, 15, 10, 10, 11, 10, 12, 9, 10, 10, 11, 10, 11, 90]):
            ws.column_dimensions[col].width = w
    base = Font(name="Calibri", size=11)
    for d in diffs:
        ws.append([
            run_label, d.sr, d.name, d.page_url, d.status, d.existing_n, d.scraped_n, d.refound_n,
            d.coverage if d.scraped_n else None, d.missing_n if d.scraped_n else None,
            len(d.new_images), d.new_in_rooms, d.new_untagged,
            applied.get(d.page_url, 0), failed.get(d.page_url, 0),
            "; ".join(d.new_categories) or None, " | ".join(d.notes) or None,
        ])
        row = ws.max_row
        for cell in ws[row]:
            cell.font = base
        ws.cell(row, 9).number_format = "0.0%"
    ws.auto_filter.ref = f"A1:{ws.cell(1, ws.max_column).column_letter}{ws.max_row}"


# ------------------------------------------------------------------ verification
def verify(src_path: str, out_path: str, expected_new: int) -> list:
    """Re-open both files and prove that nothing existing changed. Returns a list of problems (empty = ok)."""
    problems: list = []
    a, b = load_workbook(src_path), load_workbook(out_path)
    for name in a.sheetnames:
        if name not in b.sheetnames:
            problems.append(f"sheet '{name}' missing in output")
    wa, wb_ = a[IMAGES_SHEET], b[IMAGES_SHEET]
    n_old = _last_data_row(wa)
    n_new = _last_data_row(wb_)
    if n_new - n_old != expected_new:
        problems.append(f"expected {expected_new} new rows, output has {n_new - n_old}")
    ncol = len(IMG_COLS)
    ha = _header_map(wa, IMG_COLS, IMAGES_SHEET)
    link_cols = (ha["Booking.com Hotel Page"], ha["Booking.com Image URL"], ha["Local File"])
    for r in range(1, n_old + 1):
        for col in range(1, ncol + 1):
            ca, cb = wa.cell(r, col), wb_.cell(r, col)
            if ca.value != cb.value:
                problems.append(f"{IMAGES_SHEET}!{ca.coordinate} changed: {ca.value!r} -> {cb.value!r}")
            if col in link_cols:
                la = ca.hyperlink.target if ca.hyperlink else None
                lb = cb.hyperlink.target if cb.hyperlink else None
                if la != lb:
                    problems.append(f"{IMAGES_SHEET}!{ca.coordinate} hyperlink changed")
            if len(problems) > 20:
                return problems + ["... (stopping after 20 problems)"]
    for r in range(n_old + 1, n_new + 1):
        for col in link_cols:
            if not wb_.cell(r, col).hyperlink:
                problems.append(f"new row {r} column {col} has no hyperlink")
    if HOTELS_SHEET in a.sheetnames:
        ha_, hb_ = a[HOTELS_SHEET], b[HOTELS_SHEET]
        if ha_.max_row != hb_.max_row or ha_.max_column != hb_.max_column:
            problems.append("hotels sheet dimensions changed")
        for row_a, row_b in zip(ha_.iter_rows(), hb_.iter_rows()):
            for ca, cb in zip(row_a, row_b):
                if ca.value != cb.value:
                    problems.append(f"{HOTELS_SHEET}!{ca.coordinate} changed")
                ta = ca.comment.text if ca.comment else None
                tb = cb.comment.text if cb.comment else None
                if ta != tb:
                    problems.append(f"{HOTELS_SHEET}!{ca.coordinate} comment changed")
    return problems
