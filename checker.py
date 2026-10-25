#!/usr/bin/env python3
"""Free electricity alerts for EDF Power Perks.

Every run (GitHub Actions runs it once an hour) it:
  1. reads EDF's public FreePhase Dynamic prices, where 0p half-hours mean free
     electricity, plus the Power Perks Predictor's day-ahead call and 7-day forecast;
  2. works out which free windows are confirmed and which are only looking likely;
  3. sends a phone notification through ntfy for anything new or changed;
  4. rewrites the calendar feed and saves what it knows for the next run.

A "possible" window that stops looking likely, or that EDF's prices rule out, is
removed from the calendar feed, so it disappears from your phone on its next refresh.

Only the Python standard library is used, so there is nothing to install.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
UTC = timezone.utc
ROOT = Path(__file__).resolve().parent

SITE = "https://powerperkspredictor.app"
EDF_API = "https://api.edfgb-kraken.energy/v1"
DEFAULT_PRODUCT = "EDF_FREEPHASE_DYNAMIC_12M_V2_HH"
USER_AGENT = "free-power-alerts/1.0 (personal alert script; polls hourly)"

# Scheduled runs can start late (on-the-hour runs especially), so plan as if the
# next run might be this much later.
RUN_DELAY_ALLOWANCE = timedelta(minutes=30)

SOURCE_LABELS = {
    "edf": "EDF's price data",
    "outlook": "the predictor's 7-day forecast",
    "tomorrow": "the predictor's day-ahead call",
}

# Messages that can wait until morning if they're generated overnight.
DEFERRABLE = {"heads_up", "faded", "not_happening", "source_down", "recovered", "missing_prices"}


# ---------------------------------------------------------------- settings


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass
class Config:
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    ntfy_token: str = ""
    region: str = "A"
    product: str = ""  # empty: follow the predictor's hint, then DEFAULT_PRODUCT
    heads_up_probability: float = 0.9  # forecast peak needed for a heads-up
    heads_up_days: int = 3  # only warn about windows starting within this many days
    keep_probability: float = 0.6  # a possible window stays while its peak is at least this
    hour_probability: float = 0.5  # hours at least this likely make up a forecast window
    reminder_minutes: int = 60  # 0 turns reminders off
    check_minutes: int = 60  # how often the schedule runs this script
    quiet_start: int = 22  # overnight, non-urgent alerts wait until quiet_end
    quiet_end: int = 7
    history_days: int = 30  # how long past free windows stay in the calendar
    stale_hours: int = 12  # warn once a data source has been failing this long
    state_file: Path = ROOT / "data" / "state.json"
    calendar_file: Path = ROOT / "docs" / "free-electricity.ics"

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls(
            ntfy_topic=_env("NTFY_TOPIC", ""),
            ntfy_server=_env("NTFY_SERVER", cls.ntfy_server),
            ntfy_token=_env("NTFY_TOKEN", ""),
            region=_env("EDF_REGION", cls.region).upper(),
            product=_env("EDF_PRODUCT", ""),
            heads_up_probability=float(_env("HEADS_UP_PROBABILITY", str(cls.heads_up_probability))),
            heads_up_days=int(_env("HEADS_UP_DAYS", str(cls.heads_up_days))),
            reminder_minutes=int(_env("REMINDER_MINUTES", str(cls.reminder_minutes))),
            check_minutes=int(_env("CHECK_MINUTES", str(cls.check_minutes))),
            quiet_start=int(_env("QUIET_START", str(cls.quiet_start))),
            quiet_end=int(_env("QUIET_END", str(cls.quiet_end))),
        )
        if not re.fullmatch(r"[A-P]", cfg.region):
            raise SystemExit(f"EDF_REGION should be a single letter from A to P, not {cfg.region!r}")
        if cfg.product and not re.fullmatch(r"[A-Z0-9_-]+", cfg.product):
            raise SystemExit(f"EDF_PRODUCT doesn't look like an EDF product code: {cfg.product!r}")
        return cfg


# ------------------------------------------------------------------ models


@dataclass
class Slot:
    start: datetime
    end: datetime
    price: float  # pence per kWh including VAT


@dataclass
class Window:
    start: datetime
    end: datetime
    kind: str  # "confirmed" (EDF 0p), "auction" (day-ahead auction) or "forecast"
    peak: float = 1.0


@dataclass
class Event:
    uid: str
    status: str  # "possible" or "confirmed"
    source: str  # "forecast", "auction" or "edf"
    start: datetime
    end: datetime
    peak: float
    created: datetime
    updated: datetime
    sequence: int = 0
    notified: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "status": self.status,
            "source": self.source,
            "start": iso(self.start),
            "end": iso(self.end),
            "peak": self.peak,
            "created": iso(self.created),
            "updated": iso(self.updated),
            "sequence": self.sequence,
            "notified": self.notified,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(
            uid=d["uid"],
            status=d["status"],
            source=d.get("source", "forecast"),
            start=parse_time(d["start"]),
            end=parse_time(d["end"]),
            peak=float(d.get("peak", 1.0)),
            created=parse_time(d["created"]),
            updated=parse_time(d["updated"]),
            sequence=int(d.get("sequence", 0)),
            notified=list(d.get("notified", [])),
        )


@dataclass
class Message:
    kind: str
    title: str
    body: str
    priority: int = 3  # ntfy: 1 min, 2 low, 3 default, 4 high, 5 urgent
    tags: list = field(default_factory=lambda: ["zap"])
    uid: str | None = None
    event_start: datetime | None = None
    delay_until: datetime | None = None


# ------------------------------------------------------------- time helpers


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_time(day: date, hhmm: str) -> datetime:
    """A UK wall-clock time on a given day, as UTC. "24:00" means the next midnight."""
    hours, minutes = (int(part) for part in hhmm.split(":")[:2])
    if hours == 24:
        day, hours = day + timedelta(days=1), 0
    return datetime(day.year, day.month, day.day, hours, minutes, tzinfo=UK).astimezone(UTC)


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def merge(windows: list[Window]) -> list[Window]:
    """Join windows of the same kind that touch or overlap."""
    out: list[Window] = []
    for w in sorted(windows, key=lambda w: w.start):
        if out and w.start <= out[-1].end:
            out[-1].end = max(out[-1].end, w.end)
            out[-1].peak = max(out[-1].peak, w.peak)
        else:
            out.append(Window(w.start, w.end, w.kind, w.peak))
    return out


# ---------------------------------------------------------- wording helpers


def clock(dt: datetime) -> str:
    t = dt.astimezone(UK)
    if (t.hour, t.minute) == (0, 0):
        return "midnight"
    if (t.hour, t.minute) == (12, 0):
        return "midday"
    hour = t.hour % 12 or 12
    suffix = "am" if t.hour < 12 else "pm"
    return f"{hour}{suffix}" if t.minute == 0 else f"{hour}:{t.minute:02d}{suffix}"


def day_label(dt: datetime) -> str:
    t = dt.astimezone(UK)
    return f"{t:%a} {t.day} {t:%b}"


def describe(start: datetime, end: datetime) -> str:
    """e.g. "Sat 24 Oct, 11pm – 6am Sun"."""
    s, e = start.astimezone(UK), end.astimezone(UK)
    if e.date() == s.date():
        return f"{day_label(s)}, {clock(s)} – {clock(e)}"
    if e.date() == s.date() + timedelta(days=1):
        if (e.hour, e.minute) == (0, 0):
            return f"{day_label(s)}, {clock(s)} – midnight"
        return f"{day_label(s)}, {clock(s)} – {clock(e)} {e:%a}"
    return f"{day_label(s)} {clock(s)} – {day_label(e)} {clock(e)}"


def when(start: datetime, now: datetime) -> str:
    """A short name for when a window starts: "tonight", "tomorrow", "Saturday night"…"""
    s = start.astimezone(UK)
    today = now.astimezone(UK).date()
    night = s.hour >= 20 or s.hour < 5
    day = s.date() - timedelta(days=1) if s.hour < 5 else s.date()  # small hours belong to the night before
    if day <= today:
        return "tonight" if night else "today"
    if day == today + timedelta(days=1):
        return "tomorrow night" if night else "tomorrow"
    return f"{day:%A} night" if night else f"{day:%A}"


def duration(start: datetime, end: datetime) -> str:
    hours = (end - start).total_seconds() / 3600
    return "1 hour" if hours == 1 else f"{hours:g} hours"


def percent(p: float) -> str:
    return f"{round(p * 100)}%"


# ------------------------------------------------------------- the decision


def auction_date(tomorrow: dict | None) -> date | None:
    """The day the predictor's day-ahead call is for, once the auction has settled."""
    if not tomorrow or tomorrow.get("status") != "firm":
        return None
    try:
        return date.fromisoformat(tomorrow["date"])
    except (KeyError, ValueError, TypeError):
        return None


