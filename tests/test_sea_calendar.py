import datetime as dt
import itertools
import os
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import sea_calendar as sc  # noqa: E402

TZ = ZoneInfo("Asia/Jerusalem")
DAY = dt.date(2026, 10, 15)


def at(h, m=0, day=DAY):
    return dt.datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


def make_data(wind_by_hour, wave_by_hour=None, extra_sources=None, day=DAY):
    """wind_by_hour / wave_by_hour: {hour_int: value}; sunrise 06:20, sunset 17:40."""
    d = {"wind": {"open_meteo": {at(h, day=day): {"kn": v, "dir": 270, "gust": v + 3}
                                for h, v in wind_by_hour.items()}},
         "waves": {at(h, day=day): {"m": v, "period": 6, "dir": 270}
                   for h, v in (wave_by_hour or {}).items()},
         "sun": {day: (at(6, 20, day), at(17, 40, day))}, "warnings": []}
    for name, vals in (extra_sources or {}).items():
        d["wind"][name] = {at(h, day=day): {"kn": v, "dir": 270, "gust": None} for h, v in vals.items()}
    return d


def cfg(**over):
    return sc.deep_merge(sc.DEFAULT_CONFIG, over)


class Windows(unittest.TestCase):
    def test_kite_window_basic_and_threshold_is_strictly_above(self):
        data = make_data({h: 16 for h in range(6, 19)} | {h: 18 for h in (10, 11, 12, 13)})
        w = sc.evaluate(cfg(), data, [DAY])
        kites = [x for x in w if x["kind"] == "kite"]
        self.assertEqual(len(kites), 1)           # 16 kn itself does not count, only >16
        self.assertEqual((kites[0]["start"], kites[0]["end"]), (at(10), at(14)))

    def test_kite_ignores_waves(self):
        data = make_data({h: 20 for h in range(6, 19)}, {h: 2.5 for h in range(6, 19)})
        kites = [x for x in sc.evaluate(cfg(), data, [DAY]) if x["kind"] == "kite"]
        self.assertEqual(len(kites), 1)

    def test_window_clipped_to_daylight(self):
        data = make_data({h: 20 for h in range(0, 24)})
        k = [x for x in sc.evaluate(cfg(), data, [DAY]) if x["kind"] == "kite"][0]
        self.assertEqual(k["start"], at(6, 20))   # sunrise
        self.assertEqual(k["end"], at(18, 10))    # sunset 17:40 + 30 min

    def test_swim_needs_both_calm_wind_and_small_waves(self):
        wind = {h: 8 for h in range(6, 19)}
        ok = make_data(wind, {h: 0.4 for h in range(6, 19)})
        self.assertEqual([x["kind"] for x in sc.evaluate(cfg(), ok, [DAY])], ["swim"])
        bad_wave = make_data(wind, {h: 0.5 for h in range(6, 19)})
        self.assertEqual(sc.evaluate(cfg(), bad_wave, [DAY]), [])
        bad_wind = make_data({h: 11 for h in range(6, 19)}, {h: 0.2 for h in range(6, 19)})
        self.assertEqual(sc.evaluate(cfg(), bad_wind, [DAY]), [])

    def test_swim_without_wave_data_is_not_assumed_ok(self):
        self.assertEqual(sc.evaluate(cfg(), make_data({h: 5 for h in range(6, 19)}), [DAY]), [])

    def test_min_duration_filters_short_windows(self):
        one_hour = make_data({h: 5 for h in range(6, 19)} | {10: 20})
        kites = lambda c: [x for x in sc.evaluate(c, one_hour, [DAY]) if x["kind"] == "kite"]
        self.assertEqual(len(kites(cfg())), 1)                           # exactly 60 min meets the 60 min minimum
        self.assertEqual(len(kites(cfg(min_window_minutes=90))), 0)      # but not a 90 min minimum

    def test_two_separate_windows_in_one_day(self):
        wind = {h: 5 for h in range(6, 19)} | {h: 20 for h in (8, 9, 14, 15, 16)}
        kites = [x for x in sc.evaluate(cfg(), make_data(wind), [DAY]) if x["kind"] == "kite"]
        self.assertEqual([(k["start"].hour, k["end"].hour) for k in kites], [(8, 10), (14, 17)])

    def test_direction_filter(self):
        data = make_data({h: 20 for h in range(6, 19)})
        east = cfg(kite={"direction_filter": {"from_deg": 200, "to_deg": 340}})
        for h in data["wind"]["open_meteo"]:
            data["wind"]["open_meteo"][h]["dir"] = 90   # offshore
        self.assertEqual(sc.evaluate(east, data, [DAY]), [])
        self.assertEqual(len(sc.evaluate(cfg(), data, [DAY])), 1)   # default: no filter


