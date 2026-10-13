#!/usr/bin/env python3
"""
Hour-of-day Screen Time for a device, measured from the Settings pane's
hourly chart. The chart's bars are not exposed to accessibility, so each day
is captured with `screencapture -l <window id>` and the 24 bars are measured
in pixels, then scaled so the day sums to the pane's own "All Usage" total.

Needs, on top of screentime_ui_pull.py's Accessibility grant:
  System Settings → Privacy & Security → Screen Recording → the app running this.
  (Without it screencapture prints "could not create image from display".)

Usage
  python3 screentime_hourly_pull.py pull --days 30                # iPhone
  python3 screentime_hourly_pull.py pull --days 7 --device "Mark’s MacBook Pro"

Output
  csv/hourly_ui.csv            day, device, hour, seconds, px, pulled_at  (replaced per day)
  raw/hourly/{device}_{day}.png   the cropped chart the numbers came from

Method (observed on macOS 26.5.2, 2026-09-27):
  - The usage group (device picker, date, total, two charts) is located through
    accessibility; the window is captured by id so it need not be in front.
  - Inside that group the hourly chart has five dashed gridlines 24 px apart
    (at 2x). Bars rise from the bottom gridline; a bar's height is the run of
    non-background pixels above it, skipping gridline rows.
  - seconds[h] = total_seconds * px[h] / sum(px). The gridline scale is also
    reported as `scale_check`: ~1.0 means the axis topped at 60 min, ~0.5 at 2 h.
Resolution is one hour. The gray cap on tall bars is time the pane does not
attribute to any app (it is inside "All Usage"), so hourly totals include it.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

import screentime_ui_pull as ui  # OPEN_PANE, PANE, osascript, parse_date, parse_seconds

HERE = Path(__file__).resolve().parent
CSV_DIR, RAW_DIR = HERE / "csv", HERE / "raw" / "hourly"
OUT = CSV_DIR / "hourly_ui.csv"
SCALE = 2                      # Retina points -> pixels


# --------------------------------------------------------------------------- pane driving
def step_script(device: str, first: bool) -> str:
    """Select device + Today on the first call; afterwards click 'previous day'.
    Returns 'dateLabel<FS>total<FS>winX,winY<FS>gX,gY,gW,gH<FS>windowId'."""
    dev = device.replace('"', '\\"')
    select = f'''
    click pop up button "Device" of g
    delay 0.7
    click menu item "{dev}" of menu 1 of pop up button "Device" of g
    delay 1.2
    click button 2 of g
    delay 1.2
    ''' if first else '''
    set ds to value of pop up button 2 of g
    click button 1 of g
    repeat 25 times
        delay 0.2
        if (value of pop up button 2 of g) is not ds then exit repeat
    end repeat
    delay 1.2
    '''
    return (ui.OPEN_PANE if first else "") + f'''
tell application "System Events" to tell process "System Settings"
    set g to a reference to {ui.PANE}
    {select}
    set dateStr to value of pop up button 2 of g
    set totalStr to ""
    try
        set totalStr to value of static text 3 of g
    end try
    set wp to position of window 1
    set gp to position of g
    set gs to size of g
    set AppleScript's text item delimiters to ","
    set line1 to dateStr & "{ui.FIELD_SEP}" & totalStr & "{ui.FIELD_SEP}" & (wp as text) & "{ui.FIELD_SEP}" & ((gp & gs) as text)
end tell
tell application "System Settings" to set wid to id of window 1
return line1 & "{ui.FIELD_SEP}" & wid
'''


def geometry_script() -> str:
    return f'''
tell application "System Events" to tell process "System Settings"
    set g to a reference to {ui.PANE}
    set wp to position of window 1
    set gp to position of g
    set gs to size of g
    set AppleScript's text item delimiters to ","
    set line1 to (wp as text) & "{ui.FIELD_SEP}" & ((gp & gs) as text)
end tell
tell application "System Settings" to set wid to id of window 1
return line1 & "{ui.FIELD_SEP}" & wid
'''


def restore_script() -> str:
    return f'''
tell application "System Events" to tell process "System Settings"
    set g to a reference to {ui.PANE}
    click button 2 of g
    delay 0.5
    click pop up button "Device" of g
    delay 0.7
    click menu item "All Devices" of menu 1 of pop up button "Device" of g
end tell
'''


def capture(window_id: int, path: Path) -> None:
    r = subprocess.run(["screencapture", "-x", "-l", str(window_id), str(path)], capture_output=True, text=True)
    if r.returncode != 0 or not path.exists():
        sys.exit("screencapture failed (Screen Recording permission?):\n" + (r.stderr or r.stdout))


# --------------------------------------------------------------------------- measuring

def crop_group(png: Path, win_xy: tuple[int, int], g_rect: tuple[int, int, int, int]) -> np.ndarray:
    a = np.array(Image.open(png))
    ys, xs = np.where(a[:, :, 3] == 255)               # the opaque window body; the rest is shadow
    wx0, wy0 = xs.min(), ys.min()
    gx, gy, gw, gh = g_rect
    x = wx0 + (gx - win_xy[0]) * SCALE
    y = wy0 + (gy - win_xy[1]) * SCALE
    return a[y:y + gh * SCALE, x:x + gw * SCALE, :3].astype(int)


def measure(crop: np.ndarray) -> tuple[list[int], dict]:
    cols, counts = np.unique(crop.reshape(-1, 3), axis=0, return_counts=True)
    bg = cols[counts.argmax()]
    diff = np.abs(crop - bg).sum(axis=2) > 25
    frac = diff.mean(axis=1)
    # gridline rows: long faint lines. Cluster adjacent rows; the hourly chart is the last five clusters.
    line_rows = [y for y in range(crop.shape[0]) if frac[y] > 0.85]
    clusters: list[list[int]] = []
    for y in line_rows:
        if clusters and y - clusters[-1][-1] <= 1:
            clusters[-1].append(y)
        else:
            clusters.append([y])
    if len(clusters) < 5:
        raise RuntimeError(f"found {len(clusters)} gridlines, expected ≥5")
    hourly = clusters[-5:]
    grid_rows = {y for c in hourly for y in c}
    base = hourly[-1][0] - 1
    spacing = (hourly[-1][0] - hourly[0][0]) / 4
    xs = np.where(diff[hourly[-2][0]])[0]              # an unlabeled gridline gives the plot width
    x0, x1 = int(xs.min()), int(xs.max())
    w = (x1 - x0) / 24
    heights = []
    for h in range(24):
        a, b = int(x0 + h * w + 4), int(x0 + (h + 1) * w - 4)
        best = 0
        for x in range(a, b):
            y, n = base, 0
            while y > hourly[0][0] - 40:
                if diff[y, x]:
                    n += 1
                elif y in grid_rows:
                    pass
                else:
                    break
                y -= 1
            best = max(best, n)
        heights.append(best)
    return heights, {"base": base, "spacing": spacing, "x0": x0, "x1": x1, "bg": bg.tolist()}


# --------------------------------------------------------------------------- output
def write_rows(rows: list[dict]) -> Path:
    reread = {(r["day"], r["device"]) for r in rows}
    keep = []
    if OUT.exists():
        with OUT.open(newline="") as fh:
            keep = [r for r in csv.DictReader(fh) if (r["day"], r["device"]) not in reread]
    allrows = sorted(keep + rows, key=lambda r: (r["day"], r["device"], int(r["hour"])))
    CSV_DIR.mkdir(exist_ok=True)
    with OUT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["day", "device", "hour", "seconds", "px", "scale_check", "pulled_at"])
        w.writeheader()
        w.writerows(allrows)
    return OUT


# --------------------------------------------------------------------------- command
def cmd_pull(a):
    today = dt.date.today()
    device = a.device
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    rows, prev = [], None
    with tempfile.TemporaryDirectory() as td:
        for i in range(a.days):
            out = ui.osascript(step_script(device, first=(i == 0)))
            date_label, total_label, wp, gr, wid = out.split(ui.FIELD_SEP)
            if date_label == prev:
                print("  reached the front of the history"); break
            prev = date_label
            day = ui.parse_date(date_label, today)
            total = ui.parse_seconds(total_label) or 0
            png = Path(td) / f"{i}.png"
            capture(int(wid), png)
            win_xy = tuple(int(v) for v in wp.split(","))
            g_rect = tuple(int(v) for v in gr.split(","))
            crop = crop_group(png, win_xy, g_rect)
            safe = re.sub(r"[^A-Za-z0-9]+", "_", device).strip("_").lower()
            Image.fromarray(crop.astype(np.uint8)).save(RAW_DIR / f"{safe}_{day}.png")
            try:
                px, geo = measure(crop)
            except RuntimeError:
                # geometry was read mid-relayout (sliver crop); re-read it and capture again once
                time.sleep(1.5)
                wp, gr, wid = ui.osascript(geometry_script()).split(ui.FIELD_SEP)
                capture(int(wid), png)
                crop = crop_group(png, tuple(int(v) for v in wp.split(",")), tuple(int(v) for v in gr.split(",")))
                Image.fromarray(crop.astype(np.uint8)).save(RAW_DIR / f"{safe}_{day}.png")
                try:
                    px, geo = measure(crop)
                except RuntimeError as e:
                    print(f"  {day}: {e} after retry; skipped", file=sys.stderr); continue
            tot_px = sum(px)
            grid_min = tot_px / (geo["spacing"] / 15)          # minutes if the axis topped at 60 min
            check = round(grid_min / (total / 60), 2) if total else ""
            for h in range(24):
                secs = round(total * px[h] / tot_px) if tot_px else 0
                rows.append({"day": day.isoformat(), "device": device, "hour": h, "seconds": secs,
                             "px": px[h], "scale_check": check, "pulled_at": stamp})
            print(f"  {day} {day.strftime('%a')} total {total // 60:4d}m  bars {tot_px:4d}px  "
                  f"scale_check {check}  first-use hour {next((h for h in range(24) if px[h]), '-')}")
    print(f"-> {write_rows(rows).relative_to(HERE)}  ({len(rows) // 24} days)")
    try:
        ui.osascript(restore_script())
    except SystemExit as e:                      # cosmetic: leave the pane as found; never lose data over it
        print(f"  (could not restore the pane to All Devices/Today: {str(e).splitlines()[-1]})", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("pull")
    pl.add_argument("--days", type=int, default=30)
    pl.add_argument("--device", default="iPhone")
    pl.set_defaults(fn=cmd_pull)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