def auction_hours(tomorrow: dict | None) -> tuple[datetime, datetime] | None:
    """The hours a settled auction speaks for: 11pm the day before to 11pm UK time.

    Once settled, the auction is better evidence than the forecast for those hours -
    unless it cleared just above zero, when EDF have sometimes gone free anyway.
    """
    day = auction_date(tomorrow)
    if day is None or tomorrow.get("marginal"):
        return None
    return local_time(day - timedelta(days=1), "23:00"), local_time(day, "23:00")


def guess_windows(outlook: dict | None, tomorrow: dict | None, after: datetime, cfg: Config) -> list[Window]:
    """Windows that are only expected so far: from the day-ahead auction, or the forecast.

    Anything before `after` (the end of EDF's published prices) is left out, because
    there the prices already give the answer.
    """
    windows: list[Window] = []
    auction_day = auction_hours(tomorrow)
    day = auction_date(tomorrow)
    if day and tomorrow.get("free"):
        for w in tomorrow.get("windows") or []:
            try:
                start, end = auction_window(day, w)
            except (KeyError, ValueError, TypeError, IndexError):
                continue
            windows.append(Window(start, end, "auction", 1.0))

    for entry in (outlook or {}).get("days") or []:
        try:
            day = date.fromisoformat(entry["date"])
        except (KeyError, ValueError, TypeError):
            continue
        for hour in entry.get("hours") or []:
            p = hour.get("p_free")
            if p is None or p < cfg.hour_probability:
                continue
            h = int(hour["hour"])
            start = local_time(day, f"{h:02d}:00")
            # Ending at the next wall-clock hour keeps the clock-change nights joined up.
            end = local_time(day, f"{h + 1:02d}:00")
            if end <= start:
                continue
            if auction_day and overlaps(start, end, *auction_day):
                continue
            windows.append(Window(start, end, "forecast", float(p)))

    clipped = [Window(max(w.start, after), w.end, w.kind, w.peak) for w in windows if w.end > after]
    auction = merge([w for w in clipped if w.kind == "auction"])
    forecast = merge([w for w in clipped if w.kind == "forecast"])
    return sorted(auction + forecast, key=lambda w: w.start)