class SourceAgreement(unittest.TestCase):
    def test_agree_partial_single(self):
        wind = {h: 20 for h in range(6, 19)}
        both = make_data(wind, extra_sources={"windguru": wind})
        self.assertEqual(sc.evaluate(cfg(), both, [DAY])[0]["level"], "agree")
        split = make_data(wind, extra_sources={"windguru": {h: 10 for h in range(6, 19)}})
        w = sc.evaluate(cfg(), split, [DAY])[0]
        self.assertEqual(w["level"], "partial")
        self.assertTrue(sc.event_title(cfg(), w).endswith("(מקור אחד)"))
        self.assertEqual(sc.evaluate(cfg(include_partial=False), split, [DAY]), [])
        self.assertEqual(sc.evaluate(cfg(), make_data(wind), [DAY])[0]["level"], "single")

    def test_missing_hours_in_second_source_are_not_a_disagreement(self):
        wind = {h: 20 for h in range(6, 19)}
        data = make_data(wind, extra_sources={"windguru": {h: 20 for h in range(6, 12)}})
        w = sc.evaluate(cfg(), data, [DAY])[0]
        # hours covered by one source only make the window "single", never "partial":
        # missing data is not a disagreement, so no "(מקור אחד)" suffix in the title
        self.assertEqual(w["level"], "single")
        self.assertNotIn("(מקור אחד)", sc.event_title(cfg(), w))


class FakeCal:
    def __init__(self):
        self.events, self.ids = {}, itertools.count(1)
        self.calls = []

    def list_managed(self, cal_id, tmin, tmax):
        return [dict(e) for e in self.events.values()
                if sc.ev_time(e, "start") >= tmin and sc.ev_time(e, "start") < tmax]

    def insert(self, cal_id, body):
        e = dict(body, id=f"e{next(self.ids)}")
        self.events[e["id"]] = e
        self.calls.append("insert")
        return e

    def patch(self, cal_id, eid, body):
        self.events[eid].update(body)
        self.calls.append("patch")

    def delete(self, cal_id, eid):
        del self.events[eid]
        self.calls.append("delete")


NOW = at(5, day=dt.date(2026, 10, 14))


def new_state():
    return {"calendar_id": None, "created": {}, "dismissed": []}


