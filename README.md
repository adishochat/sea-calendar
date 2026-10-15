# sea-calendar

Puts **kite-surfing** and **swimming** windows into Google Calendar from sea forecasts
(wind, waves, daylight), by default for Dor beach (חוף דור), Israel.

- A week-ahead estimate (Sundays), a daily re-check of today, and an evening re-check of tomorrow.
- Kite: wind above 16 kn. Swim: waves up to 0.4 m and wind up to 10 kn. Daylight only. All configurable.
- Wind and waves from Open-Meteo; optional Windguru WRF-9 as a second wind source (experimental, off by default).
- Events are tentative, show as free, have no reminders, and live in their own calendar ("תחזית ים").
- Python 3.9+, standard library only. Runs on macOS (launchd) or Linux (cron).

## Quick start

```
python3 scripts/sea_calendar.py init        # creates ~/.sea-calendar/
# one-time Google access: see references/google-setup.md
python3 ~/.sea-calendar/sea_calendar.py auth
python3 ~/.sea-calendar/sea_calendar.py check --hourly
python3 ~/.sea-calendar/sea_calendar.py sync --mode week --dry-run
python3 ~/.sea-calendar/sea_calendar.py install-schedule
```

Settings live in `~/.sea-calendar/config.json`; only keys that differ from the defaults are needed.
Running on a Linux server instead of a laptop: see `SKILL.md`.

`SKILL.md` and `references/` also work as a Claude skill (folder name `sea-conditions-calendar`).

## Security

You create your own Google OAuth client. Never commit `client_secret.json`, `token.json` or `config.json`
(the `.gitignore` already excludes them).

## Tests

```
python3 -m unittest discover -s tests
```

Forecasts are not safety advice: check real conditions before going on the water.
