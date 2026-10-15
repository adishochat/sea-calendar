---
name: sea-conditions-calendar
description: Puts kite-surfing and swimming windows into the user's Google Calendar based on sea forecasts (wind, waves, daylight) for Dor beach (חוף דור), and keeps them up to date - a week ahead, re-checked every morning for today and every evening for tomorrow. Use this skill whenever the user mentions the sea-conditions calendar, kite or swim forecast events, "מצב ים", "יומן ים", "תחזית ים ליומן", sea-calendar, changing the kite/swim thresholds (wind knots, wave height, hours), adding another beach, fixing or re-authorizing the Google Calendar sync, the launchd schedule, or the Windguru WRF-9 comparison, even if they do not say "skill".
---

# Sea conditions calendar

A small Python tool (`scripts/sea_calendar.py`, standard library only) that runs **on the user's Mac**, reads the forecast, decides when the sea suits kiting or swimming, and writes tentative events to a dedicated Google Calendar.

The user writes Hebrew. Reply in Hebrew and keep jargon light (say "הגדרות" rather than "config JSON" unless they use the term first).

## Why it runs on the user's computer, not here

This workspace cannot reach Open-Meteo: the shell is blocked by the network allowlist and WebFetch is refused by the site's robots rules. So nothing here can fetch a real forecast. The script therefore runs on the Mac, and Claude's job is to set it up, explain it, change its settings and debug it. Do not claim you saw live forecast numbers unless the user pasted output from `check`.

## What the tool decides

Defaults (all editable in `~/.sea-calendar/config.json`):

| | Rule |
|---|---|
| Kite | wind **above** 16 kn. Waves do not matter. No direction filter (the user chose that; mention once that offshore/east wind is the dangerous case and a filter exists, only if they bring up safety or ask for one) |
| Swim | waves **at most** 0.4 m **and** wind **at most** 10 kn |
| Hours | sunrise to 30 minutes after sunset |
| Minimum window | 60 minutes |
| Place | Dor beach, wind point 32.614, 34.910; wave point 32.62, 34.88 (slightly offshore so the grid cell is sea) |

Kite and swim can never overlap (the wind ranges are disjoint), so no tie-break is needed.

**Sources.** Open-Meteo gives waves and wind. Windguru WRF-9 can be added as a second wind source (see `references/windguru.md`); it is off by default because Windguru has no documented public API and the endpoint is unverified. With two wind sources, a window where both pass is plain; a window where only one passes is kept but the title gets " (מקור אחד)" and the description says so. A source that has no data for an hour is not counted as disagreeing.

## Runs and what they do to the calendar

| Mode | When (default) | Dates |
|---|---|---|
| `week` | Sundays 08:00 | today through +6 days |
| `today` | every day 06:00 | today only |
| `tomorrow` | every day 20:00 | tomorrow only |

Each run makes the managed events for its dates match the forecast: adds new windows, moves or retitles changed ones, deletes windows that disappeared.

Design choices worth knowing, because users will ask about them:

- Events are **tentative, show as free, and titled "הערכה"** so a forecast never blocks real plans and is visibly an estimate.
- They live in their own calendar ("תחזית ים", created on first run) and carry a hidden marker. The tool never touches events without the marker.
- If the user **deletes** an event, it is remembered and not recreated for that day and kind.
- Windows that already ended are left alone. Forecast changes that only alter the description update silently; added, moved or removed windows trigger a macOS notification.

## First-time setup (walk the user through it, one step at a time)

1. Get `scripts/sea_calendar.py` onto the Mac, then `python3 sea_calendar.py init`. This creates `~/.sea-calendar/` with the settings file and a stable copy of the script, which is what the schedule uses.
2. Google access, once: follow `references/google-setup.md` (about 10 minutes). The one step people miss is publishing the OAuth app; otherwise sign-in expires every 7 days.
3. `python3 ~/.sea-calendar/sea_calendar.py auth`
4. `python3 ~/.sea-calendar/sea_calendar.py check --hourly` and read the output with the user. This is the first time real data is seen, so confirm the numbers look plausible for Dor (wave height especially, since the marine grid is coarse).
5. `sync --mode week --dry-run`, then without `--dry-run`.
6. `install-schedule` for the three automatic runs. launchd runs a job missed while the Mac slept as soon as it wakes. The Mac must be on (or wake) around those times.

