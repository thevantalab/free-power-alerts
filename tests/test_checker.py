"""Tests for checker.py. Run with:  python3 -m unittest discover tests"""

import json
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import checker as c  # noqa: E402

UTC = c.UTC


def uk(y, m, d, h, mi=0):
    """A UK wall-clock time as UTC."""
    return datetime(y, m, d, h, mi, tzinfo=c.UK).astimezone(UTC)


def prices(now, until, free=(), price=20.0):
    """Half-hourly EDF prices from a day before `now` up to `until`, 0p inside `free` windows."""
    start = now - timedelta(days=1)
    start = start.replace(minute=0 if start.minute < 30 else 30, second=0, microsecond=0)
    slots = []
    while start < until:
        end = start + timedelta(minutes=30)
        p = 0.0 if any(fs <= start < fe for fs, fe in free) else price
        slots.append(c.Slot(start, end, p))
        start = end
    return slots


def outlook(first_day, high=None, days=7, base=0.05):
    """A 7-day forecast. `high` maps (date, hour) to that hour's chance of being free."""
    high = high or {}
    out = {"days": []}
    for i in range(days):
        d = first_day + timedelta(days=i)
        hours = [{"hour": h, "p_free": high.get((d, h), base)} for h in range(24)]
        out["days"].append({"date": d.isoformat(), "max_p_free": max(x["p_free"] for x in hours), "hours": hours})
    return out


def night(d, p):
    """High chances for 11pm on day d through to 6am the next morning."""
    nxt = d + timedelta(days=1)
    hours = {(d, 23): p}
    hours.update({(nxt, h): p for h in range(6)})
    return hours


def pending(d):
    return {"date": d.isoformat(), "status": "pending"}


def firm(d, free=False, windows=(), marginal=False):
    return {"date": d.isoformat(), "status": "firm", "free": free, "windows": list(windows), "marginal": marginal}


class Sim:
    """Runs the checker repeatedly, carrying state between runs like the real thing."""

    def __init__(self, cfg=None):
        self.cfg = cfg or c.Config()
        self.state = {}

    def run(self, now, rates, forecast, tomorrow):
        state, messages = c.plan(self.state, now, rates, forecast, tomorrow, self.cfg)
        c.apply_quiet_hours(messages, now, self.cfg)
        self.state = json.loads(json.dumps(state))  # must survive being saved to disk
        return messages

    @property
    def events(self):
        return [c.Event.from_dict(e) for e in self.state["events"]]

    def ics(self):
        return c.calendar(self.state["events"])


OCT21, OCT22, OCT23, OCT24, OCT25 = (date(2026, 10, d) for d in (21, 22, 23, 24, 25))