class Sync(unittest.TestCase):
    def run_sync(self, cal, state, data, **kw):
        c = cfg()
        w = sc.evaluate(c, data, [DAY])
        return sc.sync_calendar(cal, "cal", c, state, w, [DAY], kw.get("now", NOW), kw.get("dry", False))

    def test_idempotent(self):
        cal, st = FakeCal(), new_state()
        data = make_data({h: 20 for h in range(6, 19)})
        self.assertEqual([c["type"] for c in self.run_sync(cal, st, data)], ["added"])
        self.assertEqual(self.run_sync(cal, st, data), [])
        self.assertEqual(cal.calls, ["insert"])

    def test_forecast_change_moves_then_removes(self):
        cal, st = FakeCal(), new_state()
        self.run_sync(cal, st, make_data({h: 20 for h in range(9, 15)} | {h: 5 for h in (6, 7, 8, 15, 16, 17, 18)}))
        ch = self.run_sync(cal, st, make_data({h: 20 for h in range(10, 15)} | {h: 5 for h in (6, 7, 8, 9, 15, 16, 17, 18)}))
        self.assertEqual([c["type"] for c in ch], ["moved"])
        self.assertEqual(sc.ev_time(next(iter(cal.events.values())), "start"), at(10))
        ch = self.run_sync(cal, st, make_data({h: 5 for h in range(6, 19)}))
        self.assertEqual([c["type"] for c in ch], ["removed"])
        self.assertEqual(cal.events, {})

    def test_user_deleted_event_is_not_recreated(self):
        cal, st = FakeCal(), new_state()
        data = make_data({h: 20 for h in range(6, 19)})
        self.run_sync(cal, st, data)
        cal.events.clear()                       # user deletes it in Google Calendar
        self.assertEqual(self.run_sync(cal, st, data), [])
        self.assertEqual(cal.events, {})
        self.assertIn(f"{DAY.isoformat()}|kite", st["dismissed"])

    def test_foreign_events_untouched_and_dry_run_writes_nothing(self):
        cal, st = FakeCal(), new_state()
        cal.events["mine"] = {"id": "mine", "summary": "dentist",
                              "start": {"dateTime": at(10).isoformat()}, "end": {"dateTime": at(11).isoformat()}}
        # list_managed is filtered server-side by the seaCal marker; emulate that
        cal.list_managed = lambda *a, **k: []
        ch = self.run_sync(cal, st, make_data({h: 20 for h in range(6, 19)}), dry=True)
        self.assertEqual([c["type"] for c in ch], ["added"])
        self.assertEqual(list(cal.events), ["mine"])

    def test_past_windows_are_not_added_or_deleted(self):
        cal, st = FakeCal(), new_state()
        late = at(19)   # after sunset window ended
        self.assertEqual(self.run_sync(cal, st, make_data({h: 20 for h in range(6, 19)}), now=late), [])
        self.run_sync(cal, st, make_data({h: 20 for h in range(6, 19)}))
        self.assertEqual(self.run_sync(cal, st, make_data({h: 5 for h in range(6, 19)}), now=late), [])
        self.assertEqual(len(cal.events), 1)


class CronAndGate(unittest.TestCase):
    def test_should_run_gate(self):
        sunday_6 = dt.datetime(2026, 10, 18, 6, 5, tzinfo=TZ)   # Sunday
        self.assertTrue(sc.should_run(sunday_6, 6, 0))
        self.assertFalse(sc.should_run(sunday_6, 7, 0))
        self.assertFalse(sc.should_run(sunday_6, 6, 1))
        self.assertTrue(sc.should_run(dt.datetime(2026, 10, 19, 6, 5, tzinfo=TZ), 6, 1))  # Monday = 1
        self.assertTrue(sc.should_run(sunday_6))                 # no gate = always run

    def test_cron_block_roundtrip(self):
        blk = sc.cron_block(cfg(), "/home/u/.sea-calendar/sea_calendar.py", "/usr/bin/python3", "/home/u/.sea-calendar/logs")
        lines = [l for l in blk.splitlines() if not l.startswith("#")]
        self.assertEqual(len(lines), 3)
        self.assertIn("--mode week --only-hour 8 --only-weekday 0", lines[0])
        self.assertIn("--mode today --only-hour 6", lines[1])
        self.assertIn("--mode tomorrow --only-hour 20", lines[2])
        existing = "MAILTO=me\n0 5 * * * backup.sh\n"
        merged = existing + "\n" + blk
        self.assertEqual(sc.strip_cron_block(merged).strip(), existing.strip())   # other jobs survive removal
        again = sc.strip_cron_block(merged) + "\n" + blk                          # re-install replaces, not duplicates
        self.assertEqual(again.count(sc.CRON_BEGIN), 1)

    def test_strip_on_empty(self):
        self.assertEqual(sc.strip_cron_block(""), "")


class Helpers(unittest.TestCase):
    def test_compass_and_mean_dir(self):
        self.assertEqual(sc.compass(270), "W")
        self.assertEqual(sc.compass(355), "N")
        self.assertAlmostEqual(sc.mean_dir([350, 10]) % 360, 0, delta=0.5)

    def test_event_body_marks_and_free_time(self):
        w = sc.evaluate(cfg(), make_data({h: 20 for h in range(6, 19)}), [DAY])[0]
        b = sc.event_body(cfg(), w)
        self.assertEqual(b["extendedProperties"]["private"]["seaCal"], "1")
        self.assertEqual(b["transparency"], "transparent")
        self.assertEqual(b["status"], "tentative")
        self.assertIn("קייט", b["summary"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
