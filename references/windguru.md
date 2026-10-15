# Windguru WRF-9 as a second wind source (optional, experimental)

Read this only when the user wants the WRF 9 km comparison, or asks why it is off.

## Status: not verified

Windguru shows a model called **WRF 9 km** (the KDI site lists it as "Windguru WRF-9" for the Mediterranean coast). Windguru loads its forecast in the browser and publishes no documented public API for it. When this skill was written, no working endpoint could be confirmed, because the build environment cannot reach Windguru. So the adapter in `sea_calendar.py` is written against an assumed response shape and **has never been run against the real site**. Treat the first test as a real test.

Safeguards that keep this from hurting anything:

- it is off by default (`sources.windguru.enabled: false`)
- any failure (bad URL, blocked, unexpected shape) is caught: the run prints `Windguru skipped: ...` and continues with Open-Meteo alone
- Windguru's terms may limit automated access, so the user should read them and keep the schedule light (3 runs a day is the maximum this tool makes)

## Finding the endpoint (do this with the user, on their Mac)

1. Open the Windguru page for a spot near Dor in Chrome and select the **WRF 9 km** model. Windguru spot ids are the number in the URL (`windguru.cz/734` is Haifa, which is far from Dor, so find the closest spot on Windguru's map or ask the user which spot they use).
2. Open DevTools → Network, reload, filter for `iapi` or `forecast`, and find the request whose response is JSON containing `WINDSPD`, `WINDDIR` and `hours`.
3. If the built-in browser is available in the session, `read_network_requests` can capture this instead.
4. Copy the request URL. Note any header the site needs (often `Referer: https://www.windguru.cz/<spot_id>`).

## What the adapter expects

A JSON object with `fcst` (or the top level) holding `hours` (offsets in hours), `WINDSPD`, `WINDDIR`, optional `GUST`, and `initstamp` (unix seconds of the model run). Times are `initstamp + hours * 3600`. Speeds are assumed to be **knots**; if the response is in m/s set `speed_multiplier` to `1.94384`.

If the real response looks different, change `fetch_windguru` in `sea_calendar.py` (it is about 20 lines) rather than guessing around it.

## Enabling it

In `~/.sea-calendar/config.json`:

```json
{
  "sources": {
    "windguru": {
      "enabled": true,
      "url_template": "<the URL you copied; {spot_id} and {model_id} are filled in if present>",
      "spot_id": 12345,
      "model_id": null,
      "headers": {"Referer": "https://www.windguru.cz/12345"},
      "speed_multiplier": 1.0
    }
  }
}
```

Then run `python3 ~/.sea-calendar/sea_calendar.py check --hourly`. Each hour shows both sources' wind side by side, which is the way to confirm that the numbers are sensible (compare against what the Windguru page shows for the same hours). Only after that run `sync`.