def auction_window(day: date, w: dict) -> tuple[datetime, datetime]:
    """Turn a {start, end, start_date?, end_date?} window from tomorrow.json into times."""
    start_txt, end_txt = w["start"], w["end"]
    to_minutes = lambda txt: int(txt.split(":")[0]) * 60 + int(txt.split(":")[1])
    crosses_midnight = to_minutes(end_txt) <= to_minutes(start_txt)
    start_day = date.fromisoformat(w["start_date"]) if w.get("start_date") else None
    end_day = date.fromisoformat(w["end_date"]) if w.get("end_date") else None
    if start_day is None:
        # A window crossing midnight opens the evening before the day it belongs to.
        start_day = day - timedelta(days=1) if crosses_midnight else day
    if end_day is None:
        end_day = start_day + timedelta(days=1) if crosses_midnight else start_day
    start, end = local_time(start_day, start_txt), local_time(end_day, end_txt)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def forecast_start(outlook: dict | None) -> datetime | None:
    """When the forecast's first day begins."""
    days = []
    for entry in (outlook or {}).get("days") or []:
        try:
            days.append(date.fromisoformat(entry["date"]))
        except (KeyError, ValueError, TypeError):
            continue
    return local_time(min(days), "00:00") if days else None


def forecast_peak(outlook: dict | None, start: datetime, end: datetime) -> float | None:
    """The forecast's highest hourly chance for a period, if the forecast covers it."""
    best = None
    for entry in (outlook or {}).get("days") or []:
        try:
            day = date.fromisoformat(entry["date"])
        except (KeyError, ValueError, TypeError):
            continue
        for hour in entry.get("hours") or []:
            if hour.get("p_free") is None:
                continue
            h = int(hour["hour"])
            s, e = local_time(day, f"{h:02d}:00"), local_time(day, f"{h + 1:02d}:00")
            if overlaps(s, e, start, end):
                best = max(best or 0.0, float(hour["p_free"]))
    return best


