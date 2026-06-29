# QuikPick EOD Inventory Sync

Nightly job that reads Gilbarco Passport `StoreClose` / journal XML out of the
BOOutBox, sums the day's walk-in sales per UPC, and deducts them from the
matching products in Supabase. Items at or below their `buffer_threshold` are
auto-hidden from the customer app unless staff set a manual override.

- **Operation #2** of the QuikPick inventory system (catalog scan → EOD sync → dashboard).
- **Pure Python standard library** — no `pip install`. Just Python 3.8+.
- Runs against a **local/UNC folder** (the BOOutBox share) **or** the Passport
  **built-in FTP server**. Same code works on the back office PC now or a cloud cron later.
- Also records per-product daily sales into `sales_daily`, which powers the
  Sales Report (`report.html`) and demand forecasting.

> **Prerequisite:** run `migrations/001_sales_daily.sql` in the Supabase SQL
> Editor before the first sync, so the `sales_daily` table exists for the sync
> to write into.

---

## 1. The Supabase key — use the SERVICE ROLE key, not the anon key

This is a trusted backend job. It must write to `inventory_sync_log`, which
Row Level Security blocks for the public anon key. So it uses the **service role**
key, which bypasses RLS.

Get it from: **Supabase dashboard → Project Settings → API → `service_role` secret**
(the long one labelled *secret*, **not** the `anon`/publishable key).

> ⚠️ The service role key can read/write the whole database. Treat it like a
> password. It goes only in `sync_config.json`, which is **gitignored**. Never
> put it in the browser tools (`index.html` / `dashboard.html`) or commit it.

## 2. Configure

```bash
cp sync_config.example.json sync_config.json
```

Edit `sync_config.json`:

| Field | What to set |
|---|---|
| `supabase_service_key` | Paste the service role key from step 1 |
| `source` | `"local"` (read the BOOutBox folder) or `"ftp"` (Passport FTP server) |
| `local_dir` | UNC path to the BOOutBox, e.g. `\\10.5.48.2\XMLGateway\BOOutBox` |
| `file_pattern` | Which files to process (default `StoreClose*.xml`) |
| `delete_after_processing` | `false` = move to `processed/` (safe default); `true` = delete |

> **Use the UNC path, not the `Z:` drive letter.** Mapped drives are
> per-user-session and disappear when Task Scheduler runs the job under a service
> context. `\\10.5.48.2\XMLGateway\BOOutBox` always resolves.

## 3. Test before you trust it

```bash
# Offline — just parse a file and print the sales it found (no network):
python sync.py --parse sample_StoreClose.xml

# Dry run — fetch catalog + compute deductions, print them, write NOTHING:
python sync.py --dry-run

# Process one specific file (real write):
python sync.py --file "\\10.5.48.2\XMLGateway\BOOutBox\StoreClose.xml"
```

Run a `--dry-run` first on the real PC and eyeball the planned deductions before
the first live run.

## 4. Deploy on the back office PC (Windows Task Scheduler)

1. Install **Python 3** (check "Add python.exe to PATH" during install).
2. Copy this `eod_sync/` folder to the PC (e.g. `C:\QuikPick\eod_sync`).
3. Create `sync_config.json` there with the service key + UNC BOOutBox path.
4. **Task Scheduler → Create Task** (not "Basic Task"):
   - **General:** "Run whether user is logged on or not." Run it as a user that
     has access to the `\\10.5.48.2` share (a bare SYSTEM account has no network
     identity and can't reach the share).
   - **Triggers:** Daily at **11:58 PM**.
   - **Actions:** Start a program →
     - Program: `python` (or full path to `python.exe`)
     - Arguments: `sync.py`
     - Start in: `C:\QuikPick\eod_sync`
   - **Conditions:** tick **"Wake the computer to run this task"** if the PC
     ever sleeps. The PC must be on at 11:58 PM.
5. Logs are written to `eod_sync/logs/sync_YYYYMMDD.log` (Task Scheduler hides
   stdout, so check the log file to confirm a run).

## How it behaves

- **Idempotent per business date.** Before deducting, it checks
  `inventory_sync_log` for a successful run on that file's date. If found, the
  file is archived and **not** deducted again — so a double-fire, a manual rerun,
  or a re-appearing file won't double-count.
- **Catch-up.** It processes every matching file in the BOOutBox, so a night the
  PC was off is picked up the next run (each file carries its own date).
- **Refunds** net against sales within a file; a net-negative item is floored at
  0 (never silently adds phantom stock).
- **UPC matching** is digits-only, with a leading-zero-stripped fallback to
  bridge UPC-A (12) vs EAN-13 (13) differences.
- **Partial failure is safe-by-default.** If any single product update fails, the
  run is logged as `partial` and the file is **left in place** (not archived).
  Review before rerunning — a blind rerun would re-deduct the items that already
  succeeded. (For a 10-item pilot this is rare; revisit if it ever happens.)

## FTP alternative

Passport runs a built-in FTP server (v6+), no setup required, default user
`GilbarcoFTP` / `XMLUser4FTP`, root `C:\Passport\XMLGateway`. To use it instead
of the share, set `"source": "ftp"` and fill in the `ftp` block. Note that
Passport FTP is **plaintext on the store LAN** — keep it on the LAN, never expose
port 21 to the internet.

## Related

- This job is the only thing that should use the service role key. The browser
  tools use the public anon key. Tightening the anon RLS policies (so the public
  key can't write arbitrary products) is tracked separately as operation #3.