class LastWeekendReplay(unittest.TestCase):
    """Sat 24 Oct 11pm - Sun 25 Oct 6am, the night the clocks went back."""

    def test_heads_up_then_confirmation_then_reminder(self):
        sim = Sim()
        start, end = uk(2026, 10, 24, 23), uk(2026, 10, 25, 6)

        # Wed 22:40: the forecast is 99% for Saturday night, three days out.
        now = uk(2026, 10, 21, 22, 40)
        msgs = sim.run(now, prices(now, uk(2026, 10, 22, 23)), outlook(OCT22, night(OCT24, 0.99)), firm(OCT22))
        self.assertEqual([m.kind for m in msgs], ["heads_up"])
        self.assertEqual(msgs[0].title, "Free electricity possible Saturday night")
        self.assertIn("Sat 24 Oct, 11pm – 6am Sun", msgs[0].body)
        self.assertEqual(msgs[0].delay_until, uk(2026, 10, 22, 7))  # held until Thursday morning
        [event] = sim.events
        self.assertEqual((event.status, event.start, event.end), ("possible", start, end))
        uid = event.uid
        self.assertIn("STATUS:TENTATIVE", sim.ics())

        # Thu 13:40: still looking good - nothing new to say.
        now = uk(2026, 10, 22, 13, 40)
        msgs = sim.run(now, prices(now, uk(2026, 10, 23, 23)), outlook(OCT23, night(OCT24, 0.99)), firm(OCT23))
        self.assertEqual(msgs, [])

        # Fri 22:40: dipped a little, still above the keep level.
        now = uk(2026, 10, 23, 22, 40)
        msgs = sim.run(now, prices(now, uk(2026, 10, 24, 23)), outlook(OCT24, night(OCT24, 0.79)), firm(OCT24))
        self.assertEqual(msgs, [])
        self.assertEqual(sim.events[0].peak, 0.79)

        # Sat 11:00: the auction for Sunday clears at zero overnight.
        now = uk(2026, 10, 24, 11)
        window = {"start": "23:00", "end": "06:00", "start_date": "2026-10-24", "end_date": "2026-10-25"}
        msgs = sim.run(
            now, prices(now, uk(2026, 10, 24, 23)), outlook(OCT24, night(OCT24, 0.79)), firm(OCT25, True, [window])
        )
        self.assertEqual(msgs, [])
        self.assertEqual(sim.events[0].source, "auction")
        self.assertIn("very likely", sim.ics())

        # Sat 12:30: EDF publish Sunday's prices with 0p overnight - confirmed, same calendar entry.
        now = uk(2026, 10, 24, 12, 30)
        rates = prices(now, uk(2026, 10, 25, 23), free=[(start, end)])
        msgs = sim.run(now, rates, outlook(OCT24, night(OCT24, 0.79)), firm(OCT25, True, [window]))
        self.assertEqual([m.kind for m in msgs], ["confirmed"])
        self.assertEqual(msgs[0].title, "Free electricity confirmed tonight")
        self.assertIn("Sat 24 Oct, 11pm – 6am Sun (8 hours)", msgs[0].body)
        self.assertIsNone(msgs[0].delay_until)
        [event] = sim.events
        self.assertEqual((event.uid, event.status), (uid, "confirmed"))
        self.assertIn("STATUS:CONFIRMED", sim.ics())

        # Sat 22:10: reminder, once.
        now = uk(2026, 10, 24, 22, 10)
        rates = prices(now, uk(2026, 10, 25, 23), free=[(start, end)])
        msgs = sim.run(now, rates, outlook(OCT25), firm(OCT25, True, [window]))
        self.assertEqual([m.kind for m in msgs], ["reminder"])
        self.assertEqual(msgs[0].title, "Free electricity starts in about an hour")
        self.assertEqual(msgs[0].body, "Free from 11pm until 6am Sun.")
        now = uk(2026, 10, 24, 22, 25)
        self.assertEqual(sim.run(now, prices(now, uk(2026, 10, 25, 23), free=[(start, end)]), outlook(OCT25), firm(OCT25, True, [window])), [])

        # Sunday evening: it's history now, still in the calendar, and nothing keeps changing.
        before = json.dumps(sim.state)
        for now in (uk(2026, 10, 25, 17), uk(2026, 10, 25, 23, 30)):
            msgs = sim.run(now, prices(now, uk(2026, 10, 26, 23), free=[(start, end)]), outlook(OCT26), firm(OCT26))
            self.assertEqual(msgs, [])
        self.assertEqual(json.dumps(sim.state), before)
        self.assertIn("STATUS:CONFIRMED", sim.ics())


OCT26 = date(2026, 10, 26)
OCT14, OCT15, OCT16, OCT17, OCT18 = (date(2026, 10, d) for d in (14, 15, 16, 17, 18))


def sat_day(p):
    return {(OCT17, h): p for h in range(10, 15)}