def plan(
    state: dict,
    now: datetime,
    rates: list[Slot] | None,
    outlook: dict | None,
    tomorrow: dict | None,
    cfg: Config,
) -> tuple[dict, list[Message]]:
    """Work out what has changed since the last run.

    `rates`, `outlook` and `tomorrow` are None when that source couldn't be fetched.
    Returns the new state and the notifications to send.
    """
    events = [Event.from_dict(e) for e in state.get("events", [])]
    messages: list[Message] = []
    rates = rates or None  # an empty price list tells us nothing
    healthy = {"edf": rates is not None, "outlook": outlook is not None, "tomorrow": tomorrow is not None}

    def new_event(w: Window, status: str, source: str) -> Event:
        uid = hashlib.sha1(f"{iso(w.start)}|{iso(now)}".encode()).hexdigest()[:16]
        e = Event(uid, status, source, w.start, w.end, round(w.peak, 2), now, now)
        events.append(e)
        return e

    def touch(e: Event) -> None:
        e.updated = now
        e.sequence += 1

    covered_until = max(s.end for s in rates) if rates else None
    confirmed = merge([Window(s.start, s.end, "confirmed") for s in rates if s.price <= 0]) if rates else []
    after = max(now, covered_until) if covered_until else now
    # Without EDF's prices there's no telling which hours are already settled, so no guessing.
    guesses = guess_windows(outlook, tomorrow, after, cfg) if covered_until else []
    matched: set[str] = set()

    # 1. EDF's own prices: half-hours at 0p are free.
    for w in confirmed:
        hits = [e for e in events if e.uid not in matched and overlaps(e.start, e.end, w.start, w.end)]
        hits.sort(key=lambda e: (e.status != "confirmed", e.start))
        if hits:
            e = hits[0]
            if e.status == "confirmed" and e.end <= now:
                # Finished. The price lookup only reaches back a day, so it can show
                # this window cut short; leave the record as it was.
                matched.add(e.uid)
                continue
            old_times, was = (e.start, e.end), e.status
            if was != "confirmed" or old_times != (w.start, w.end):
                e.status, e.source, e.peak = "confirmed", "edf", 1.0
                e.start, e.end = w.start, w.end
                touch(e)
                if e.end > now:
                    if was != "confirmed":
                        messages.append(confirmed_message(e, now))
                    else:
                        messages.append(changed_message(e, old_times, now))
        else:
            e = new_event(w, "confirmed", "edf")
            if e.end > now:
                messages.append(confirmed_message(e, now))
        matched.add(e.uid)

    # 2. Windows that are only expected so far.
    original = {e.uid: (e.start, e.end) for e in events}
    horizon = forecast_start(outlook)
    for w in guesses:
        if w.peak < cfg.keep_probability:
            continue
        if any(e.status == "confirmed" and overlaps(e.start, e.end, w.start, w.end) for e in events):
            continue
        hits = [
            e
            for e in events
            if e.status == "possible"
            and (
                overlaps(e.start, e.end, w.start, w.end)
                or overlaps(*original.get(e.uid, (e.start, e.end)), w.start, w.end)
            )
        ]
        if hits:
            e = hits[0]
            if e.uid in matched:
                # The forecast has split one window into several: keep one event spanning them.
                start, end = min(e.start, w.start), max(e.end, w.end)
                peak = max(e.peak, round(w.peak, 2))
                source = "auction" if "auction" in (e.source, w.kind) else w.kind
            else:
                start, end, peak, source = w.start, w.end, round(w.peak, 2), w.kind
                if w.kind == "forecast" and horizon and w.start <= horizon and after <= e.start < start:
                    # Those early hours have dropped off the front of the forecast but
                    # aren't priced yet, so nothing has ruled them out.
                    start = e.start
            if (e.start, e.end, e.peak, e.source) != (start, end, peak, source):
                e.start, e.end, e.peak, e.source = start, end, peak, source
                touch(e)
            matched.add(e.uid)
            for extra in hits[1:]:
                if extra.uid not in matched:  # the forecast has joined two windows into one
                    events.remove(extra)
        elif w.peak >= cfg.heads_up_probability and (
            w.kind == "auction" or starts_within(w.start, now, cfg.heads_up_days)
        ):
            e = new_event(w, "possible", w.kind)
            matched.add(e.uid)
            messages.append(heads_up_message(e, now))

    # 3. Events nothing matched this run.
    for e in list(events):
        if e.uid in matched:
            continue
        if any(overlaps(e.start, e.end, w.start, w.end) for w in confirmed):
            events.remove(e)  # joined into a neighbouring free window
            continue
        if e.status == "confirmed":
            if e.end <= now:
                if e.end < now - timedelta(days=cfg.history_days):
                    events.remove(e)
            elif covered_until and e.end <= covered_until:
                events.remove(e)  # EDF have re-priced a window they had at 0p
                messages.append(cancelled_message(e, now))
            continue
        if e.end <= now:
            events.remove(e)
        elif covered_until and e.end <= covered_until:
            events.remove(e)
            messages.append(not_happening_message(e, now))
        elif all(healthy.values()):
            events.remove(e)
            settled = auction_hours(tomorrow)
            auction_said_no = bool(settled and overlaps(e.start, e.end, *settled))
            latest = None if auction_said_no else forecast_peak(outlook, e.start, e.end)
            messages.append(faded_message(e, now, latest, auction_said_no))
        # Otherwise a source is down: keep the event until the data comes back.

    # 4. Reminders shortly before a confirmed window starts. If the reminder falls due
    #    before the next run, hand it to ntfy now to deliver at exactly the right time.
    if cfg.reminder_minutes > 0:
        just_confirmed = {m.uid for m in messages if m.kind == "confirmed"}
        next_run = now + timedelta(minutes=cfg.check_minutes) + RUN_DELAY_ALLOWANCE
        for e in events:
            if e.status != "confirmed" or "reminder" in e.notified or e.start <= now:
                continue
            due = e.start - timedelta(minutes=cfg.reminder_minutes)
            if due > next_run:
                continue  # a later run will see to it
            e.notified.append("reminder")
            if due <= now and e.uid in just_confirmed:
                continue  # confirmed this close to the start: that alert is the reminder
            reminder = reminder_message(e, max(due, now))
            if due > now + timedelta(minutes=1):
                reminder.delay_until = due
            messages.append(reminder)

    # 5. Data sources that keep failing.
    health = {name: dict(info) for name, info in (state.get("health") or {}).items()}
    for name, ok in healthy.items():
        info = health.get(name, {})
        if ok:
            if info.get("warned"):
                messages.append(recovered_message(name))
            health.pop(name, None)
            continue
        since = parse_time(info["failing_since"]) if info.get("failing_since") else now
        info["failing_since"] = iso(since)
        if not info.get("warned") and now - since >= timedelta(hours=cfg.stale_hours):
            info["warned"] = True
            messages.append(source_down_message(name, since))
        health[name] = info

    new_state = {"version": 1, "events": [], "health": health}
    missing_warned = state.get("missing_prices_warned")
    local_now = now.astimezone(UK)
    tomorrow_noon = datetime.combine(local_now.date() + timedelta(days=1), time(12), UK)
    if (
        covered_until
        and local_now.hour >= 18
        and covered_until < tomorrow_noon
        and missing_warned != local_now.date().isoformat()
    ):
        missing_warned = local_now.date().isoformat()
        messages.append(missing_prices_message())
    if missing_warned:
        new_state["missing_prices_warned"] = missing_warned

    for m in messages:
        e = next((e for e in events if e.uid == m.uid), None)
        if e and m.kind not in e.notified:
            e.notified.append(m.kind)
    new_state["events"] = [e.to_dict() for e in sorted(events, key=lambda e: e.start)]
    return new_state, messages


