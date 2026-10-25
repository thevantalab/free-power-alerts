# Free electricity alerts

Phone notifications and a calendar feed for EDF's free electricity (Power Perks) windows, so you hear about them days ahead instead of a few hours before.

## What you get

| When | Alert | Calendar |
|---|---|---|
| Usually 2–5 days ahead, when the [Power Perks Predictor](https://powerperkspredictor.app) forecast reaches 90% within the next 3 days | "Free electricity possible Saturday night" | Tentative entry |
| If the forecast drops below 60%, or the day-ahead auction or EDF's prices rule it out | "Free electricity less likely…" / "No free electricity … after all" | Entry removed |
| By about midday the day before, if the auction clears at £0 and there was no heads-up yet | "Free electricity very likely tomorrow" | Tentative entry |
| Within about an hour of EDF's own prices showing 0p (they publish around midday the day before), often before EDF's text | "Free electricity confirmed tonight" with exact times | Confirmed entry |
| An hour before it starts, on time even between checks (ntfy holds it until then) | "Free electricity starts in about an hour" | – |

Heads-ups and "less likely" alerts that come up between 10pm and 7am wait until 7am, unless the window starts before then. Confirmations and reminders are never held back.

## How it works

`checker.py` runs once an hour on GitHub Actions, at 50 minutes past, shortly after the predictor refreshes its files. So an alert can arrive up to about an hour after new information appears, plus any delay in GitHub starting the run (usually a few minutes). Each run it reads:

- **EDF's FreePhase Dynamic prices** from EDF's public API (no login needed). Half-hours priced at 0p are free. These have lined up with Power Perks windows, and EDF publish them around midday the day before.
- **The Power Perks Predictor's** `tomorrow.json` (the day-ahead auction call, about 11:00) and `outlook.json` (the 7-day forecast, updated a couple of times a day).

It saves what it knows in `data/state.json` and writes the calendar feed to `docs/free-electricity.ics`, which GitHub Pages serves. Nothing to install: the script uses only Python's standard library.

EDF's text is still the official confirmation. This is an early-warning system built on public data.

## Setup (about 10 minutes)

1. **Phone alerts.** Install the free **ntfy** app ([iPhone](https://apps.apple.com/app/ntfy/id1625396347), [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)). Tap **+** and subscribe to your topic name. Anyone who knows the name can see the alerts, so keep it hard to guess.
2. **GitHub repository.** Create a **public** repository and push this folder to it. Public keeps it free: unlimited Actions minutes, and free GitHub Pages for the calendar. Nothing private is stored in it.
3. **Secret.** In the repository: **Settings → Secrets and variables → Actions → New repository secret**. Name `NTFY_TOPIC`, value: your topic name.
4. **Calendar hosting.** **Settings → Pages**: Source **Deploy from a branch**, branch **main**, folder **/docs**.
5. **Test.** **Actions → Check for free electricity → Run workflow**, tick **Just send a test notification**. Your phone should buzz. Then run it again without the tick, to create the calendar feed.
6. **Subscribe to the calendar.** Open `https://<your-username>.github.io/<repo-name>/` on your phone and tap **Add to Calendar**.
   - **iPhone:** subscribed calendars refresh on their own schedule (Settings → Calendar → Accounts → Fetch New Data; "Hourly" is enough, as the feed changes at most once an hour). iOS removes alerts from subscribed calendars by default, so rely on the ntfy reminder.
   - **Google Calendar:** re-reads subscribed calendars only every few hours, sometimes up to a day. A cancelled window can linger that long, but the ntfy alert tells you straight away.

## Optional settings

Set these as repository **variables** (Settings → Secrets and variables → Actions → Variables). For variables other than `EDF_REGION`, also add them to the `env:` list in `.github/workflows/check.yml`.

| Variable | Default | Meaning |
|---|---|---|
| Schedule | hourly | Not a variable: edit the `cron` line in `.github/workflows/check.yml`, and set `CHECK_MINUTES` next to it to match, so reminders stay on time |
| `EDF_REGION` | `A` | Your electricity region letter (A–P). Free windows have been the same in every region checked, so this rarely matters. |
| `HEADS_UP_PROBABILITY` | `0.9` | How confident the forecast must be before a heads-up |
| `HEADS_UP_DAYS` | `3` | How many days ahead to warn about |
| `REMINDER_MINUTES` | `60` | Reminder before a confirmed window; `0` turns it off |
| `QUIET_START` / `QUIET_END` | `22` / `7` | Overnight hours when non-urgent alerts wait |

## Running it on your Mac

```sh
python3 checker.py --dry-run                      # check live data; print what it would send, save nothing
NTFY_TOPIC=your-topic python3 checker.py --test   # send a test notification
python3 -m unittest discover tests                # run the tests
```

## If something breaks

- **Data source down for 12 hours:** you get one "data problem" notification, then "back to normal" when it recovers. Tentative calendar entries are kept until the data comes back.
- **EDF's prices for tomorrow missing by 6pm:** you get a warning. If it keeps happening, EDF have probably replaced the FreePhase tariff, and `DEFAULT_PRODUCT` in `checker.py` needs updating.
- **The script itself fails:** GitHub emails you about the failed workflow run.
- **The predictor site changes its files:** heads-ups stop, but confirmations from EDF's prices keep working.
- **Schedule paused:** GitHub pauses schedules in repositories that have been quiet for 60 days. The workflow re-enables itself on every run to prevent that. If GitHub ever emails you that it has been disabled, re-enable it under **Actions**.

## Credits

The forecast comes from [Power Perks Predictor](https://powerperkspredictor.app) by Steve Kirtley, an unofficial one-person project. This checker reads their files once an hour. If you find it useful, consider [supporting their work](https://ko-fi.com/stevekirtley). Not affiliated with EDF Energy.
