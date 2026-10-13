#!/usr/bin/env python3
"""
Pull per-device Screen Time (iPhone included) by reading the System Settings
Screen Time pane through the macOS accessibility API.

Why this exists: the synced iPhone usage lives in RMAdminStore-Cloud.sqlite
inside ScreenTimeAgent's protected container, which even Full Disk Access
cannot open, and the CloudKit container is encrypted. The Settings pane is the
only local surface that shows the phone's numbers, so we read the pane.

Needs: System Settings → Privacy & Security → Accessibility → the app that runs
this script (VS Code for Claude, your terminal, or Python.app for the launch
agent). Without it osascript fails with error -1719.

What it does, per device, per day: opens System Settings → Screen Time →
App & Website Activity, picks the device, clicks "Today", then steps back one
day at a time reading the date, the day total, and every row of the apps table.
It restores the pane to "All Devices" / Today when done.

Usage
  python3 screentime_ui_pull.py pull --days 30                 # iPhone only (default)
  python3 screentime_ui_pull.py pull --days 30 --device iPhone --device "Mark’s MacBook Pro"
  python3 screentime_ui_pull.py devices                        # names in the Device menu

Output
  raw/ui_{device}_{date}.txt   the tab-separated text the pane returned
  csv/daily_ui.csv             day, device, app, seconds, minutes, pulled_at
                               deduped on (day, device, app); newer pull wins

Resolution is what the pane shows: minutes for anything over a minute, seconds
below. The "All Usage" row is the day total. A day with no rows is written as
a single All Usage = 0 row so absence is visible. Accessibility paths were
observed on macOS 26.5.2 (2026-09-27); if Apple moves a control, fix PANE below.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW_DIR = HERE / "raw"
CSV_DIR = HERE / "csv"
OUT = CSV_DIR / "daily_ui.csv"

PANE_URL = "x-apple.systempreferences:com.apple.Screen-Time-Settings.extension"
# Content group of the App & Website Activity subpane, relative to window 1 of System Settings.
CONTENT = "group 3 of splitter group 1 of group 1 of window 1"
PANE = f"group 1 of scroll area 1 of group 1 of {CONTENT}"          # device/date controls + total
TABLE = f"outline 1 of scroll area 1 of group 1 of scroll area 2 of group 1 of {CONTENT}"
ROW_SEP, FIELD_SEP, DAY_SEP = "␞", "␟", "␝"          # ␞ ␟ ␝ — never appear in app names

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


# --------------------------------------------------------------------------- applescript
def osascript(script: str, timeout: int = 900) -> str:
    r = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        err = r.stderr.strip()
        if "assistive access" in err:
            sys.exit("Accessibility permission missing for the app running this script.\n"
                     "System Settings → Privacy & Security → Accessibility → add it, then re-run.\n" + err)
        sys.exit(f"osascript failed:\n{err}")
    return r.stdout.rstrip("\n")


OPEN_PANE = f'''
do shell script "open '{PANE_URL}'"
tell application "System Events"
    tell process "System Settings"
        repeat 40 times
            delay 0.25
            if (count of windows) > 0 then exit repeat
        end repeat
        set wn to name of window 1
        if wn does not contain "App & Website Activity" then
            -- first row under "Limit Usage" on the Screen Time root pane
            click button 1 of group 1 of scroll area 1 of group 1 of {CONTENT}
            repeat 40 times
                delay 0.25
                if name of window 1 contains "App & Website Activity" then exit repeat
            end repeat
        end if
    end tell
end tell
'''


def script_devices() -> str:
    return OPEN_PANE + f'''
tell application "System Events" to tell process "System Settings"
    set g to a reference to {PANE}
    click pop up button "Device" of g
    delay 0.7
    set nms to name of every menu item of menu 1 of pop up button "Device" of g
    key code 53
    set AppleScript's text item delimiters to "{ROW_SEP}"
    return nms as text
end tell
'''


READ_ROWS = f'''
on readRows()
    set acc to {{}}
    tell application "System Events" to tell process "System Settings"
        try
            set t to a reference to {TABLE}
            repeat with r in rows of t
                set nm to ""
                set tm to ""
                try
                    set nm to value of static text 1 of group 1 of UI element 1 of r
                end try
                try
                    set tm to value of static text 1 of group 1 of UI element 2 of r
                end try
                set end of acc to nm & "{FIELD_SEP}" & tm
            end repeat
        end try
    end tell
    return acc
end readRows
'''


def script_pull(device: str, days: int) -> str:
    dev = device.replace('"', '\\"')
    return READ_ROWS + OPEN_PANE + f'''
tell application "System Events" to tell process "System Settings"
    set g to a reference to {PANE}
    click pop up button "Device" of g
    delay 0.7
    click menu item "{dev}" of menu 1 of pop up button "Device" of g
    delay 1.2
    click button 2 of g  -- Today
    delay 1.0
    set out to ""
    set prevDate to ""
    repeat {days} times
        set dateStr to value of pop up button 2 of g
        if dateStr is prevDate then exit repeat  -- ran off the front of the history
        set prevDate to dateStr
        set totalStr to ""
        try
            set totalStr to value of static text 3 of g
        end try
        -- the table refreshes after the header; read it twice and keep the fuller read
        set readA to my readRows()
        delay 0.7
        set readB to my readRows()
        if (count of readA) > (count of readB) then
            set rowsOut to readA
        else
            set rowsOut to readB
        end if
        set dayOut to dateStr & "{FIELD_SEP}" & totalStr
        repeat with x in rowsOut
            set dayOut to dayOut & "{ROW_SEP}" & x
        end repeat
        set out to out & dayOut & "{DAY_SEP}"
        click button 1 of g  -- previous day
        repeat 25 times
            delay 0.2
            if (value of pop up button 2 of g) is not dateStr then exit repeat
        end repeat
        delay 0.8
    end repeat
    -- leave the pane as we found it
    click button 2 of g
    delay 0.5
    click pop up button "Device" of g
    delay 0.7
    click menu item "All Devices" of menu 1 of pop up button "Device" of g
    return out
end tell
'''


# --------------------------------------------------------------------------- parsing
def parse_date(label: str, today: dt.date) -> dt.date | None:
    """'Today, September 27' / 'Yesterday, September 26' / 'Thursday, September 24' → date.
    No year in the label: assume the most recent occurrence on or before today."""
    m = re.search(r"([A-Z][a-z]+) (\d{1,2})", label)
    if not m or m.group(1) not in MONTHS:
        return None
    d = dt.date(today.year, MONTHS[m.group(1)], int(m.group(2)))
    if d > today:
        d = d.replace(year=today.year - 1)
    return d


def parse_seconds(label: str) -> int | None:
    """'3 hours, 23 minutes' / '58 seconds' / '1 hour' → seconds."""
    if not label:
        return None
    total, found = 0, False
    for n, unit in re.findall(r"(\d+)\s*(hour|minute|second)", label):
        found = True
        total += int(n) * {"hour": 3600, "minute": 60, "second": 1}[unit]
    return total if found else None


def parse_pull(text: str, device: str, today: dt.date) -> list[dict]:
    rows, stamp = [], dt.datetime.now().isoformat(timespec="seconds")
    for day_blob in filter(None, text.split(DAY_SEP)):
        parts = day_blob.split(ROW_SEP)
        date_label, total_label = (parts[0].split(FIELD_SEP) + [""])[:2]
        day = parse_date(date_label, today)
        if day is None:
            print(f"  skipped unparseable date label: {date_label!r}", file=sys.stderr)
            continue
        table = [p.split(FIELD_SEP) for p in parts[1:]]
        if not table:
            rows.append({"day": day.isoformat(), "device": device, "app": "All Usage",
                         "seconds": parse_seconds(total_label) or 0, "pulled_at": stamp})
        for name, tm in table:
            secs = parse_seconds(tm)
            if not name or secs is None:
                continue
            rows.append({"day": day.isoformat(), "device": device, "app": name, "seconds": secs, "pulled_at": stamp})
    return rows


# --------------------------------------------------------------------------- output
def write_csv(new: list[dict]) -> Path:
    """A re-read day replaces every existing row for that (day, device): the pane
    sometimes shows an app as its bundle id before the name resolves, so merging
    by app name would keep both."""
    reread = {(r["day"], r["device"]) for r in new}
    existing: dict[tuple, dict] = {}
    if OUT.exists():
        with OUT.open(newline="") as fh:
            for r in csv.DictReader(fh):
                if (r["day"], r["device"]) not in reread:
                    existing[(r["day"], r["device"], r["app"])] = r
    for r in new:
        r["minutes"] = round(int(r["seconds"]) / 60, 1)
        existing[(r["day"], r["device"], r["app"])] = r
    CSV_DIR.mkdir(exist_ok=True)
    with OUT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["day", "device", "app", "seconds", "minutes", "pulled_at"])
        w.writeheader()
        for k in sorted(existing):
            w.writerow(existing[k])
    return OUT


# --------------------------------------------------------------------------- commands
def cmd_devices(_):
    print("\n".join(osascript(script_devices()).split(ROW_SEP)))


def cmd_pull(a):
    today = dt.date.today()
    devices = a.device or ["iPhone"]
    RAW_DIR.mkdir(exist_ok=True)
    all_rows = []
    for dev in devices:
        print(f"{dev}: reading {a.days} days from the Screen Time pane…")
        text = osascript(script_pull(dev, a.days))
        safe = re.sub(r"[^A-Za-z0-9]+", "_", dev).strip("_").lower()
        raw = RAW_DIR / f"ui_{safe}_{today.isoformat()}.txt"
        raw.write_text(text.replace(DAY_SEP, "\n").replace(ROW_SEP, "\n  ").replace(FIELD_SEP, "\t"))
        rows = parse_pull(text, dev, today)
        days = sorted({r["day"] for r in rows})
        print(f"  {len(rows)} rows over {len(days)} days"
              + (f" ({days[0]} → {days[-1]})" if days else "") + f"  raw: {raw.name}")
        all_rows.extend(rows)
    out = write_csv(all_rows)
    print(f"-> {out.relative_to(HERE)}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices", help="list the names in the pane's Device menu").set_defaults(fn=cmd_devices)
    pl = sub.add_parser("pull", help="read N days per device into csv/daily_ui.csv")
    pl.add_argument("--days", type=int, default=30, help="days back from today, inclusive of today (default 30)")
    pl.add_argument("--device", action="append", help="Device-menu name; repeatable (default: iPhone)")
    pl.set_defaults(fn=cmd_pull)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