def starts_within(start: datetime, now: datetime, days: int) -> bool:
    return start.astimezone(UK).date() <= now.astimezone(UK).date() + timedelta(days=days)


# ------------------------------------------------------------------ messages


def heads_up_message(e: Event, now: datetime) -> Message:
    span = describe(e.start, e.end)
    if e.source == "auction":
        return Message(
            "heads_up",
            f"Free electricity very likely {when(e.start, now)}",
            f"The day-ahead power auction cleared at or below £0 for {span}. "
            "EDF's own prices usually confirm it around midday.",
            priority=4,
            uid=e.uid,
            event_start=e.start,
        )
    return Message(
        "heads_up",
        f"Free electricity possible {when(e.start, now)}",
        f"Forecast: {span} could be free (peak chance {percent(e.peak)}). "
        "You'll get another alert when EDF's prices confirm it, usually around midday the day before. "
        "It's in your calendar as tentative.",
        uid=e.uid,
        event_start=e.start,
    )


def confirmed_message(e: Event, now: datetime) -> Message:
    if e.start <= now:
        return Message(
            "confirmed",
            "Free electricity on now",
            f"EDF's prices are 0p until {clock(e.end)} {e.end.astimezone(UK):%a}.",
            priority=4,
            tags=["zap", "white_check_mark"],
            uid=e.uid,
            event_start=e.start,
        )
    return Message(
        "confirmed",
        f"Free electricity confirmed {when(e.start, now)}",
        f"EDF's prices are 0p for {describe(e.start, e.end)} ({duration(e.start, e.end)}). "
        "Power Perks have matched these windows, so EDF's text should follow.",
        priority=4,
        tags=["zap", "white_check_mark"],
        uid=e.uid,
        event_start=e.start,
    )


