#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sea_calendar.py - find kite / swim windows from wave + wind forecasts and keep
them in a Google Calendar.

Standard library only (Python 3.9+), so it runs on a stock Mac with no installs.

Commands
  init                 create ~/.sea-calendar/ with config + a stable copy of this script
  auth                 one-time Google sign-in (needs ~/.sea-calendar/client_secret.json)
  check                print the assessment, touch nothing
  sync --mode M        write to the calendar; M = week | today | tomorrow
  install-schedule     (macOS) install launchd jobs for the three runs
  uninstall-schedule   remove them
"""
import argparse
import copy
import datetime as dt
import http.server
import json
import math
import os
import plistlib
import secrets
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    sys.exit("sea_calendar.py needs Python 3.9 or newer (zoneinfo). Check with: python3 --version")

HOME =os.path.expanduser("~/.sea-calendar")
CONFIG_PATH = os.path.join(HOME, "config.json")
STATE_PATH = os.path.join(HOME, "state.json")
TOKEN_PATH = os.path.join(HOME, "token.json")
CLIENT_PATH = os.path.join(HOME, "client_secret.json")
LOG_DIR = os.path.join(HOME, "logs")

DEFAULT_CONFIG = {
    "timezone": "Asia/Jerusalem",
    "location": {
        "name": "חוף דור",
        "lat": 32.614, "lon": 34.910,          # wind point (beach)
        "wave_lat": 32.62, "wave_lon": 34.88,  # wave point (a little offshore, so the grid cell is sea)
    },
    "horizon_days": 7,
    # Window = sunrise + start_offset .. sunset + end_offset
    "daylight": {"start_offset_min": 0, "end_offset_min": 30},
    "min_window_minutes": 60,
    "kite": {"min_wind_kn": 16, "direction_filter": None},  # filter: {"from_deg": 200, "to_deg": 340}
    "swim": {"max_wave_m": 0.4, "max_wind_kn": 10},
    "sources": {
        "open_meteo": {"enabled": True},
        # Experimental, off by default. See references/windguru.md for how to enable it.
        "windguru": {"enabled": False, "url_template": None, "spot_id": None,
                     "model_id": None, "headers": {}, "speed_multiplier": 1.0},
    },
    # When two wind sources disagree: keep the window but mark it "one source only"
    "include_partial": True,
    "calendar": {"calendar_name": "תחזית ים", "calendar_id": None},
    "titles": {"kite": "🪁 קייט – הערכה", "swim": "🏊 שחייה – הערכה",
               "partial_suffix": " (מקור אחד)"},
    "event_status": "tentative",
    "notify": True,
    "schedule": {
        "week": {"weekday": 0, "hour": 8, "minute": 0},     # Sunday 08:00 (launchd: 0 = Sunday)
        "today": {"hour": 6, "minute": 0},                  # every morning
        "tomorrow": {"hour": 20, "minute": 0},              # every evening
    },
}

GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
CAL_API = "https://www.googleapis.com/calendar/v3"

KIND_ORDER = ("kite", "swim")
COLORS = {"kite": "9", "swim": "7"}
HOUR = dt.timedelta(hours=1)


# --------------------------------------------------------------------------- config / state

def deep_merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    user = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
    return deep_merge(DEFAULT_CONFIG, user)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"calendar_id": None, "created": {}, "dismissed": []}


def save_state(state):
    os.makedirs(HOME, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def log(msg):
    print(f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- fetching

def ssl_context():
    """A Python installed from python.org on macOS ships with no CA certificates, so every HTTPS call
    fails with CERTIFICATE_VERIFY_FAILED. Adding the macOS system bundle fixes that and is harmless
    elsewhere. Verification stays on."""
    ctx = ssl.create_default_context()
    for path in ("/etc/ssl/cert.pem",):
        if os.path.exists(path):
            try:
                ctx.load_verify_locations(path)
            except (ssl.SSLError, OSError):
                pass
    return ctx


def http_json(url, headers=None, timeout=30, data=None, method=None):
    h = {"User-Agent": "sea-calendar/1.0"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} for {url.split('?')[0]}: {e.read().decode('utf-8', 'replace')[:300]}")


def local_dt(s, tz):
    return dt.datetime.fromisoformat(s).replace(tzinfo=tz)


def fetch_open_meteo(cfg, tz):
    """Wind (knots) + sunrise/sunset from the forecast API, waves from the marine API."""
    loc = cfg["location"]
    days = cfg["horizon_days"] + 2
    wind = http_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": loc["lat"], "longitude": loc["lon"],
        "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "daily": "sunrise,sunset", "wind_speed_unit": "kn",
        "timezone": cfg["timezone"], "forecast_days": days}))
    marine = http_json("https://marine-api.open-meteo.com/v1/marine?" + urllib.parse.urlencode({
        "latitude": loc["wave_lat"], "longitude": loc["wave_lon"],
        "hourly": "wave_height,wave_period,wave_direction",
        "timezone": cfg["timezone"], "forecast_days": days}))

    hw = wind["hourly"]
    wind_by_hour = {}
    for i, t in enumerate(hw["time"]):
        wind_by_hour[local_dt(t, tz)] = {
            "kn": hw["wind_speed_10m"][i], "dir": hw["wind_direction_10m"][i],
            "gust": hw["wind_gusts_10m"][i]}

    hm = marine["hourly"]
    waves = {}
    for i, t in enumerate(hm["time"]):
        waves[local_dt(t, tz)] = {
            "m": hm["wave_height"][i], "period": hm["wave_period"][i],
            "dir": hm["wave_direction"][i]}

    sun = {}
    for i, d in enumerate(wind["daily"]["time"]):
        sun[dt.date.fromisoformat(d)] = (local_dt(wind["daily"]["sunrise"][i], tz),
                                         local_dt(wind["daily"]["sunset"][i], tz))
    return {"wind": wind_by_hour, "waves": waves, "sun": sun}


def fetch_windguru(cfg, tz):
    """Experimental. Windguru has no documented public API, so the URL comes from config
    (see references/windguru.md). Any failure just drops this source for the run."""
    wg = cfg["sources"]["windguru"]
    url = (wg.get("url_template") or "").format(spot_id=wg.get("spot_id"), model_id=wg.get("model_id"))
    data = http_json(url, headers=wg.get("headers"))
    f = data.get("fcst") or data
    hours, spd, wdir = f.get("hours"), f.get("WINDSPD"), f.get("WINDDIR")
    init = f.get("initstamp") or data.get("initstamp")
    if not (hours and spd and init):
        raise ValueError("unexpected Windguru response shape (need hours, WINDSPD, initstamp)")
    mult = float(wg.get("speed_multiplier", 1.0))
    gust = f.get("GUST") or [None] * len(hours)
    out = {}
    for i, h in enumerate(hours):
        when = dt.datetime.fromtimestamp(int(init) + int(h) * 3600, tz).replace(minute=0, second=0, microsecond=0)
        if spd[i] is None:
            continue
        out[when] = {"kn": spd[i] * mult, "dir": (wdir[i] if wdir else None),
                     "gust": (gust[i] * mult if gust[i] is not None else None)}
    return out


def fetch_all(cfg):
    tz = ZoneInfo(cfg["timezone"])
    data = {"wind": {}, "waves": {}, "sun": {}, "warnings": []}
    srcs = cfg["sources"]
    if srcs["open_meteo"]["enabled"]:
        om = fetch_open_meteo(cfg, tz)   # essential: let it raise
        data["wind"]["open_meteo"] = om["wind"]
        data["waves"], data["sun"] = om["waves"], om["sun"]
    if srcs["windguru"]["enabled"]:
        try:
            data["wind"]["windguru"] = fetch_windguru(cfg, tz)
        except Exception as e:  # experimental source must never break the run
            data["warnings"].append(f"Windguru skipped: {e}")
    if not data["wind"]:
        raise RuntimeError("no wind source is enabled")
    return data


# --------------------------------------------------------------------------- evaluation

def direction_ok(deg, flt):
    if not flt or deg is None:
        return True
    a, b = flt["from_deg"] % 360, flt["to_deg"] % 360
    deg %= 360
    return a <= deg <= b if a <= b else (deg >= a or deg <= b)


def compass(deg):
    if deg is None:
        return "?"
    names = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return names[int((deg % 360) / 22.5 + 0.5) % 16]


def mean_dir(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    s = sum(math.sin(math.radians(v)) for v in vals)
    c = sum(math.cos(math.radians(v)) for v in vals)
    return (math.degrees(math.atan2(s, c)) + 360) % 360


def hour_levels(cfg, data, h):
    """For one hour: level per kind. 'single' (one source, passes), 'agree' (all sources pass),
    'partial' (some pass), None (nothing passes or not enough data)."""
    srcs = {n: w[h] for n, w in data["wind"].items() if h in w and w[h].get("kn") is not None}
    if not srcs:
        return {"kite": None, "swim": None}
    kite = cfg["kite"]
    kite_pass = [n for n, v in srcs.items()
                 if v["kn"] > kite["min_wind_kn"] and direction_ok(v.get("dir"), kite.get("direction_filter"))]
    wave = data["waves"].get(h)
    swim_pass = []
    if wave and wave.get("m") is not None and wave["m"] <= cfg["swim"]["max_wave_m"]:
        swim_pass = [n for n, v in srcs.items() if v["kn"] <= cfg["swim"]["max_wind_kn"]]

    def level(passing):
        if not passing:
            return None
        if len(passing) == len(srcs):
            return "agree" if len(srcs) > 1 else "single"
        return "partial"
    return {"kite": level(kite_pass), "swim": level(swim_pass)}


def window_stats(data, hours):
    stats = {"wind": {}, "wave": None}
    for name, w in data["wind"].items():
        pts = [w[h] for h in hours if h in w and w[h].get("kn") is not None]
        if pts:
            stats["wind"][name] = {
                "avg": sum(p["kn"] for p in pts) / len(pts),
                "max": max(p["kn"] for p in pts),
                "dir": mean_dir([p.get("dir") for p in pts])}
    wv = [data["waves"][h]["m"] for h in hours if h in data["waves"] and data["waves"][h].get("m") is not None]
    if wv:
        stats["wave"] = {"avg": sum(wv) / len(wv), "max": max(wv)}
    return stats


WEAKEST = {"partial": 0, "single": 1, "agree": 2}


def day_windows(cfg, data, date):
    if date not in data["sun"]:
        return []
    sunrise, sunset = data["sun"][date]
    start = sunrise + dt.timedelta(minutes=cfg["daylight"]["start_offset_min"])
    end = sunset + dt.timedelta(minutes=cfg["daylight"]["end_offset_min"])
    min_len = dt.timedelta(minutes=cfg["min_window_minutes"])
    done, cur = [], {}

    def close(kind):
        c = cur.pop(kind, None)
        if c and c["end"] - c["start"] >= min_len:
            c["level"] = min(c["levels"], key=lambda l: WEAKEST[l])
            c["stats"] = window_stats(data, c["hours"])
            done.append(c)

    h = start.replace(minute=0, second=0, microsecond=0)
    while h < end:
        seg_s, seg_e = max(h, start), min(h + HOUR, end)
        lv = hour_levels(cfg, data, h)
        for kind in KIND_ORDER:
            level = lv[kind]
            ok = level in ("agree", "single") or (level == "partial" and cfg["include_partial"])
            c = cur.get(kind)
            if ok and c and c["end"] == seg_s:
                c["end"] = seg_e
                c["hours"].append(h)
                c["levels"].append(level)
            else:
                if c:
                    close(kind)
                if ok:
                    cur[kind] = {"kind": kind, "date": date, "start": seg_s, "end": seg_e,
                                 "hours": [h], "levels": [level]}
        h += HOUR
    for kind in KIND_ORDER:
        close(kind)
    return sorted(done, key=lambda w: (w["start"], w["kind"]))


def evaluate(cfg, data, dates):
    out = []
    for d in dates:
        out.extend(day_windows(cfg, data, d))
    return out


# --------------------------------------------------------------------------- event content

def fmt_stats(w):
    lines = []
    labels = {"open_meteo": "Open-Meteo", "windguru": "Windguru WRF-9"}
    for name, s in w["stats"]["wind"].items():
        lines.append(f"רוח {labels.get(name, name)}: ממוצע {s['avg']:.0f} קשר, מקס׳ {s['max']:.0f}, "
                     f"כיוון {compass(s['dir'])}")
    if w["stats"]["wave"]:
        wv = w["stats"]["wave"]
        lines.append(f"גל: ממוצע {wv['avg']:.1f} מ׳, מקס׳ {wv['max']:.1f}")
    return lines


def event_title(cfg, w):
    t = cfg["titles"][w["kind"]]
    return t + (cfg["titles"]["partial_suffix"] if w["level"] == "partial" else "")


def event_description(cfg, w):
    lines = fmt_stats(w)
    notes = {"agree": "כל מקורות הרוח מסכימים.",
             "partial": "רק חלק ממקורות הרוח עומדים בתנאים – כדאי לבדוק שוב קרוב לזמן.",
             "single": ""}
    if notes[w["level"]]:
        lines.append(notes[w["level"]])
    lines.append("נוצר ומתעדכן אוטומטית (sea-calendar). אפשר למחוק אירוע – הוא לא יחזור.")
    return "\n".join(lines)


def event_body(cfg, w):
    tz = cfg["timezone"]
    return {
        "summary": event_title(cfg, w),
        "description": event_description(cfg, w),
        "start": {"dateTime": w["start"].isoformat(), "timeZone": tz},
        "end": {"dateTime": w["end"].isoformat(), "timeZone": tz},
        "transparency": "transparent",
        "status": cfg["event_status"],
        "colorId": COLORS[w["kind"]],
        "reminders": {"useDefault": False},
        "extendedProperties": {"private": {"seaCal": "1", "kind": w["kind"], "date": w["date"].isoformat()}},
    }


# --------------------------------------------------------------------------- Google Calendar

class GCal:
    def __init__(self):
        self._access = None
        self._exp = 0

    def _client(self):
        with open(CLIENT_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        return raw.get("installed") or raw.get("web")

    def _token(self):
        if self._access and time.time() < self._exp - 60:
            return self._access
        if not os.path.exists(TOKEN_PATH):
            raise RuntimeError("not signed in - run: python3 sea_calendar.py auth")
        with open(TOKEN_PATH, encoding="utf-8") as f:
            tok = json.load(f)
        c = self._client()
        body = urllib.parse.urlencode({
            "client_id": c["client_id"], "client_secret": c["client_secret"],
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"}).encode()
        try:
            r = http_json(TOKEN_URI, data=body, method="POST",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
        except RuntimeError as e:
            if "invalid_grant" in str(e):
                raise RuntimeError("Google sign-in expired - run: python3 sea_calendar.py auth "
                                   "(if this repeats every week, publish the OAuth app, see references/google-setup.md)")
            raise
        self._access = r["access_token"]
        self._exp = time.time() + int(r.get("expires_in", 3600))
        return self._access

    def _req(self, method, path, params=None, body=None):
        url = CAL_API + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        return http_json(url, method=method, data=data, headers={
            "Authorization": "Bearer " + self._token(), "Content-Type": "application/json"})

    def ensure_calendar(self, cfg, state):
        c = cfg["calendar"]
        if c.get("calendar_id"):
            return c["calendar_id"]
        if state.get("calendar_id"):
            return state["calendar_id"]
        name = c["calendar_name"]
        token = None
        while True:
            r = self._req("GET", "/users/me/calendarList", {"pageToken": token} if token else None)
            for item in r.get("items", []):
                if item.get("summary") == name and item.get("accessRole") == "owner":
                    state["calendar_id"] = item["id"]
                    return item["id"]
            token = r.get("nextPageToken")
            if not token:
                break
        created = self._req("POST", "/calendars", body={"summary": name, "timeZone": cfg["timezone"]})
        state["calendar_id"] = created["id"]
        log(f"created calendar '{name}'")
        return created["id"]

    def list_managed(self, cal_id, tmin, tmax):
        out, token = [], None
        while True:
            p = {"privateExtendedProperty": "seaCal=1", "timeMin": tmin.isoformat(),
                 "timeMax": tmax.isoformat(), "singleEvents": "true", "maxResults": 250}
            if token:
                p["pageToken"] = token
            r = self._req("GET", f"/calendars/{urllib.parse.quote(cal_id)}/events", p)
            out.extend(r.get("items", []))
            token = r.get("nextPageToken")
            if not token:
                return out

    def insert(self, cal_id, body):
        return self._req("POST", f"/calendars/{urllib.parse.quote(cal_id)}/events", body=body)

    def patch(self, cal_id, event_id, body):
        return self._req("PATCH", f"/calendars/{urllib.parse.quote(cal_id)}/events/{event_id}", body=body)

    def delete(self, cal_id, event_id):
        return self._req("DELETE", f"/calendars/{urllib.parse.quote(cal_id)}/events/{event_id}")


# --------------------------------------------------------------------------- sync

def ev_time(e, key):
    return dt.datetime.fromisoformat(e[key]["dateTime"])


def overlap(a_s, a_e, b_s, b_e):
    return max(dt.timedelta(0), min(a_e, b_e) - max(a_s, b_s))


def sync_calendar(cal, cal_id, cfg, state, windows, dates, now, dry_run=False):
    """Make the managed events for `dates` match `windows`. Returns a list of change dicts.
    - events we did not create (no seaCal marker) are never touched
    - an event the user deleted is remembered and not recreated
    - events that already ended are left alone"""
    tz = ZoneInfo(cfg["timezone"])
    tmin = dt.datetime.combine(min(dates), dt.time(0), tz)
    tmax = dt.datetime.combine(max(dates) + dt.timedelta(days=1), dt.time(0), tz)
    existing = cal.list_managed(cal_id, tmin, tmax)
    live_ids = {e["id"] for e in existing}
    date_set = {d.isoformat() for d in dates}

    # detect user deletions: we created it, it is in range, it is gone
    for eid, sig in list(state["created"].items()):
        day = sig.split("|")[0]
        if day in date_set and eid not in live_ids:
            if sig not in state["dismissed"]:
                state["dismissed"].append(sig)
            del state["created"][eid]

    changes = []
    for d in dates:
        for kind in KIND_ORDER:
            sig = f"{d.isoformat()}|{kind}"
            want = [w for w in windows if w["date"] == d and w["kind"] == kind]
            have = [e for e in existing
                    if e.get("extendedProperties", {}).get("private", {}).get("date") == d.isoformat()
                    and e.get("extendedProperties", {}).get("private", {}).get("kind") == kind]
            used = set()
            for w in want:
                best, best_ov = None, dt.timedelta(0)
                for e in have:
                    if e["id"] in used:
                        continue
                    ov = overlap(w["start"], w["end"], ev_time(e, "start"), ev_time(e, "end"))
                    if ov > best_ov:
                        best, best_ov = e, ov
                body = event_body(cfg, w)
                if best:
                    used.add(best["id"])
                    moved = (ev_time(best, "start") != w["start"] or ev_time(best, "end") != w["end"])
                    retitled = best.get("summary") != body["summary"]
                    redescribed = best.get("description") != body["description"]
                    if moved or retitled or redescribed:
                        if not dry_run:
                            cal.patch(cal_id, best["id"], body)
                        changes.append({"type": "moved" if moved else ("level" if retitled else "details"),
                                        "kind": kind, "date": d, "window": w})
                else:
                    if sig in state["dismissed"] or w["end"] <= now:
                        continue
                    if not dry_run:
                        created = cal.insert(cal_id, body)
                        state["created"][created["id"]] = sig
                    changes.append({"type": "added", "kind": kind, "date": d, "window": w})
            for e in have:
                if e["id"] in used or ev_time(e, "end") <= now:
                    continue
                if not dry_run:
                    cal.delete(cal_id, e["id"])
                    state["created"].pop(e["id"], None)
                changes.append({"type": "removed", "kind": kind, "date": d, "event": e})

    # forget dismissals for days that are over
    today = now.date().isoformat()
    state["dismissed"] = [s for s in state["dismissed"] if s.split("|")[0] >= today]
    return changes


def describe_change(c):
    names = {"kite": "קייט", "swim": "שחייה"}
    d = c["date"].strftime("%d/%m")
    kind = names[c["kind"]]
    if c["type"] == "removed":
        e = c["event"]
        return f"{d} {kind}: בוטל החלון {ev_time(e, 'start').strftime('%H:%M')}-{ev_time(e, 'end').strftime('%H:%M')}"
    w = c["window"]
    span = f"{w['start'].strftime('%H:%M')}-{w['end'].strftime('%H:%M')}"
    verb = {"added": "נוסף", "moved": "עודכן", "level": "עודכנה רמת הוודאות", "details": "עודכנו פרטים"}[c["type"]]
    return f"{d} {kind}: {verb} {span}"


def notify(title, message):
    if sys.platform != "darwin":
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"'],
                   check=False)


# --------------------------------------------------------------------------- commands

def dates_for_mode(cfg, mode, today):
    if mode == "week":
        return [today + dt.timedelta(days=i) for i in range(cfg["horizon_days"])]
    if mode == "today":
        return [today]
    if mode == "tomorrow":
        return [today + dt.timedelta(days=1)]
    raise ValueError(mode)


def print_check(cfg, data, windows, dates, hourly=False):
    for w in data["warnings"]:
        print("!", w)
    kinds = {"kite": "קייט", "swim": "שחייה"}
    lv = {"agree": "כל המקורות מסכימים", "single": "", "partial": "מקור אחד בלבד"}
    for d in dates:
        day = [w for w in windows if w["date"] == d]
        print(f"\n{d.strftime('%a %d/%m')}")
        if d in data["sun"]:
            s, e = data["sun"][d]
            print(f"  אור: {s.strftime('%H:%M')} - {e.strftime('%H:%M')} (שקיעה)")
        if not day:
            print("  אין חלון מתאים")
        for w in day:
            print(f"  {kinds[w['kind']]:6} {w['start'].strftime('%H:%M')}-{w['end'].strftime('%H:%M')}  {lv[w['level']]}")
            for line in fmt_stats(w):
                print("         " + line)
        if hourly and d in data["sun"]:
            s, e = data["sun"][d]
            h = s.replace(minute=0, second=0, microsecond=0)
            while h < e:
                parts = [f"{n}:{data['wind'][n][h]['kn']:.0f}kn" for n in data["wind"] if h in data["wind"][n]]
                wv = data["waves"].get(h, {}).get("m")
                lvl = hour_levels(cfg, data, h)
                print(f"    {h.strftime('%H:%M')}  {'  '.join(parts)}  גל {wv if wv is None else format(wv, '.1f')}  "
                      f"קייט={lvl['kite'] or '-'} שחייה={lvl['swim'] or '-'}")
                h += HOUR


def cmd_check(args):
    cfg = load_config()
    tz = ZoneInfo(cfg["timezone"])
    today = dt.datetime.now(tz).date()
    n = args.days or cfg["horizon_days"]
    dates = [today + dt.timedelta(days=i) for i in range(n)]
    data = fetch_all(cfg)
    print_check(cfg, data, evaluate(cfg, data, dates), dates, hourly=args.hourly)


def should_run(now, only_hour=None, only_weekday=None):
    """Gate for hourly cron: weekday uses 0 = Sunday, like launchd."""
    if only_hour is not None and now.hour != only_hour:
        return False
    if only_weekday is not None and (now.weekday() + 1) % 7 != only_weekday:
        return False
    return True


def cmd_sync(args):
    cfg = load_config()
    tz = ZoneInfo(cfg["timezone"])
    now = dt.datetime.now(tz)
    if not should_run(now, args.only_hour, args.only_weekday):
        return
    dates = dates_for_mode(cfg, args.mode, now.date())
    data = fetch_all(cfg)
    for w in data["warnings"]:
        log("warning: " + w)
    windows = evaluate(cfg, data, dates)
    state = load_state()
    cal = GCal()
    cal_id = cal.ensure_calendar(cfg, state)
    changes = sync_calendar(cal, cal_id, cfg, state, windows, dates, now, dry_run=args.dry_run)
    if not args.dry_run:
        save_state(state)
    prefix = "[dry-run] " if args.dry_run else ""
    if not changes:
        log(f"{prefix}{args.mode}: no changes ({len(windows)} windows)")
        return
    lines = [describe_change(c) for c in changes]
    for line in lines:
        log(prefix + line)
    significant = [describe_change(c) for c in changes if c["type"] != "details"]
    if cfg["notify"] and significant and not args.dry_run:
        notify("תחזית ים עודכנה", "\n".join(significant[:4]))


def cmd_init(_args):
    os.makedirs(LOG_DIR, exist_ok=True)
    target = os.path.join(HOME, "sea_calendar.py")
    if os.path.abspath(__file__) != target:
        shutil.copy(os.path.abspath(__file__), target)
    os.chmod(target, 0o755)
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"wrote {CONFIG_PATH}")
    print(f"script copy: {target}")
    print(f"next: put the Google OAuth file at {CLIENT_PATH}, then run: python3 {target} auth")


def cmd_auth(_args):
    if not os.path.exists(CLIENT_PATH):
        sys.exit(f"missing {CLIENT_PATH} - see references/google-setup.md")
    with open(CLIENT_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    c = raw.get("installed") or raw.get("web")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    redirect = f"http://127.0.0.1:{port}"
    nonce = secrets.token_urlsafe(16)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [None])[0] == nonce and "code" in q:
                result["code"] = q["code"][0]
                msg = "Signed in. You can close this tab."
            else:
                result["error"] = q.get("error", ["bad request"])[0]
                msg = "Sign-in failed. Go back to the terminal."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg.encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    url = AUTH_URI + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": GOOGLE_SCOPE, "access_type": "offline", "prompt": "consent", "state": nonce})
    print("opening browser for Google sign-in...\nif it does not open, visit:\n" + url)
    webbrowser.open(url)
    for _ in range(300):
        if result:
            break
        time.sleep(1)
    srv.server_close()
    if "code" not in result:
        sys.exit("sign-in did not complete: " + result.get("error", "timeout"))
    r = http_json(TOKEN_URI, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"},
                  data=urllib.parse.urlencode({
                      "code": result["code"], "client_id": c["client_id"], "client_secret": c["client_secret"],
                      "redirect_uri": redirect, "grant_type": "authorization_code"}).encode())
    if "refresh_token" not in r:
        sys.exit("Google did not return a refresh token; remove the app under myaccount.google.com/permissions and retry")
    os.makedirs(HOME, exist_ok=True)
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump({"refresh_token": r["refresh_token"]}, f)
    os.chmod(TOKEN_PATH, 0o600)
    print("signed in; token saved")


LABELS = {"week": "com.seacal.week", "today": "com.seacal.today", "tomorrow": "com.seacal.tomorrow"}


def install_launchd():
    cfg = load_config()
    script = os.path.join(HOME, "sea_calendar.py")
    if not os.path.exists(script):
        sys.exit("run init first")
    agents = os.path.expanduser("~/Library/LaunchAgents")
    os.makedirs(agents, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    uid = os.getuid()
    for mode, label in LABELS.items():
        when = {k: v for k, v in {"Weekday": cfg["schedule"][mode].get("weekday"),
                                  "Hour": cfg["schedule"][mode]["hour"],
                                  "Minute": cfg["schedule"][mode]["minute"]}.items() if v is not None}
        plist = {
            "Label": label,
            "ProgramArguments": [sys.executable, script, "sync", "--mode", mode],
            "StartCalendarInterval": when,
            "StandardOutPath": os.path.join(LOG_DIR, f"{mode}.log"),
            "StandardErrorPath": os.path.join(LOG_DIR, f"{mode}.log"),
        }
        path = os.path.join(agents, label + ".plist")
        with open(path, "wb") as f:
            plistlib.dump(plist, f)
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", path], check=False, capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", path], capture_output=True, text=True)
        print(f"{mode}: {'installed' if r.returncode == 0 else 'FAILED ' + r.stderr.strip()}  ({when})")


CRON_BEGIN = "# >>> sea-calendar (managed block, do not edit) >>>"
CRON_END = "# <<< sea-calendar <<<"


def cron_block(cfg, script, py, log_dir):
    """Hourly cron lines. The script itself only acts at the configured local hour, so the server's
    timezone, cron's CRON_TZ support and Israel's clock change do not matter."""
    s = cfg["schedule"]

    def line(mode, sched, extra=""):
        return (f"{sched['minute']} * * * * {shlex.quote(py)} {shlex.quote(script)} sync --mode {mode} "
                f"--only-hour {sched['hour']}{extra} >> {shlex.quote(os.path.join(log_dir, mode + '.log'))} 2>&1")
    wk = s["week"]
    return "\n".join([
        CRON_BEGIN,
        f"# cron fires hourly; sea_calendar.py acts only at the local hour set in config ({cfg['timezone']})",
        line("week", wk, f" --only-weekday {wk.get('weekday', 0)}"),
        line("today", s["today"]),
        line("tomorrow", s["tomorrow"]),
        CRON_END]) + "\n"


def strip_cron_block(text):
    out, skipping = [], False
    for ln in text.splitlines():
        if ln.strip() == CRON_BEGIN:
            skipping = True
            continue
        if skipping:
            if ln.strip() == CRON_END:
                skipping = False
            continue
        out.append(ln)
    return "\n".join(out).rstrip("\n") + ("\n" if out else "")


def read_crontab():
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def write_crontab(text):
    r = subprocess.run(["crontab", "-"], input=text, text=True, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("crontab failed: " + r.stderr.strip())


def install_cron():
    if not shutil.which("crontab"):
        sys.exit("crontab not found; install cron (e.g. 'sudo apt install cron' or 'sudo dnf install cronie') and enable it")
    cfg = load_config()
    script = os.path.join(HOME, "sea_calendar.py")
    if not os.path.exists(script):
        sys.exit("run init first")
    os.makedirs(LOG_DIR, exist_ok=True)
    block = cron_block(cfg, script, sys.executable, LOG_DIR)
    base = strip_cron_block(read_crontab())
    write_crontab((base + "\n" if base else "") + block)
    print("cron installed:\n" + block)


def uninstall_cron():
    write_crontab(strip_cron_block(read_crontab()))
    print("removed sea-calendar cron block")


def cmd_install_schedule(_args):
    install_launchd() if sys.platform == "darwin" else install_cron()


def cmd_uninstall_schedule(_args):
    uninstall_launchd() if sys.platform == "darwin" else uninstall_cron()


def uninstall_launchd():
    uid = os.getuid()
    for label in LABELS.values():
        path = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", path], check=False, capture_output=True)
        if os.path.exists(path):
            os.remove(path)
        print("removed", label)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    sub.add_parser("auth").set_defaults(fn=cmd_auth)
    c = sub.add_parser("check")
    c.add_argument("--days", type=int)
    c.add_argument("--hourly", action="store_true")
    c.set_defaults(fn=cmd_check)
    s = sub.add_parser("sync")
    s.add_argument("--mode", choices=["week", "today", "tomorrow"], required=True)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--only-hour", type=int, help="run only when the local hour matches (used by cron)")
    s.add_argument("--only-weekday", type=int, help="run only on this weekday, 0 = Sunday (used by cron)")
    s.set_defaults(fn=cmd_sync)
    sub.add_parser("install-schedule").set_defaults(fn=cmd_install_schedule)
    sub.add_parser("uninstall-schedule").set_defaults(fn=cmd_uninstall_schedule)
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