class HeadsUpRules(unittest.TestCase):
    def test_false_alarm_fades_and_leaves_the_calendar(self):
        sim = Sim()
        now = uk(2026, 10, 14, 13, 40)
        msgs = sim.run(now, prices(now, uk(2026, 10, 15, 23)), outlook(OCT15, sat_day(0.92)), firm(OCT15))
        self.assertEqual([m.kind for m in msgs], ["heads_up"])
        self.assertEqual(msgs[0].title, "Free electricity possible Saturday")
        self.assertIsNone(msgs[0].delay_until)

        now = uk(2026, 10, 15, 22, 40)
        msgs = sim.run(now, prices(now, uk(2026, 10, 16, 23)), outlook(OCT16, sat_day(0.27)), firm(OCT16))
        self.assertEqual([m.kind for m in msgs], ["faded"])
        self.assertIn("dropped to 27%", msgs[0].body)
        self.assertEqual(msgs[0].delay_until, uk(2026, 10, 16, 7))
        self.assertEqual(sim.events, [])
        self.assertNotIn("BEGIN:VEVENT", sim.ics())

    def test_too_far_ahead_or_too_unlikely_stays_quiet(self):
        now = uk(2026, 10, 14, 13, 40)
        rates = prices(now, uk(2026, 10, 15, 23))
        far = {(date(2026, 10, 19), h): 0.97 for h in range(10, 15)}  # Monday: five days out
        self.assertEqual(Sim().run(now, rates, outlook(OCT15, far), firm(OCT15)), [])
        self.assertEqual(Sim().run(now, rates, outlook(OCT15, sat_day(0.85)), firm(OCT15)), [])

    def test_stays_while_above_keep_level(self):
        sim = Sim()
        now = uk(2026, 10, 14, 13, 40)
        sim.run(now, prices(now, uk(2026, 10, 15, 23)), outlook(OCT15, sat_day(0.92)), firm(OCT15))
        now = uk(2026, 10, 15, 13, 40)
        msgs = sim.run(now, prices(now, uk(2026, 10, 16, 23)), outlook(OCT16, sat_day(0.7)), firm(OCT16))
        self.assertEqual(msgs, [])
        self.assertIn("Possible free electricity (70%)", sim.ics())

    def test_prices_rule_it_out(self):
        sim = Sim()
        now = uk(2026, 10, 14, 13, 40)
        sim.run(now, prices(now, uk(2026, 10, 15, 23)), outlook(OCT15, sat_day(0.92)), firm(OCT15))
        now = uk(2026, 10, 16, 12, 30)  # Friday: Saturday's prices are out, none at 0p
        msgs = sim.run(now, prices(now, uk(2026, 10, 17, 23)), outlook(OCT17, sat_day(0.92)), firm(OCT17))
        self.assertEqual([m.kind for m in msgs], ["not_happening"])
        self.assertEqual(msgs[0].title, "No free electricity tomorrow after all")
        self.assertEqual(sim.events, [])

    def test_auction_rules_it_out_before_prices(self):
        sim = Sim()
        now = uk(2026, 10, 14, 13, 40)
        sim.run(now, prices(now, uk(2026, 10, 15, 23)), outlook(OCT15, sat_day(0.92)), firm(OCT15))
        now = uk(2026, 10, 16, 11)
        msgs = sim.run(now, prices(now, uk(2026, 10, 16, 23)), outlook(OCT16, sat_day(0.92)), firm(OCT17, False))
        self.assertEqual([m.kind for m in msgs], ["faded"])
        self.assertIn("auction didn't clear at zero", msgs[0].body)

    def test_marginal_auction_keeps_it(self):
        sim = Sim()
        now = uk(2026, 10, 14, 13, 40)
        sim.run(now, prices(now, uk(2026, 10, 15, 23)), outlook(OCT15, sat_day(0.92)), firm(OCT15))
        now = uk(2026, 10, 16, 11)
        msgs = sim.run(
            now, prices(now, uk(2026, 10, 16, 23)), outlook(OCT16, sat_day(0.92)), firm(OCT17, False, marginal=True)
        )
        self.assertEqual(msgs, [])
        self.assertEqual(len(sim.events), 1)

    def test_auction_alone_gives_a_heads_up(self):
        now = uk(2026, 10, 16, 11)
        window = {"start": "10:00", "end": "15:00"}
        msgs = Sim().run(now, prices(now, uk(2026, 10, 16, 23)), outlook(OCT16), firm(OCT17, True, [window]))
        self.assertEqual([m.kind for m in msgs], ["heads_up"])
        self.assertEqual(msgs[0].title, "Free electricity very likely tomorrow")
        self.assertEqual(msgs[0].priority, 4)

    def test_split_forecast_keeps_one_event(self):
        sim = Sim()
        now = uk(2026, 10, 21, 13, 40)
        sim.run(now, prices(now, uk(2026, 10, 22, 23)), outlook(OCT22, night(OCT24, 0.95)), firm(OCT22))
        split = night(OCT24, 0.95)
        split[(OCT25, 1)] = split[(OCT25, 2)] = 0.1
        now = uk(2026, 10, 22, 13, 40)
        msgs = sim.run(now, prices(now, uk(2026, 10, 23, 23)), outlook(OCT23, split), firm(OCT23))
        self.assertEqual(msgs, [])
        [event] = sim.events
        self.assertEqual((event.start, event.end), (uk(2026, 10, 24, 23), uk(2026, 10, 25, 6)))

    def test_evening_hours_kept_once_forecast_moves_on(self):
        # On Saturday morning the forecast starts at Sunday midnight, so it no longer
        # shows Saturday 11pm. That hour isn't priced yet either, so the event keeps it.
        sim = Sim()
        now = uk(2026, 10, 21, 13, 40)
        sim.run(now, prices(now, uk(2026, 10, 22, 23)), outlook(OCT22, night(OCT24, 0.95)), firm(OCT22))
        now = uk(2026, 10, 24, 9)
        msgs = sim.run(now, prices(now, uk(2026, 10, 24, 23)), outlook(OCT25, night(OCT24, 0.95)), firm(OCT24))
        self.assertEqual(msgs, [])
        self.assertEqual(sim.events[0].start, uk(2026, 10, 24, 23))

    def test_overnight_heads_up_not_held_if_it_starts_before_morning(self):
        # EDF's Saturday prices are late, so at 11:30pm the forecast is all there is.
        now = uk(2026, 10, 16, 23, 30)
        hours = {(OCT17, h): 0.95 for h in range(4, 8)}
        msgs = Sim().run(now, prices(now, uk(2026, 10, 16, 23)), outlook(OCT17, hours), firm(OCT17, marginal=True))
        self.assertEqual([m.kind for m in msgs], ["heads_up", "missing_prices"])
        self.assertIsNone(msgs[0].delay_until)  # 4am is before the 7am wake-up, so send now
        self.assertEqual(msgs[1].delay_until, uk(2026, 10, 17, 7))