def changed_message(e: Event, old: tuple[datetime, datetime], now: datetime) -> Message:
    return Message(
        "changed",
        f"Free electricity times changed {when(e.start, now)}",
        f"Now {describe(e.start, e.end)} (was {describe(*old)}).",
        priority=4,
        uid=e.uid,
        event_start=e.start,
    )


def cancelled_message(e: Event, now: datetime) -> Message:
    return Message(
        "cancelled",
        f"Free electricity cancelled {when(e.start, now)}",
        f"EDF's prices for {describe(e.start, e.end)} are no longer 0p. Removed from your calendar.",
        priority=4,
        tags=["x"],
        uid=e.uid,
        event_start=e.start,
    )


def not_happening_message(e: Event, now: datetime) -> Message:
    return Message(
        "not_happening",
        f"No free electricity {when(e.start, now)} after all",
        f"EDF's prices for {describe(e.start, e.end)} are out and none of them are 0p. "
        "Removed from your calendar.",
        tags=["x"],
        uid=e.uid,
        event_start=e.start,
    )


def faded_message(e: Event, now: datetime, latest: float | None, auction_said_no: bool) -> Message:
    span = describe(e.start, e.end)
    if auction_said_no:
        body = (
            f"The day-ahead power auction didn't clear at zero for {span}, so free electricity is unlikely. "
            "EDF's prices, usually out around midday, will settle it."
        )
    elif latest is not None:
        body = f"The forecast for {span} has dropped to {percent(latest)}."
    else:
        body = f"The forecast no longer shows {span} as likely."
    body += " Removed from your calendar. You'll get another alert if it comes back."
    return Message(
        "faded",
        f"Free electricity less likely {when(e.start, now)}",
        body,
        priority=2,
        tags=["chart_with_downwards_trend"],
        uid=e.uid,
        event_start=e.start,
    )


def reminder_message(e: Event, now: datetime) -> Message:
    minutes = round((e.start - now).total_seconds() / 60)
    soon = "in about an hour" if minutes >= 50 else f"in {minutes} minutes"
    end_day = "" if e.end.astimezone(UK).date() == e.start.astimezone(UK).date() else f" {e.end.astimezone(UK):%a}"
    return Message(
        "reminder",
        f"Free electricity starts {soon}",
        f"Free from {clock(e.start)} until {clock(e.end)}{end_day}.",
        priority=4,
        tags=["alarm_clock", "zap"],
        uid=e.uid,
        event_start=e.start,
    )


def source_down_message(name: str, since: datetime) -> Message:
    return Message(
        "source_down",
        "Free electricity alerts: data problem",
        f"Couldn't reach {SOURCE_LABELS[name]} since {day_label(since)} {clock(since)}. "
        "Alerts may be late or missed until it's back.",
        tags=["warning"],
    )


def recovered_message(name: str) -> Message:
    label = SOURCE_LABELS[name]
    return Message(
        "recovered",
        "Free electricity alerts: back to normal",
        f"{label[0].upper()}{label[1:]} is reachable again.",
        priority=2,
        tags=["white_check_mark"],
    )


def missing_prices_message() -> Message:
    return Message(
        "missing_prices",
        "EDF's prices for tomorrow haven't appeared",
        "They normally publish around midday. If this keeps happening, EDF may have changed "
        "their tariff and the checker needs updating.",
        tags=["warning"],
    )


def in_quiet_hours(hour: int, cfg: Config) -> bool:
    if cfg.quiet_start == cfg.quiet_end:
        return False
    if cfg.quiet_start > cfg.quiet_end:
        return hour >= cfg.quiet_start or hour < cfg.quiet_end
    return cfg.quiet_start <= hour < cfg.quiet_end


def apply_quiet_hours(messages: list[Message], now: datetime, cfg: Config) -> None:
    """Hold non-urgent overnight alerts until morning, unless the window starts before then."""
    local = now.astimezone(UK)
    if not in_quiet_hours(local.hour, cfg):
        return
    wake = local.replace(hour=cfg.quiet_end, minute=0, second=0, microsecond=0)
    if wake <= local:
        wake += timedelta(days=1)
    wake = wake.astimezone(UTC)
    for m in messages:
        if m.kind in DEFERRABLE and (m.event_start is None or m.event_start > wake + timedelta(hours=1)):
            m.delay_until = wake


