#!/usr/bin/env python3
"""
QuikPick EOD inventory sync.

Reads Gilbarco Passport StoreClose / journal XML out of the BOOutBox, sums the
day's walk-in sales per UPC, and deducts them from the matching products in
Supabase. Items at or below their buffer_threshold are auto-hidden from the
customer app (is_active = false) unless staff have set a manual override.

Design notes
------------
* Pure standard library. No `pip install` needed — just Python 3.8+.
* Source is either a local/UNC folder (the BOOutBox share) OR the Passport
  built-in FTP server (user GilbarcoFTP). Pick with `source` in the config.
* Idempotent per business date: if a successful sync row already exists in
  inventory_sync_log for a file's date, the file is archived and skipped rather
  than deducted again. This protects against scheduler double-fires and reruns.
* Uses the Supabase SERVICE ROLE key (not the anon key). It is a trusted backend
  job and must write to inventory_sync_log, which RLS blocks for the anon role.
  Keep that key out of git — it lives in sync_config.json (gitignored).

Usage
-----
  python sync.py                 # process BOOutBox per the config (real run)
  python sync.py --dry-run       # fetch + compute, print planned changes, write nothing
  python sync.py --parse FILE    # offline: just parse a file and print the sales it found
  python sync.py --file FILE     # process one specific local file
  python sync.py --source ftp    # override the configured source
"""

import argparse
import ftplib
import glob
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


# ── logging ──────────────────────────────────────────────────────────────────
class Log:
    def __init__(self, logdir):
        self.fh = None
        try:
            logdir.mkdir(parents=True, exist_ok=True)
            self.fh = open(logdir / f"sync_{date.today():%Y%m%d}.log", "a", encoding="utf-8")
        except OSError:
            pass  # logging to file is best-effort; stdout still works

    def __call__(self, msg, level="INFO"):
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} [{level}] {msg}"
        print(line, flush=True)
        if self.fh:
            self.fh.write(line + "\n")
            self.fh.flush()


# ── config ───────────────────────────────────────────────────────────────────
def load_config(path):
    if not path.exists():
        sys.exit(
            f"Config not found: {path}\n"
            f"Copy sync_config.example.json to sync_config.json and fill it in."
        )
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    # Allow env vars to override secrets (handy for cloud/CI deploys).
    cfg["supabase_url"] = os.environ.get("SUPABASE_URL", cfg.get("supabase_url", ""))
    cfg["supabase_service_key"] = os.environ.get(
        "SUPABASE_SERVICE_KEY", cfg.get("supabase_service_key", "")
    )
    return cfg


