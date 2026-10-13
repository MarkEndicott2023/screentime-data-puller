#!/usr/bin/env python3
"""
Pull Screen Time app usage (iPhone + Mac) out of the Mac's Knowledge store.

Apple has no public Screen Time API. With Screen Time -> "Share Across Devices"
on, every device in the iCloud account syncs its app-usage intervals into the
Mac's knowledgeC.db, one row per foreground session, tagged with the device it
came from. This script copies that database and reads the copy.

Store (macOS 26, TCC-protected):
  ~/Library/Application Support/Knowledge/knowledgeC.db  (+ -wal, -shm)

The process needs Full Disk Access or the copy step fails with EPERM.
System Settings -> Privacy & Security -> Full Disk Access -> add the app that
runs this script (your terminal, VS Code, or Python.app for the dashboard
button). See README.md.

Usage
  python3 screentime_pull.py check                 # readable? which streams, devices, date span
  python3 screentime_pull.py devices               # device ids with row counts; name them in devices.json
  python3 screentime_pull.py pull --days 30
  python3 screentime_pull.py pull --from 2026-09-01 --to 2026-09-28
  python3 screentime_pull.py pull --days 30 --streams /app/usage,/app/webUsage

Output
  raw/{from}_{to}.json     untouched query rows (ground truth for this pull)
  csv/app_usage.csv        one row per usage interval, deduped by uuid, newer pull wins
  csv/daily.csv            seconds per day x device x app, rebuilt from app_usage.csv

Schema notes (column names are checked at runtime; anything missing is skipped):
  ZOBJECT.ZSTREAMNAME   '/app/usage' = Screen Time app sessions; '/app/webUsage' = Safari domains
  ZOBJECT.ZVALUESTRING  bundle id (or domain for webUsage)
  ZOBJECT.ZSTARTDATE / ZENDDATE   seconds since 2001-01-01 UTC (Core Data epoch)
  ZOBJECT.ZSECONDSFROMGMT         UTC offset the device was in
  ZSOURCE.ZDEVICEID     which device produced the row; NULL is expected for this Mac
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW_DIR = HERE / "raw"
CSV_DIR = HERE / "csv"
DEVICE_NAMES = HERE / "devices.json"          # {"<ZDEVICEID>": "iPhone 15 Plus", "": "MacBook"}

import os
STORE = Path(os.environ.get("KNOWLEDGE_DB") or (Path.home() / "Library" / "Application Support" / "Knowledge" / "knowledgeC.db"))
STORE_DIR = STORE.parent                       # KNOWLEDGE_DB=/path/to/copy.db for tests
CORE_DATA_EPOCH = 978307200                    # 2001-01-01T00:00:00Z as unix seconds
DEFAULT_STREAMS = ["/app/usage"]

FDA_HELP = (
    "Cannot read the Knowledge store: Full Disk Access is missing for the app running this script.\n"
    "  System Settings -> Privacy & Security -> Full Disk Access -> (+)\n"
    "  Manual runs:      add the terminal app you launched this from (or Visual Studio Code).\n"
    "  Dashboard button: add /Library/Frameworks/Python.framework/Versions/3.14/Resources/Python.app\n"
    "Then restart that app and re-run:  python3 screentime_pull.py check"
)


# --------------------------------------------------------------------------- store access
def snapshot(tmp: Path) -> Path:
    """Copy db + wal + shm so the read sees committed-but-not-checkpointed rows
    and never touches the live file that knowledged holds open."""
    if not STORE_DIR.exists():
        sys.exit(f"Store directory not found: {STORE_DIR}")
    try:
        for suffix in ("", "-wal", "-shm"):
            src = STORE.with_name(STORE.name + suffix)
            if src.exists():
                shutil.copy2(src, tmp / src.name)
    except PermissionError:
        sys.exit(FDA_HELP)
    dst = tmp / STORE.name
    if not dst.exists():
        sys.exit(f"{STORE} not found. Is Screen Time on in System Settings?")
    return dst


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}


def cd_to_unix(v) -> float | None:
    return None if v is None else float(v) + CORE_DATA_EPOCH


def to_local_iso(unix: float | None, offset_s) -> str:
    if unix is None:
        return ""
    tz = dt.timezone(dt.timedelta(seconds=int(offset_s))) if offset_s is not None else None
    try:
        t = dt.datetime.fromtimestamp(unix, tz=dt.timezone.utc)
        return (t.astimezone(tz) if tz else t.astimezone()).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return f"?{unix:.0f}"                 # sentinel/garbage timestamp (seen in sync bookkeeping streams)


def device_names() -> dict[str, str]:
    if DEVICE_NAMES.exists():
        return json.loads(DEVICE_NAMES.read_text())
    return {}


# --------------------------------------------------------------------------- queries
def usage_rows(con: sqlite3.Connection, streams: list[str], start: dt.date, end: dt.date) -> list[dict]:
    zo, zs = columns(con, "ZOBJECT"), columns(con, "ZSOURCE")
    need = {"ZSTREAMNAME", "ZVALUESTRING", "ZSTARTDATE", "ZENDDATE"}
    missing = need - zo
    if missing:
        sys.exit(f"ZOBJECT is missing {sorted(missing)}; the schema changed. Run `check` and adjust.")
    sel = ["o.Z_PK AS pk", "o.ZSTREAMNAME AS stream", "o.ZVALUESTRING AS value",
           "o.ZSTARTDATE AS start_cd", "o.ZENDDATE AS end_cd"]
    sel.append("o.ZUUID AS uuid" if "ZUUID" in zo else "NULL AS uuid")
    sel.append("o.ZSECONDSFROMGMT AS gmt_off" if "ZSECONDSFROMGMT" in zo else "NULL AS gmt_off")
    sel.append("o.ZCREATIONDATE AS created_cd" if "ZCREATIONDATE" in zo else "NULL AS created_cd")
    join = ""
    if "ZSOURCE" in zo and zs:
        join = "LEFT JOIN ZSOURCE s ON s.Z_PK = o.ZSOURCE"
        sel.append("s.ZDEVICEID AS device_id" if "ZDEVICEID" in zs else "NULL AS device_id")
        sel.append("s.ZBUNDLEID AS source_bundle" if "ZBUNDLEID" in zs else "NULL AS source_bundle")
    else:
        sel += ["NULL AS device_id", "NULL AS source_bundle"]
    lo = dt.datetime.combine(start, dt.time()).astimezone().timestamp() - CORE_DATA_EPOCH
    hi = dt.datetime.combine(end, dt.time()).astimezone().timestamp() - CORE_DATA_EPOCH
    marks = ",".join("?" * len(streams))
    sql = (f"SELECT {', '.join(sel)} FROM ZOBJECT o {join} "
           f"WHERE o.ZSTREAMNAME IN ({marks}) AND o.ZSTARTDATE >= ? AND o.ZSTARTDATE < ? "
           f"ORDER BY o.ZSTARTDATE")
    return [dict(r) for r in con.execute(sql, [*streams, lo, hi])]


def stream_summary(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(con.execute(
        "SELECT ZSTREAMNAME AS stream, COUNT(*) AS n, MIN(ZSTARTDATE) AS lo, MAX(ZSTARTDATE) AS hi "
        "FROM ZOBJECT GROUP BY ZSTREAMNAME ORDER BY n DESC"))


def device_summary(con: sqlite3.Connection, streams: list[str]) -> list[sqlite3.Row]:
    zs = columns(con, "ZSOURCE")
    dev = "s.ZDEVICEID" if "ZDEVICEID" in zs else "NULL"
    marks = ",".join("?" * len(streams))
    return list(con.execute(
        f"SELECT {dev} AS device_id, o.ZSTREAMNAME AS stream, COUNT(*) AS n, "
        f"MIN(o.ZSTARTDATE) AS lo, MAX(o.ZSTARTDATE) AS hi, "
        f"SUM(o.ZENDDATE - o.ZSTARTDATE) AS secs "
        f"FROM ZOBJECT o LEFT JOIN ZSOURCE s ON s.Z_PK = o.ZSOURCE "
        f"WHERE o.ZSTREAMNAME IN ({marks}) GROUP BY 1, 2 ORDER BY n DESC", streams))


# --------------------------------------------------------------------------- output
def shape(r: dict, names: dict[str, str]) -> dict:
    start, end = cd_to_unix(r["start_cd"]), cd_to_unix(r["end_cd"])
    dev = r.get("device_id") or ""
    local_start = to_local_iso(start, r.get("gmt_off"))
    return {
        "uuid": r.get("uuid") or f"pk:{r['pk']}",
        "stream": r["stream"],
        "device_id": dev,
        "device": names.get(dev, ""),
        "app": r["value"] or "",
        "start_local": local_start,
        "end_local": to_local_iso(end, r.get("gmt_off")),
        "seconds": round(end - start, 1) if start is not None and end is not None else "",
        "day": local_start[:10],
        "gmt_offset_s": r.get("gmt_off") if r.get("gmt_off") is not None else "",
        "created_local": to_local_iso(cd_to_unix(r.get("created_cd")), r.get("gmt_off")),
        "source_bundle": r.get("source_bundle") or "",
    }


def write_raw(start: dt.date, end: dt.date, streams: list[str], rows: list[dict]) -> Path:
    RAW_DIR.mkdir(exist_ok=True)
    path = RAW_DIR / f"{start.isoformat()}_{end.isoformat()}.json"
    path.write_text(json.dumps({
        "store": str(STORE), "streams": streams, "from": start.isoformat(), "to": end.isoformat(),
        "pulledAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "count": len(rows), "rows": rows,
    }, indent=1))
    return path


def write_usage(rows: list[dict]) -> Path:
    path = CSV_DIR / "app_usage.csv"
    existing: dict[str, dict] = {}
    if path.exists():
        with path.open(newline="") as fh:
            for r in csv.DictReader(fh):
                existing[r["uuid"]] = r
    for r in rows:
        existing[r["uuid"]] = r                       # newer pull wins
    cols = list(rows[0].keys()) if rows else list(next(iter(existing.values())).keys())
    CSV_DIR.mkdir(exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in sorted(existing.values(), key=lambda x: x.get("start_local", "")):
            w.writerow(r)
    return path


def write_daily(names: dict[str, str]) -> Path:
    """Rebuilt from app_usage.csv every time: seconds per day x device x app."""
    src, path = CSV_DIR / "app_usage.csv", CSV_DIR / "daily.csv"
    agg: dict[tuple, float] = defaultdict(float)
    with src.open(newline="") as fh:
        for r in csv.DictReader(fh):
            if r["stream"] != "/app/usage" or not r["seconds"]:
                continue
            agg[(r["day"], r["device_id"], r["app"])] += float(r["seconds"])
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["day", "device_id", "device", "app", "seconds", "minutes"])
        for (day, dev, app), s in sorted(agg.items()):
            w.writerow([day, dev, names.get(dev, ""), app, round(s, 1), round(s / 60, 1)])
    return path


# --------------------------------------------------------------------------- commands
def fmt_cd(v) -> str:
    return to_local_iso(cd_to_unix(v), None)[:16] if v is not None else "—"


def cmd_check(a):
    with tempfile.TemporaryDirectory() as td:
        con = connect(snapshot(Path(td)))
        print(f"Store readable: {STORE}")
        print(f"ZOBJECT columns: {', '.join(sorted(columns(con, 'ZOBJECT')))}")
        print(f"ZSOURCE columns: {', '.join(sorted(columns(con, 'ZSOURCE')))}")
        print("\nStreams (all rows):")
        for r in stream_summary(con):
            print(f"  {r['n']:8d}  {fmt_cd(r['lo'])} .. {fmt_cd(r['hi'])}  {r['stream']}")
        print("\nDevices in the usage streams:")
        _print_devices(device_summary(con, a.streams.split(",")))


def _print_devices(rows):
    names = device_names()
    if not rows:
        print("  (none) — no rows in those streams. Check Screen Time -> Share Across Devices.")
    for r in rows:
        dev = r["device_id"] or ""
        label = names.get(dev, "(this Mac?)" if not dev else "(unnamed — add to devices.json)")
        hrs = (r["secs"] or 0) / 3600
        print(f"  {r['n']:8d} rows  {hrs:8.1f} h  {fmt_cd(r['lo'])} .. {fmt_cd(r['hi'])}  {r['stream']:16s} {dev or 'NULL':40s} {label}")


def cmd_devices(a):
    with tempfile.TemporaryDirectory() as td:
        con = connect(snapshot(Path(td)))
        _print_devices(device_summary(con, a.streams.split(",")))
    print(f"\nName devices in {DEVICE_NAMES.name}: {{\"<device_id>\": \"iPhone 15 Plus\", \"\": \"MacBook\"}}")


def cmd_pull(a):
    today = dt.date.today()
    end = dt.date.fromisoformat(a.to) if a.to else today + dt.timedelta(days=1)
    start = dt.date.fromisoformat(getattr(a, "from")) if getattr(a, "from") else end - dt.timedelta(days=a.days)
    if start >= end:
        sys.exit("--from must be before --to")
    streams = [s.strip() for s in a.streams.split(",") if s.strip()]
    names = device_names()
    with tempfile.TemporaryDirectory() as td:
        con = connect(snapshot(Path(td)))
        rows = usage_rows(con, streams, start, end)
    raw = write_raw(start, end, streams, rows)
    shaped = [shape(r, names) for r in rows]
    if a.device:
        shaped = [r for r in shaped if r["device_id"] == a.device]
    usage = write_usage(shaped)
    daily = write_daily(names)
    by_dev = defaultdict(int)
    for r in shaped:
        by_dev[r["device"] or r["device_id"] or "NULL"] += 1
    print(f"Pulled {start} -> {end} (exclusive): {len(shaped)} intervals from {', '.join(streams)}")
    for dev, n in sorted(by_dev.items(), key=lambda x: -x[1]):
        print(f"  {n:7d}  {dev}")
    print(f"  -> {usage.relative_to(HERE)}, {daily.relative_to(HERE)}  (raw: {raw.name})")
    unnamed = {r["device_id"] for r in shaped if r["device_id"] and not r["device"]}
    if unnamed:
        print(f"Unnamed device ids (add to {DEVICE_NAMES.name}): {', '.join(sorted(unnamed))}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--streams", default=",".join(DEFAULT_STREAMS),
                   help=f"comma list of ZSTREAMNAME values (default {','.join(DEFAULT_STREAMS)})")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="verify access; list streams, columns, devices").set_defaults(fn=cmd_check)
    sub.add_parser("devices", help="device ids in the usage streams").set_defaults(fn=cmd_devices)
    pl = sub.add_parser("pull", help="pull intervals into raw/ and csv/")
    pl.add_argument("--from", help="start date YYYY-MM-DD (inclusive, local)")
    pl.add_argument("--to", help="end date YYYY-MM-DD (exclusive); default tomorrow")
    pl.add_argument("--days", type=int, default=30, help="if --from omitted: days back from --to (default 30)")
    pl.add_argument("--device", help="keep only this ZDEVICEID in the CSVs (raw/ keeps everything)")
    pl.set_defaults(fn=cmd_pull)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