# ------------------------------------------------------------------ calendar


def ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_fold(line: str) -> str:
    """Fold a content line at 75 bytes, as the iCalendar format requires."""
    parts, current, size = [], "", 0
    for ch in line:
        width = len(ch.encode("utf-8"))
        if size + width > 75:
            parts.append(current)
            current, size = " " + ch, 1 + width
        else:
            current, size = current + ch, size + width
    parts.append(current)
    return "\r\n".join(parts)


def ics_time(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def calendar(events: list[dict]) -> str:
    """The calendar feed. Depends only on the events, so it only changes when they do."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//free-power-alerts//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Free electricity",
        "X-WR-CALDESC:EDF free electricity windows: confirmed from EDF's prices\\, "
        "tentative from the Power Perks Predictor forecast.",
        "X-WR-TIMEZONE:Europe/London",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]
    for d in events:
        e = Event.from_dict(d)
        if e.status == "confirmed":
            summary = "⚡ Free electricity"
            status = "CONFIRMED"
            detail = (
                "EDF's FreePhase prices are 0p for this window. Power Perks have matched these "
                "windows; EDF's text is the official confirmation."
            )
        elif e.source == "auction":
            summary = "⚡ Free electricity very likely"
            status = "TENTATIVE"
            detail = (
                "The day-ahead power auction cleared at or below £0. This becomes confirmed, or "
                "disappears, once EDF publish their prices (usually around midday)."
            )
        else:
            summary = f"⚡ Possible free electricity ({percent(e.peak)})"
            status = "TENTATIVE"
            detail = (
                f"Forecast only (peak chance {percent(e.peak)}). This updates as the forecast changes, "
                "and becomes confirmed or disappears once EDF publish their prices "
                "(usually around midday the day before)."
            )
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e.uid}@free-power-alerts",
            f"DTSTAMP:{ics_time(e.updated)}",
            f"LAST-MODIFIED:{ics_time(e.updated)}",
            f"SEQUENCE:{e.sequence}",
            f"DTSTART:{ics_time(e.start)}",
            f"DTEND:{ics_time(e.end)}",
            f"SUMMARY:{ics_escape(summary)}",
            f"DESCRIPTION:{ics_escape(detail + ' ' + SITE)}",
            f"STATUS:{status}",
            "TRANSP:TRANSPARENT",
            f"URL:{SITE}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(line) for line in lines) + "\r\n"


# --------------------------------------------------------------- networking


def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # python.org builds on macOS come without CA certificates; use the system bundle.
    if not ctx.get_ca_certs():
        for bundle in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
            if os.path.exists(bundle):
                ctx.load_verify_locations(bundle)
                break
    return ctx


SSL = ssl_context()


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30, context=SSL) as response:
        return json.load(response)


def try_fetch(url: str) -> dict | None:
    try:
        return fetch_json(url)
    except Exception as exc:  # noqa: BLE001 - any failure just means "no data this run"
        log(f"Couldn't fetch {url}: {exc}")
        return None


def parse_rates(results: list[dict]) -> list[Slot]:
    slots = []
    for r in results:
        if not r.get("valid_from") or not r.get("valid_to") or r.get("value_inc_vat") is None:
            continue
        if r.get("payment_method") not in (None, "DIRECT_DEBIT"):
            continue
        slots.append(Slot(parse_time(r["valid_from"]), parse_time(r["valid_to"]), float(r["value_inc_vat"])))
    return sorted(slots, key=lambda s: s.start)


def product_hint(tomorrow: dict | None) -> str | None:
    product = ((tomorrow or {}).get("freephase") or {}).get("product")
    if isinstance(product, str) and "FREEPHASE" in product and re.fullmatch(r"[A-Z0-9_-]+", product):
        return product
    return None


def fetch_rates(cfg: Config, now: datetime, hint: str | None) -> list[Slot] | None:
    products = [cfg.product] if cfg.product else list(dict.fromkeys(p for p in (hint, DEFAULT_PRODUCT) if p))
    query = urllib.parse.urlencode(
        {"period_from": iso(now - timedelta(days=1)), "period_to": iso(now + timedelta(days=3)), "page_size": 1500}
    )
    for product in products:
        tariff = f"E-1R-{product}-{cfg.region}"
        url = f"{EDF_API}/products/{product}/electricity-tariffs/{tariff}/standard-unit-rates/?{query}"
        results: list[dict] = []
        try:
            while url:
                page = fetch_json(url)
                results += page.get("results") or []
                url = page.get("next")
        except Exception as exc:  # noqa: BLE001
            log(f"Couldn't fetch EDF prices for {product}: {exc}")
            continue
        slots = parse_rates(results)
        if slots:
            return slots
        log(f"EDF returned no prices for {product}")
    return None


def send(message: Message, cfg: Config) -> None:
    body = {
        "topic": cfg.ntfy_topic,
        "title": message.title,
        "message": message.body,
        "priority": message.priority,
        "tags": message.tags,
        "click": SITE,
    }
    if message.delay_until:
        body["delay"] = str(int(message.delay_until.timestamp()))
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
    request = urllib.request.Request(
        cfg.ntfy_server.rstrip("/") + "/", data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=30, context=SSL) as response:
        response.read()


# ---------------------------------------------------------------------- main


def log(text: str) -> None:
    print(text, file=sys.stderr)


def load_inputs(cfg: Config, now: datetime, fixtures: str | None):
    if fixtures:
        folder = Path(fixtures)

        def read(name: str):
            path = folder / name
            return json.loads(path.read_text()) if path.exists() else None

        raw = read("rates.json")
        rates = parse_rates(raw["results"] if isinstance(raw, dict) else raw) if raw is not None else None
        return rates, read("outlook.json"), read("tomorrow.json")

    tomorrow = try_fetch(f"{SITE}/tomorrow.json")
    outlook = try_fetch(f"{SITE}/outlook.json")
    rates = fetch_rates(cfg, now, product_hint(tomorrow))
    return rates, outlook, tomorrow


def summarise(rates, outlook, tomorrow, events: list[dict]) -> None:
    if rates:
        free = sum(1 for s in rates if s.price <= 0)
        log(f"EDF prices: {len(rates)} half-hours up to {day_label(rates[-1].end)} {clock(rates[-1].end)}, {free} at 0p")
    if tomorrow:
        log(f"Day-ahead call for {tomorrow.get('date')}: {tomorrow.get('status')}, free={tomorrow.get('free')}")
    if outlook:
        best = max(outlook.get("days") or [], key=lambda d: d.get("max_p_free") or 0, default=None)
        if best:
            log(f"Forecast: highest peak {percent(best.get('max_p_free') or 0)} on {best.get('date')}")
    for d in events:
        e = Event.from_dict(d)
        log(f"Calendar: {e.status:9s} {describe(e.start, e.end)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Free electricity alerts for EDF Power Perks.")
    parser.add_argument("--dry-run", action="store_true", help="print notifications instead of sending them; save nothing")
    parser.add_argument("--test", action="store_true", help="send a test notification and stop")
    parser.add_argument("--now", help="pretend it is this time (ISO 8601), for testing")
    parser.add_argument("--fixtures", help="read rates.json, outlook.json and tomorrow.json from this folder")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    now = parse_time(args.now) if args.now else datetime.now(UTC)
    if not cfg.ntfy_topic and not args.dry_run:
        raise SystemExit("NTFY_TOPIC isn't set. Set it, or use --dry-run to see what would be sent.")

    if args.test:
        send(
            Message(
                "test",
                "Free electricity alerts are working",
                "You'll get alerts here when free electricity looks likely, when EDF's prices confirm it, "
                "and an hour before it starts.",
                tags=["white_check_mark"],
            ),
            cfg,
        )
        log("Test notification sent.")
        return 0

    state = json.loads(cfg.state_file.read_text()) if cfg.state_file.exists() else {}
    rates, outlook, tomorrow = load_inputs(cfg, now, args.fixtures)
    new_state, messages = plan(state, now, rates, outlook, tomorrow, cfg)
    apply_quiet_hours(messages, now, cfg)
    summarise(rates, outlook, tomorrow, new_state["events"])

    for m in messages:
        held = f" (held until {clock(m.delay_until)})" if m.delay_until else ""
        log(f"Notify{held}: {m.title} - {m.body}")
    if not messages:
        log("Nothing new to report.")
    if args.dry_run:
        return 0

    for m in messages:
        send(m, cfg)  # a failure stops here without saving, so the next run tries again

    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_file.write_text(json.dumps(new_state, indent=2, ensure_ascii=False) + "\n")
    cfg.calendar_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.calendar_file.write_bytes(calendar(new_state["events"]).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