# ── Supabase REST (urllib, stdlib only) ──────────────────────────────────────
class Supabase:
    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1"
        self.key = key

    def _request(self, method, path, params=None, body=None, prefer=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("apikey", self.key)
        req.add_header("Authorization", "Bearer " + self.key)
        req.add_header("Content-Type", "application/json")
        if prefer:
            req.add_header("Prefer", prefer)
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode()
            return json.loads(text) if text else []

    def get(self, table, params):
        return self._request("GET", "/" + table, params=params)

    def patch(self, table, params, body):
        return self._request("PATCH", "/" + table, params=params, body=body,
                             prefer="return=representation")

    def insert(self, table, body):
        return self._request("POST", "/" + table, body=body,
                             prefer="return=representation")

    def upsert(self, table, rows, on_conflict):
        # Bulk upsert on a unique constraint (idempotent reruns).
        return self._request("POST", "/" + table, params={"on_conflict": on_conflict},
                             body=rows, prefer="resolution=merge-duplicates,return=minimal")


# ── XML parsing (namespace-agnostic) ─────────────────────────────────────────
def _local(tag):
    """Strip an XML namespace: '{ns}POSCode' -> 'POSCode'."""
    return tag.rsplit("}", 1)[-1]


def _digits(s):
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _find_text(elem, name):
    for node in elem.iter():
        if _local(node.tag) == name and node.text and node.text.strip():
            return node.text.strip()
    return None


def parse_store_close(xml_bytes, fallback_date):
    """
    Return (sales_by_upc, business_date, warnings).

    Confirmed Passport structure (namespace ignored):
      JournalReport > SaleEvent > TransactionDetailGroup > TransactionLine >
      ItemLine > ItemCode > POSCode            -> UPC
                          > POSCodeModifier[@name="pc"] -> quantity sold
    Quantities are summed per UPC across every ItemLine in the file. Negative
    quantities (refunds/voids) net against sales; net is floored at 0.
    """
    warnings = []
    root = ET.fromstring(xml_bytes)

    # business date: first date-ish element we can parse, else the fallback.
    business_date = None
    for node in root.iter():
        name = _local(node.tag)
        if "date" in name.lower() and node.text:
            t = node.text.strip()[:10]
            try:
                business_date = datetime.strptime(t, "%Y-%m-%d").date().isoformat()
                break
            except ValueError:
                continue
    if not business_date:
        business_date = fallback_date.isoformat()

    sales = {}
    item_lines = [n for n in root.iter() if _local(n.tag) == "ItemLine"]
    for line in item_lines:
        upc_raw = _find_text(line, "POSCode")
        # the quantity lives in POSCodeModifier with attribute name="pc"
        qty_raw = None
        for node in line.iter():
            if _local(node.tag) == "POSCodeModifier" and node.attrib.get("name") == "pc":
                qty_raw = (node.text or "").strip()
                break

        upc = _digits(upc_raw)
        if not upc:
            warnings.append("ItemLine with no POSCode/UPC — skipped")
            continue
        try:
            qty = int(round(float(qty_raw)))
        except (TypeError, ValueError):
            warnings.append(f"UPC {upc}: unreadable quantity {qty_raw!r}, assuming 1")
            qty = 1
        sales[upc] = sales.get(upc, 0) + qty

    # floor net sales at 0 (so a net-refund line never silently adds phantom stock)
    sales = {u: q for u, q in sales.items() if q > 0}
    return sales, business_date, warnings


# ── file sources ─────────────────────────────────────────────────────────────
class LocalSource:
    def __init__(self, directory, pattern, archive_dir, delete_after, log):
        self.dir = Path(directory)
        self.pattern = pattern
        self.archive_dir = archive_dir
        self.delete_after = delete_after
        self.log = log

    def list_files(self):
        if not self.dir.exists():
            self.log(f"BOOutBox path not reachable: {self.dir}", "ERROR")
            return []
        out = []
        for p in sorted(self.dir.glob(self.pattern)):
            if p.is_file():
                out.append((p.name, p.read_bytes(), str(p)))
        return out

    def finalize(self, ref):
        p = Path(ref)
        if self.delete_after:
            p.unlink(missing_ok=True)
            self.log(f"deleted {p.name}")
        else:
            dest_dir = self.dir / self.archive_dir
            dest_dir.mkdir(exist_ok=True)
            p.replace(dest_dir / p.name)
            self.log(f"archived {p.name} -> {self.archive_dir}/")

    def close(self):
        pass


class FtpSource:
    def __init__(self, conf, pattern, archive_dir, delete_after, log):
        self.conf = conf
        self.pattern = pattern
        self.archive_dir = archive_dir
        self.delete_after = delete_after
        self.log = log
        self.ftp = ftplib.FTP(timeout=30)

    def _connect(self):
        c = self.conf
        self.ftp.connect(c["host"], c.get("port", 21))
        self.ftp.login(c.get("user", "GilbarcoFTP"), c.get("password", "XMLUser4FTP"))
        if c.get("dir"):
            self.ftp.cwd(c["dir"])

    def list_files(self):
        import fnmatch
        self._connect()
        names = [n for n in self.ftp.nlst() if fnmatch.fnmatch(n, self.pattern)]
        out = []
        for name in sorted(names):
            buf = io.BytesIO()
            self.ftp.retrbinary("RETR " + name, buf.write)
            out.append((name, buf.getvalue(), name))
        return out

    def finalize(self, ref):
        if self.delete_after:
            self.ftp.delete(ref)
            self.log(f"deleted {ref} (ftp)")
        else:
            try:
                self.ftp.mkd(self.archive_dir)
            except ftplib.error_perm:
                pass  # already exists
            self.ftp.rename(ref, f"{self.archive_dir}/{ref}")
            self.log(f"archived {ref} -> {self.archive_dir}/ (ftp)")

    def close(self):
        try:
            self.ftp.quit()
        except Exception:
            pass


def make_source(cfg, log):
    pattern = cfg.get("file_pattern", "StoreClose*.xml")
    archive = cfg.get("archive_dir", "processed")
    delete_after = cfg.get("delete_after_processing", False)
    if cfg["source"] == "ftp":
        return FtpSource(cfg["ftp"], pattern, archive, delete_after, log)
    return LocalSource(cfg["local_dir"], pattern, archive, delete_after, log)


# ── core ─────────────────────────────────────────────────────────────────────
def already_synced(supa, station_id, business_date):
    rows = supa.get("inventory_sync_log", {
        "select": "id",
        "station_id": f"eq.{station_id}",
        "sync_date": f"eq.{business_date}",
        "sync_status": "eq.success",
        "limit": 1,
    })
    return bool(rows)


def process_file(name, data, ref, supa, station_id, products_by_upc, source, dry_run, log):
    fallback = datetime.now().date()
    sales, business_date, warnings = parse_store_close(data, fallback)
    for w in warnings:
        log(w, "WARN")
    log(f"{name}: business_date={business_date}, {len(sales)} distinct UPCs sold")

    if not sales:
        log(f"{name}: no sales lines found — leaving file in place for review", "WARN")
        return

    if not dry_run and already_synced(supa, station_id, business_date):
        log(f"{name}: {business_date} already synced — archiving without re-deducting")
        source.finalize(ref)
        return

    matched = flagged_low = 0
    errors = []
    history_rows = []  # one sales_daily row per matched product
    now = datetime.now(timezone.utc).isoformat()

    for upc, sold in sales.items():
        p = products_by_upc.get(upc)
        if not p:
            continue  # UPC we don't carry (tobacco, fuel, etc.) — counted as unmatched
        matched += 1
        old = p["stock_quantity"]
        new = max(0, old - sold)
        active = p["is_active"] if p.get("manual_override") else (new > p["buffer_threshold"])
        crossed_low = old > p["buffer_threshold"] >= new
        if crossed_low:
            flagged_low += 1

        # Record the day's sale (history is set-based, so a rerun overwrites
        # rather than double-counts — safer than the stock decrement).
        history_rows.append({
            "station_id": station_id,
            "product_id": p["id"],
            "upc": upc,
            "product_name": p["name"],
            "sale_date": business_date,
            "quantity_sold": sold,
            "unit_price": p.get("price", 0),
        })

        verb = "would deduct" if dry_run else "deduct"
        log(f"  {p['name']}: {verb} {sold}  ({old} -> {new})"
            + (f"  [hide: low stock]" if crossed_low and not active else "")
            + ("  [manual override kept]" if p.get("manual_override") else ""))

        if not dry_run:
            try:
                supa.patch("products", {"id": f"eq.{p['id']}"},
                          {"stock_quantity": new, "is_active": active, "last_synced_at": now})
                p["stock_quantity"] = new  # keep local copy current within this run
                p["is_active"] = active
            except Exception as e:
                errors.append(f"{p['name']} ({upc}): {e}")
                log(f"  ! update failed for {p['name']}: {e}", "ERROR")

    # Persist per-product sales history (powers the Sales Report + forecasting).
    if history_rows:
        if dry_run:
            log(f"  would record {len(history_rows)} sales_daily rows for {business_date}")
        else:
            try:
                supa.upsert("sales_daily", history_rows, "product_id,sale_date")
            except Exception as e:
                errors.append(f"sales_daily upsert: {e}")
                log(f"  ! sales_daily write failed: {e}", "ERROR")

    processed = len(sales)
    unmatched = processed - matched
    log(f"{name}: matched {matched}, unmatched {unmatched}, flagged_low {flagged_low}"
        + (f", {len(errors)} errors" if errors else ""))

    if dry_run:
        log(f"{name}: DRY RUN — no writes, file left in place")
        return

    status = "success" if not errors else "partial"
    try:
        supa.insert("inventory_sync_log", {
            "station_id": station_id,
            "sync_date": business_date,
            "items_processed": processed,
            "items_matched": matched,
            "items_unmatched": unmatched,
            "items_flagged_low": flagged_low,
            "sync_status": status,
            "error_log": "\n".join(errors) if errors else None,
        })
    except Exception as e:
        log(f"could not write inventory_sync_log: {e}", "ERROR")
        status = "partial"

    if status == "success":
        source.finalize(ref)
    else:
        log(f"{name}: partial/failed — NOT archiving; review before rerun "
            f"(rerunning would re-deduct items that already succeeded)", "WARN")


# ── entrypoint ───────────────────────────────────────────────────────────────
def parse_args():
    ap = argparse.ArgumentParser(description="QuikPick EOD inventory sync")
    ap.add_argument("--config", default=str(SCRIPT_DIR / "sync_config.json"))
    ap.add_argument("--dry-run", action="store_true", help="compute + print, write nothing")
    ap.add_argument("--parse", metavar="FILE", help="offline: parse a file, print sales, exit")
    ap.add_argument("--file", metavar="FILE", help="process one specific local file")
    ap.add_argument("--source", choices=["local", "ftp"], help="override configured source")
    return ap.parse_args()


def main():
    args = parse_args()

    # Offline parse mode needs no config or network.
    if args.parse:
        sales, bdate, warnings = parse_store_close(Path(args.parse).read_bytes(), datetime.now().date())
        print(f"business_date: {bdate}")
        for w in warnings:
            print(f"  WARN: {w}")
        print(f"{len(sales)} distinct UPCs sold:")
        for upc, qty in sorted(sales.items()):
            print(f"  {upc}: {qty}")
        return

    cfg = load_config(Path(args.config))
    if args.source:
        cfg["source"] = args.source
    log = Log(SCRIPT_DIR / "logs")
    started = time.time()

    if not cfg.get("supabase_service_key") or "PASTE" in cfg["supabase_service_key"]:
        sys.exit("supabase_service_key is not set in the config. See README.")

    supa = Supabase(cfg["supabase_url"], cfg["supabase_service_key"])
    store_id = cfg.get("passport_store_id", 110)

    stations = supa.get("stations", {"select": "id,name", "passport_store_id": f"eq.{store_id}", "limit": 1})
    if not stations:
        sys.exit(f"No station found for passport_store_id={store_id}")
    station_id = stations[0]["id"]
    log(f"station {stations[0]['name']} ({station_id})")

    products = supa.get("products", {
        "select": "id,upc,name,price,stock_quantity,buffer_threshold,is_active,manual_override",
        "station_id": f"eq.{station_id}",
    })
    # Index by normalized UPC, plus a leading-zero-stripped key to bridge the
    # common UPC-A (12) vs EAN-13 (13, leading 0) mismatch.
    products_by_upc = {}
    for p in products:
        key = _digits(p.get("upc"))
        if key:
            products_by_upc.setdefault(key, p)
            products_by_upc.setdefault(key.lstrip("0"), p)
    log(f"{len(products)} products in catalog ({'DRY RUN' if args.dry_run else 'live'})")

    source = make_source(cfg, log)
    try:
        if args.file:
            data = Path(args.file).read_bytes()
            files = [(Path(args.file).name, data, str(args.file))]
            source = LocalSource(str(Path(args.file).parent), "*", cfg.get("archive_dir", "processed"),
                                cfg.get("delete_after_processing", False), log)
        else:
            files = source.list_files()

        if not files:
            log("no StoreClose files to process")
        for name, data, ref in files:
            process_file(name, data, ref, supa, station_id, products_by_upc, source, args.dry_run, log)
    finally:
        source.close()

    log(f"done in {time.time() - started:.1f}s")


if __name__ == "__main__":
    main()