If the session is linked to the user's computer and file tools for it are available, use them to read or edit `~/.sea-calendar/config.json` directly. Otherwise give the exact lines to change.

## Running on a Linux server instead of the Mac

The same script runs on any Linux box with Python 3.9+ and cron (tested logic only; the user's server is an Oracle Cloud Always Free VM, Oracle Linux 9, user `opc`). Why a server: the Mac must be awake at run time, a server does not.

1. Copy the current `sea_calendar.py`, `config.json`, `client_secret.json` and `token.json` from the Mac's `~/.sea-calendar/` to the same folder on the server with `scp`. Do the Google sign-in (`auth`) on the Mac, because it needs a browser; the refresh token works from any machine. Do not copy `state.json`.
2. On the server: `python3 ~/.sea-calendar/sea_calendar.py check` to confirm it can reach Open-Meteo, then `install-schedule`. On Linux this writes a managed block into the user's crontab.
3. The cron lines fire **hourly** and the script itself only acts at the configured local hour (`--only-hour`, `--only-weekday`). That is deliberate: it makes the server's timezone, cron's `CRON_TZ` support and Israel's clock change irrelevant.
4. If `crontab` is missing: `sudo dnf install -y cronie && sudo systemctl enable --now crond` (Oracle Linux) or `sudo apt install cron` (Ubuntu).
5. Remove the Mac schedule (`uninstall-schedule` there) so two machines do not both write to the calendar.
6. `token.json` and `client_secret.json` give calendar access: keep them mode 600 and never put them in a repository.

Oracle may reclaim Always Free VMs that stay almost idle, so if updates stop, check that the instance still exists before debugging the script.

## Changing settings

Edit `~/.sea-calendar/config.json`; only keys that differ from the defaults need to be there. Common requests:

- thresholds: `kite.min_wind_kn`, `swim.max_wave_m`, `swim.max_wind_kn`
- hours: `daylight.start_offset_min`, `daylight.end_offset_min`; shortest window: `min_window_minutes`
- direction filter: `kite.direction_filter` = `{"from_deg": 200, "to_deg": 340}` (wraps around north if from > to)
- another beach: `location` (lat/lon for wind, `wave_lat`/`wave_lon` slightly offshore). Several beaches need a second config and a second calendar name; the tool handles one location per config
- run times: `schedule`, then `install-schedule` again
- hide partial-agreement windows: `include_partial: false`

After a change, `check` shows the effect without touching the calendar.

## Troubleshooting

- `Google sign-in expired`: run `auth` again. If it repeats weekly, the OAuth app is still in "Testing"; publish it (google-setup.md).
- `HTTP 4xx` from Open-Meteo: usually a typo in coordinates or temporary rate limiting; rerun later.
- `Windguru skipped: ...` appears as a warning and the run continues with Open-Meteo only. That is by design.
- Events duplicated or missing after manual edits: the log is `~/.sea-calendar/logs/<mode>.log`, and `sync --mode week --dry-run` prints what it would change.
- `CERTIFICATE_VERIFY_FAILED`: a python.org Python on macOS has no CA certificates. The script already adds the macOS bundle (`/etc/ssl/cert.pem`) itself, so this means an old copy of the script is installed in `~/.sea-calendar/`: rerun `init` from the current file to refresh it. If it still fails, run the "Install Certificates.command" in the Python folder under Applications. Never turn verification off.

## Files

- `scripts/sea_calendar.py`: the whole tool (`init`, `auth`, `check`, `sync`, `install-schedule`, `uninstall-schedule`)
- `references/google-setup.md`: one-time Google Calendar access (Hebrew)
- `references/windguru.md`: enabling the optional WRF-9 source