class ConfirmedWindows(unittest.TestCase):
    def setUp(self):
        self.sim = Sim()
        self.start, self.end = uk(2026, 10, 24, 23), uk(2026, 10, 25, 6)
        self.now = uk(2026, 10, 24, 12, 30)
        msgs = self.sim.run(
            self.now, prices(self.now, uk(2026, 10, 25, 23), free=[(self.start, self.end)]), outlook(OCT25), firm(OCT25)
        )
        self.assertEqual([m.kind for m in msgs], ["confirmed"])  # no heads-up needed first

    def test_cancelled(self):
        now = self.now + timedelta(minutes=15)
        msgs = self.sim.run(now, prices(now, uk(2026, 10, 25, 23)), outlook(OCT25), firm(OCT25))
        self.assertEqual([m.kind for m in msgs], ["cancelled"])
        self.assertEqual(self.sim.events, [])

    def test_times_changed(self):
        now = self.now + timedelta(minutes=15)
        shorter = (self.start, uk(2026, 10, 25, 5))
        msgs = self.sim.run(now, prices(now, uk(2026, 10, 25, 23), free=[shorter]), outlook(OCT25), firm(OCT25))
        self.assertEqual([m.kind for m in msgs], ["changed"])
        self.assertIn("Now Sat 24 Oct, 11pm – 5am Sun (was Sat 24 Oct, 11pm – 6am Sun)", msgs[0].body)

    def test_source_outage_changes_nothing(self):
        now = self.now + timedelta(minutes=15)
        self.assertEqual(self.sim.run(now, None, None, None), [])
        self.assertEqual(len(self.sim.events), 1)

    def test_no_reminder_straight_after_a_late_confirmation(self):
        sim = Sim()
        now = uk(2026, 10, 24, 22, 30)
        msgs = sim.run(now, prices(now, uk(2026, 10, 25, 23), free=[(self.start, self.end)]), outlook(OCT25), firm(OCT25))
        self.assertEqual([m.kind for m in msgs], ["confirmed"])
        now = uk(2026, 10, 24, 22, 45)
        msgs = sim.run(now, prices(now, uk(2026, 10, 25, 23), free=[(self.start, self.end)]), outlook(OCT25), firm(OCT25))
        self.assertEqual(msgs, [])

    def test_already_running(self):
        sim = Sim()
        now = uk(2026, 10, 25, 2)
        msgs = sim.run(now, prices(now, uk(2026, 10, 25, 23), free=[(self.start, self.end)]), outlook(OCT26), firm(OCT26))
        self.assertEqual(msgs[0].title, "Free electricity on now")
        self.assertEqual(msgs[0].body, "EDF's prices are 0p until 6am Sun.")


