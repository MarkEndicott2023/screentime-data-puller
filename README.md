# Screen Time (iPhone 15 Plus + Mac)

Apple has no public Screen Time API, Shortcuts action, or export button.
Three pullers cover the places the data is reachable:

| Script | Covers | Source | Resolution |
|---|---|---|---|
| `screentime_ui_pull.py` | iPhone (and Mac) | Screen Time pane, via accessibility | per app per day, minutes |
| `screentime_hourly_pull.py` | iPhone (and Mac) | screenshot of the pane's hourly chart | per hour per day |
| `screentime_pull.py` | Mac only | `~/Library/Application Support/Knowledge/knowledgeC.db` | per session, to the second |

Synced iPhone rows live in ScreenTimeAgent's protected container, which even
Full Disk Access cannot open. The Settings pane is the only local surface
that shows the phone.

## Outputs (gitignored, personal data)

```
raw/                 pane rows, chart crops, raw knowledgeC rows
csv/daily_ui.csv     day, device, app, seconds, minutes, pulled_at
csv/hourly_ui.csv    day, hour, seconds
csv/app_usage.csv    Mac sessions, deduped by uuid
csv/daily.csv        Mac seconds per day × app, rebuilt each pull
devices.json         {"<ZDEVICEID>": "name"}, optional, Mac only
```

## iPhone: `screentime_ui_pull.py`

Setup once: turn on App & Website Activity and Share Across Devices on both
iPhone and Mac. Grant **Accessibility** to the app running the script;
without it `osascript` fails with `-1719`.

```
python3 screentime_ui_pull.py devices                 # names in the Device menu
python3 screentime_ui_pull.py pull --days 30          # iPhone, default
python3 screentime_ui_pull.py pull --days 30 --device iPhone --device "Mark’s MacBook Pro"
```

The script opens the pane, picks the device, and steps back one day at a time
(~1 s per day). Keep System Settings free while it runs. `daily_ui.csv`
dedupes on (day, device, app); the newer pull wins.

**"All Usage" is not the sum of the app rows.** Most days the rows fall 7–25 min
short (minute rounding); some days 40–165 min. The gap is in Apple's data, not
the read. Treat `All Usage` as the daily total and the rows as the
attributable part.

## iPhone by hour: `screentime_hourly_pull.py`

The hourly chart is not exposed to accessibility, so this screenshots the
pane, measures the 24 bars in pixels, and scales each day so bars sum to
"All Usage". Needs **Screen Recording** on top of Accessibility.
`scale_check` ≈ 0.95 means the axis topped at 60 min.

```
python3 screentime_hourly_pull.py pull --days 30
```

## Mac: `screentime_pull.py`

Setup once: grant **Full Disk Access** to whatever runs the script, then
restart it. Run `check` to list streams and device ids; copy ids into
`devices.json`. The empty `ZDEVICEID` row is this Mac.

```
python3 screentime_pull.py check
python3 screentime_pull.py pull --days 30
python3 screentime_pull.py pull --from 2026-09-01 --to 2026-10-01    # --to exclusive
```

Local time uses the offset the device recorded.

## Caveats

- **iPhone rows never reach knowledgeC**, even with Share Across Devices on
  (verified 2026-09-27, macOS 26.5.2). This script is Mac only.
- **Retention.** knowledgeC keeps roughly four weeks. Pull weekly; no backfill.
- **A missing day is not a zero.** No rows = no sync, not no use.
- Pickups and notifications live in other streams, or not at all.
