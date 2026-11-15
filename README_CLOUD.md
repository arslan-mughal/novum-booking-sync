# Running it automatically (OneDrive + free scheduler)

`cloud_sync.py` does one unattended run:

```
OneDrive workbook ──download──▶ check Booking.com (stalest hotels first)
                                   │ new photos
                                   ▼
        OneDrive: <folder>/image/<hotel>/<room>/NN_<id>.jpg   (where the workbook's 'Local File' hyperlinks point)
        OneDrive: workbook replaced (new rows appended + "Update Log" tab)   ← only if nobody edited it meanwhile
        OneDrive: <folder>/sync-reports/<date>/  hotels_report.csv, new_images.csv, sync.log
```

## What is and isn't verified
* **Tested** (against a fake of Microsoft Graph/OneDrive + your real workbook): download → compare → upload images → replace workbook, refresh-token rotation, throttling/retry, big-file upload sessions, "workbook edited meanwhile", leftovers from a half-failed run.
* **Not tested**: the real Microsoft service (I had no account/network access) and the live Booking.com site. Do the dry-run in step 4 before trusting it.

## 1. Where to run it — read this first
Booking.com checks visitors with a bot test. Cloud machines use *datacenter* IP addresses, which are blocked far more often than a home/office connection, and nobody is there to click a "verify" page.

| Option | Cost | Booking.com likely to accept it? |
|---|---|---|
| **GitHub Actions, hosted runner** (this repo's workflow as-is) | Free: unlimited minutes for public repos, 2,000 min/month for private repos; 6 h max per job | **Uncertain / often blocked** (datacenter IP). Cheap to try. |
| **GitHub Actions, self-hosted runner** on a PC or server you control (same workflow, set `runs-on: self-hosted`) | GitHub charges nothing for self-hosted runners today (a fee was announced for 2026, then postponed) | **Much better** (your own IP, persistent browser profile where you can solve a check once). Machine must be on at run time. |
| Any machine + Windows Task Scheduler / cron running `cloud_sync.py` | Free | Same as above; no GitHub needed |
| Free cloud VMs (e.g. Oracle "Always Free") | Free | Datacenter IP again — same risk as hosted |

**Recommendation:** try the hosted runner once with a dry run. If the report says `BLOCKED`, switch to a self-hosted runner/Task Scheduler. Nothing else changes.
The run fails safe: a blocked hotel is never changed, three blocked hotels in a row stop the run, and an all-blocked run exits with an error so GitHub e-mails you.

Notes: in a *public* repo GitHub disables scheduled workflows after 60 days without repository activity — use a private repo (2,000 free minutes is plenty: a full pass is a few hours per month). Never commit secrets; use repository secrets.

## 2. One-time Microsoft setup (free)
Create an app registration at <https://entra.microsoft.com> → *App registrations* → *New registration* (a free account is enough; for a *personal* OneDrive choose "Accounts in any organizational directory and personal Microsoft accounts"). Copy the **Application (client) ID**.

Pick **one** sign-in mode:

| | A) App-only | B) Signed in as you |
|---|---|---|
| Works for | work/school Microsoft 365 only | personal **or** work/school |
| Setup | *API permissions → Microsoft Graph → Application → `Files.ReadWrite.All` → Grant admin consent*; *Certificates & secrets → New client secret* | *Authentication → Allow public client flows = Yes*; *API permissions → Delegated → `Files.ReadWrite`*; then run `python login_device.py --client-id <id> --tenant common` (or `consumers`) and sign in |
| Maintenance | none, until the secret expires (max 24 months) | refresh token is renewed on every run (see below) |
| Caution | the permission covers **every user's files in the tenant** — ask your admin; keep the secret private | limited to your own files |

Shared/other-person folders: set `ONEDRIVE_DRIVE_ID` and, in mode B, `MS_SCOPE=https://graph.microsoft.com/Files.ReadWrite.All offline_access`.

## 3. Settings
Repository → *Settings → Secrets and variables → Actions*:

| Name | Kind | Value |
|---|---|---|
| `MS_CLIENT_ID` | secret | application (client) ID |
| `MS_TENANT_ID` | secret | A: your directory (tenant) ID · B: `common` (work/school) or `consumers` (personal) |
| `MS_CLIENT_SECRET` | secret | mode A only |
| `MS_REFRESH_TOKEN` | secret | mode B only (printed by `login_device.py`) |
| `GH_PAT` | secret, optional | mode B on a hosted runner: fine-grained token with *Secrets: read & write* on this repo, so the workflow can store the renewed refresh token. Not needed on a self-hosted runner/own PC (the token file persists in `sync_run/token.json`). |
| `ONEDRIVE_WORKBOOK_PATH` | variable | e.g. `NOVUM_Hotels_All_Booking_Images/NOVUM_Hospitality_Hotels_with_Booking_Images.xlsx` |
| `ONEDRIVE_IMAGES_FOLDER` | variable, optional | folder that **contains** the `image` folder (default: the workbook's folder) - new photos are uploaded under `<folder>/image/<hotel>/<room>/`, exactly where the workbook's 'Local File' hyperlinks point |
| `ONEDRIVE_USER` | variable | mode A: e-mail of the OneDrive owner |

Put your existing workbook and your existing `image` folder at those OneDrive paths (workbook next to the `image` folder). New photos are uploaded under `<folder>/image/<hotel>/<room>/`, next to the old ones, exactly where the workbook's 'Local File' hyperlinks point.

## 4. First run
Actions → *Booking image sync* → *Run workflow* → keep **dry_run = true**, optionally `only = 28` → open OneDrive `…/sync-reports/<date>/hotels_report.csv`. A healthy hotel shows Status OK and coverage near 100%. If not, see the "Booking.com layer" section of `README.md` (selectors; run it locally with `--debug` to see screenshots). Then run again with dry_run = false, or just wait for the schedule (edit the `cron:` line for weekly/monthly).

## 5. How a run behaves
* **Time budget**: each run scrapes the *stalest* hotels first (state kept in `<folder>/sync-state/state.json`) and stops after 300 min; the rest are done next run. All 126 hotels are covered over a few runs even if one run isn't enough.
* **Order of writes**: images are uploaded first, the workbook last. If a run dies in between, the next run finds those images already in OneDrive and reuses them (no duplicates).
* **Workbook edited while the job ran** (or open and locked): it is *not* overwritten. The updated copy is saved as `sync-reports/<date>/…_updated.xlsx`, the run ends with exit code 3, and the next run starts clean. OneDrive version history also keeps every previous workbook version.
* Nothing is ever deleted; a photo Booking removed is only counted in the report.

Exit codes: `0` ok · `1` verification problem · `2` configuration/sign-in error · `3` workbook conflict (copy saved) · `4` every hotel failed.

## 6. Running on your own Windows PC instead (Task Scheduler)
```powershell
copy cloud.env.example cloud.env      # fill it in
python login_device.py --client-id <id> --tenant common --out sync_run\token.json   # mode B only
python cloud_sync.py --env-file cloud.env --dry-run -- --only 28 --debug
```
Schedule: *Task Scheduler → Create Task → Trigger monthly/weekly → Action:* `python` with arguments `cloud_sync.py --env-file cloud.env --max-minutes 300 -- --challenge-wait 120` and "Start in" = the project folder; tick "Run only when user is logged on" so the browser window can open. As a self-hosted GitHub runner (Windows), replace the workflow's `xvfb-run -a python …` with `python …`.