class Health(unittest.TestCase):
    def test_warns_once_after_twelve_hours_then_recovers(self):
        sim = Sim()
        start = uk(2026, 10, 20, 9)
        for i in range(0, 13 * 4):
            now = start + timedelta(minutes=15 * i)
            msgs = sim.run(now, prices(now, uk(2026, 10, 21, 23)), None, firm(OCT21))
            kinds = [m.kind for m in msgs]
            if i == 12 * 4:
                self.assertEqual(kinds, ["source_down"])
                self.assertIn("the predictor's 7-day forecast", msgs[0].body)
            else:
                self.assertEqual(kinds, [], i)
        now = start + timedelta(hours=14)
        msgs = sim.run(now, prices(now, uk(2026, 10, 21, 23)), outlook(OCT21), firm(OCT21))
        self.assertEqual([m.kind for m in msgs], ["recovered"])
        self.assertEqual(sim.state["health"], {})

    def test_missing_prices_warning_once_a_day(self):
        sim = Sim()
        now = uk(2026, 10, 20, 18, 15)
        msgs = sim.run(now, prices(now, uk(2026, 10, 20, 23)), outlook(OCT21), firm(OCT21))
        self.assertEqual([m.kind for m in msgs], ["missing_prices"])
        now = uk(2026, 10, 20, 18, 30)
        self.assertEqual(sim.run(now, prices(now, uk(2026, 10, 20, 23)), outlook(OCT21), firm(OCT21)), [])


class Formatting(unittest.TestCase):
    def test_describe(self):
        self.assertEqual(c.describe(uk(2026, 10, 24, 23), uk(2026, 10, 25, 6)), "Sat 24 Oct, 11pm – 6am Sun")
        self.assertEqual(c.describe(uk(2026, 7, 25, 9), uk(2026, 7, 25, 16)), "Sat 25 Jul, 9am – 4pm")
        self.assertEqual(c.describe(uk(2026, 7, 25, 20), uk(2026, 7, 26, 0)), "Sat 25 Jul, 8pm – midnight")
        self.assertEqual(c.describe(uk(2026, 4, 4, 23), uk(2026, 4, 5, 16)), "Sat 4 Apr, 11pm – 4pm Sun")
        self.assertEqual(c.describe(uk(2026, 4, 4, 23, 30), uk(2026, 4, 6, 12)), "Sat 4 Apr 11:30pm – Mon 6 Apr midday")

    def test_when(self):
        now = uk(2026, 10, 24, 12)
        self.assertEqual(c.when(uk(2026, 10, 24, 23), now), "tonight")
        self.assertEqual(c.when(uk(2026, 10, 25, 2), now), "tonight")
        self.assertEqual(c.when(uk(2026, 10, 25, 10), now), "tomorrow")
        self.assertEqual(c.when(uk(2026, 10, 27, 23), now), "Tuesday night")

    def test_auction_window_without_dates(self):
        start, end = c.auction_window(OCT25, {"start": "23:00", "end": "06:00"})
        self.assertEqual((start, end), (uk(2026, 10, 24, 23), uk(2026, 10, 25, 6)))
        start, end = c.auction_window(OCT25, {"start": "10:00", "end": "15:00"})
        self.assertEqual((start, end), (uk(2026, 10, 25, 10), uk(2026, 10, 25, 15)))

    def test_calendar_format(self):
        sim = Sim()
        now = uk(2026, 10, 21, 13, 40)
        sim.run(now, prices(now, uk(2026, 10, 22, 23)), outlook(OCT22, night(OCT24, 0.95)), firm(OCT22))
        text = sim.ics()
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))
        for line in text.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75, line)
        self.assertIn("DTSTART:20261024T220000Z", text)
        self.assertIn("DTEND:20261025T060000Z", text)
        # Unfolded, the description is intact.
        unfolded = text.replace("\r\n ", "")
        self.assertIn("Forecast only (peak chance 95%)", unfolded)


if __name__ == "__main__":
    unittest.main()
