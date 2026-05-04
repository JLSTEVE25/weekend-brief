#!/usr/bin/env python3
"""
Weekend Brief Generator
=======================
Runs every Monday at 4 AM ET via GitHub Actions.

Pulls live data from Airtable (Restaurants, Events, Friends),
fetches Charlotte weekend weather (Open-Meteo — no API key needed),
pulls John's + Sara's + Family Google Calendars,
calls Claude API to generate the full HTML, and commits to the repo.

Required GitHub Secrets:
  AIRTABLE_API_KEY      — Airtable Personal Access Token
  AIRTABLE_BASE_ID      — e.g. appXXXXXXXXXXXXXX  (find in your Airtable URL)
  ANTHROPIC_API_KEY     — Claude API key
  GOOGLE_CLIENT_ID      — OAuth 2.0 client ID
  GOOGLE_CLIENT_SECRET  — OAuth 2.0 client secret
  GOOGLE_REFRESH_TOKEN  — Long-lived refresh token (run get_google_token.py once to obtain)

Optional: verify AIRTABLE_TABLE_* names match your actual Airtable base.
"""

import os
import sys
import json
import datetime
from zoneinfo import ZoneInfo
import requests
import anthropic

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleRequest
    GOOGLE_AUTH_AVAILABLE = True
except ImportError:
    GOOGLE_AUTH_AVAILABLE = False

# ── Config ──────────────────────────────────────────────────────────────────
AIRTABLE_API_KEY  = os.environ["AIRTABLE_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# Google Apps Script feedback endpoint — set once deployed (GitHub Secret: FEEDBACK_ENDPOINT).
FEEDBACK_ENDPOINT = os.environ.get("FEEDBACK_ENDPOINT", "")

# Cloudflare Worker URL for passkey auth (GitHub Secret: PASSKEY_AUTH_URL).
PASSKEY_AUTH_URL = os.environ.get("PASSKEY_AUTH_URL", "")

# Google Calendar OAuth — optional; if not set, calendar section is skipped.
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")

# Calendar IDs — overridable via env, sensible defaults baked in.
CALENDAR_IDS = {
    "John":   os.environ.get("WB_CAL_JOHN",   "jlstevenson2@gmail.com"),
    "Sara":   os.environ.get("WB_CAL_SARA",   "sara.smith.stevenson@gmail.com"),
    "Family": os.environ.get("WB_CAL_FAMILY", "family00679441475095031757@group.calendar.google.com"),
}

# Airtable base IDs — overridable via env, sensible defaults baked in.
RESTAURANTS_BASE_ID = os.environ.get("WB_AT_RESTAURANTS_BASE", "appyUA9SEI4R0grrH")
EVENTS_BASE_ID      = os.environ.get("WB_AT_EVENTS_BASE",      "appQEVLUQt03RUIgE")
FRIENDS_BASE_ID     = os.environ.get("WB_AT_FRIENDS_BASE",     "appTGMNTmT9weRbjL")
TABLE_NAME          = os.environ.get("WB_AT_TABLE_NAME",       "Imported table")

# Max radar events passed to Claude (anything beyond this gets logged + dropped).
RADAR_EVENT_CAP = int(os.environ.get("WB_RADAR_CAP", "40"))

AT_HEADERS = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

# Charlotte, NC coordinates
LAT, LON = 35.2271, -80.8431

ET = ZoneInfo("America/New_York")


# ── Airtable helpers ─────────────────────────────────────────────────────────

def fetch_airtable(base_id, filter_formula=None):
    """Fetch all records from a base's 'Imported table', handling pagination."""
    records, params = [], {}
    if filter_formula:
        params["filterByFormula"] = filter_formula

    table_encoded = requests.utils.quote(TABLE_NAME)
    url = f"https://api.airtable.com/v0/{base_id}/{table_encoded}"
    while True:
        resp = requests.get(url, headers=AT_HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
        params["offset"] = offset

    # Flatten record_id into fields so Claude sees it alongside name, price, etc.
    return [{"_record_id": r["id"], **r.get("fields", {})} for r in records]


# ── Weather ──────────────────────────────────────────────────────────────────

WMO_MAP = {
    0:  ("☀️", "Sunny"),
    1:  ("🌤️", "Mostly sunny"),
    2:  ("⛅", "Partly cloudy"),
    3:  ("☁️", "Cloudy"),
    45: ("🌫️", "Foggy"),
    48: ("🌫️", "Freezing fog"),
    51: ("🌦️", "Light drizzle"),
    53: ("🌧️", "Drizzle"),
    55: ("🌧️", "Heavy drizzle"),
    61: ("🌧️", "Light rain"),
    63: ("🌧️", "Rain"),
    65: ("🌧️", "Heavy rain"),
    71: ("❄️", "Light snow"),
    73: ("❄️", "Snow"),
    75: ("❄️", "Heavy snow"),
    80: ("🌦️", "Showers"),
    81: ("🌦️", "Heavy showers"),
    82: ("🌦️", "Violent showers"),
    95: ("⛈️", "Thunderstorm"),
    96: ("⛈️", "Thunderstorm + hail"),
    99: ("⛈️", "Heavy thunderstorm"),
}

def wmo_desc(code):
    return WMO_MAP.get(code, ("🌤️", "Partly cloudy"))


def get_weekend_weather():
    """Return weather dicts for Friday, Saturday, Sunday of the current calendar week's weekend.

    Mon–Sun all point at the same Fri–Sun window. Rolls over to next weekend on Monday.
    """
    today = datetime.date.today()
    # Current week's Friday. Negative on Sat/Sun → Friday is in the past, which is what we want.
    friday   = today + datetime.timedelta(days=(4 - today.weekday()))
    saturday = friday + datetime.timedelta(days=1)
    sunday   = friday + datetime.timedelta(days=2)

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude":  LAT,
        "longitude": LON,
        "daily": [
            "weather_code",
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_probability_max",
        ],
        "temperature_unit": "fahrenheit",
        "timezone": "America/New_York",
        "start_date": friday.isoformat(),
        "end_date":   sunday.isoformat(),
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()["daily"]

    weather = []
    for i, date_str in enumerate(data["time"]):
        date = datetime.date.fromisoformat(date_str)
        icon, desc = wmo_desc(data["weather_code"][i])
        weather.append({
            "day":      date.strftime("%a"),   # "Fri", "Sat", "Sun"
            "date":     date.strftime("%b %d"),
            "high":     round(data["temperature_2m_max"][i]),
            "low":      round(data["temperature_2m_min"][i]),
            "rain_pct": data["precipitation_probability_max"][i],
            "icon":     icon,
            "desc":     desc,
        })

    return weather, friday, saturday, sunday


# ── Google Calendar ──────────────────────────────────────────────────────────

def get_weekend_calendar(friday, saturday, sunday):
    """Pull events from John's, Sara's, and Family Google Calendars for the weekend.
    Returns a list of event dicts tagged with calendar source."""
    if not GOOGLE_AUTH_AVAILABLE:
        print("   ⚠️  google-auth not installed — skipping calendar pull.")
        return []

    if not all([GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN]):
        print("   ⚠️  GOOGLE_* secrets not set — skipping calendar pull.")
        return []

    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )
    creds.refresh(GoogleRequest())

    # Friday 00:00 ET through Sunday 23:59 ET
    time_min = f"{friday.isoformat()}T00:00:00-05:00"
    time_max = f"{sunday.isoformat()}T23:59:59-05:00"

    all_events = []
    for calendar_label, calendar_id in CALENDAR_IDS.items():
        cal_encoded = requests.utils.quote(calendar_id, safe="")
        url = f"https://www.googleapis.com/calendar/v3/calendars/{cal_encoded}/events"
        params = {
            "timeMin":      time_min,
            "timeMax":      time_max,
            "singleEvents": "true",
            "orderBy":      "startTime",
            "maxResults":   50,
        }
        headers = {"Authorization": f"Bearer {creds.token}"}
        resp = requests.get(url, headers=headers, params=params)
        if resp.status_code == 200:
            for item in resp.json().get("items", []):
                start = item.get("start", {})
                end   = item.get("end", {})
                all_day = "date" in start and "dateTime" not in start
                all_events.append({
                    "summary":  item.get("summary", "(No title)"),
                    "start":    start.get("dateTime", start.get("date", "")),
                    "end":      end.get("dateTime",   end.get("date",   "")),
                    "location": item.get("location"),
                    "calendar": calendar_label,
                    "all_day":  all_day,
                })
        else:
            print(f"   ⚠️  Calendar fetch failed for {calendar_label}: {resp.status_code} {resp.text[:120]}")

    # Sort by start time (ISO string sort works for both date and dateTime formats)
    all_events.sort(key=lambda e: e["start"] + ("T00:00:00" if "T" not in e["start"] else ""))
    print(f"   Calendar events fetched: {len(all_events)}")
    return all_events


def get_radar_calendar(today):
    """Pull calendar events for the 15–75 day radar window across all 3 calendars.
    Returns a simplified list of dicts: {date, summary, calendar, all_day, time?}.
    On failure, returns [] so the brief still generates."""
    if not GOOGLE_AUTH_AVAILABLE:
        print("   ⚠️  google-auth not installed — skipping radar calendar pull.")
        return []

    if not all([GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN]):
        print("   ⚠️  GOOGLE_* secrets not set — skipping radar calendar pull.")
        return []

    try:
        creds = Credentials(
            token=None,
            refresh_token=GOOGLE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/calendar.readonly"],
        )
        creds.refresh(GoogleRequest())
    except Exception as ex:
        print(f"   ⚠️  Radar calendar auth failed: {ex}")
        return []

    window_start = today + datetime.timedelta(days=15)
    window_end   = today + datetime.timedelta(days=75)
    time_min = f"{window_start.isoformat()}T00:00:00-05:00"
    time_max = f"{window_end.isoformat()}T23:59:59-05:00"

    simplified = []
    for calendar_label, calendar_id in CALENDAR_IDS.items():
        cal_encoded = requests.utils.quote(calendar_id, safe="")
        url = f"https://www.googleapis.com/calendar/v3/calendars/{cal_encoded}/events"
        params = {
            "timeMin":      time_min,
            "timeMax":      time_max,
            "singleEvents": "true",
            "orderBy":      "startTime",
            "maxResults":   250,
        }
        headers = {"Authorization": f"Bearer {creds.token}"}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
        except Exception as ex:
            print(f"   ⚠️  Radar calendar fetch failed for {calendar_label}: {ex}")
            continue

        if resp.status_code != 200:
            print(f"   ⚠️  Radar calendar fetch failed for {calendar_label}: {resp.status_code} {resp.text[:120]}")
            continue

        for item in resp.json().get("items", []):
            start = item.get("start", {})
            all_day = "date" in start and "dateTime" not in start
            if all_day:
                date_str = start.get("date", "")
                simplified.append({
                    "date":     date_str,
                    "summary":  item.get("summary", "(No title)"),
                    "calendar": calendar_label,
                    "all_day":  True,
                })
            else:
                dt_str = start.get("dateTime", "")
                if not dt_str:
                    continue
                try:
                    dt = datetime.datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    simplified.append({
                        "date":     dt.date().isoformat(),
                        "summary":  item.get("summary", "(No title)"),
                        "time":     dt.strftime("%-I:%M %p"),
                        "calendar": calendar_label,
                        "all_day":  False,
                    })
                except Exception:
                    continue

    simplified.sort(key=lambda e: (e["date"], e.get("time", "")))
    print(f"   Radar calendar events fetched: {len(simplified)}")
    return simplified


# ── Open Window Detection ────────────────────────────────────────────────────

def find_open_windows(calendar_events, friday, saturday, sunday):
    """Identify free time blocks across the weekend.
    Time blocks (ET): Morning 8-12, Afternoon 12-17, Evening 17-22.
    Friday: evening only. Sat/Sun: all three blocks."""

    BLOCKS = {
        "morning":   (8,  12),
        "afternoon": (12, 17),
        "evening":   (17, 22),
    }

    def events_for_day(date):
        d_str = date.isoformat()
        return [e for e in calendar_events if e["start"].startswith(d_str)]

    def block_is_free(events, h_start, h_end):
        """True if no non-all-day event overlaps this ET hour range."""
        for e in events:
            if e["all_day"]:
                continue
            s_str = e["start"]
            end_str = e["end"]
            if "T" not in s_str:
                continue
            try:
                s_dt   = datetime.datetime.fromisoformat(s_str.replace("Z", "+00:00")).astimezone(ET)
                end_dt = datetime.datetime.fromisoformat(end_str.replace("Z", "+00:00")).astimezone(ET)
                s_et   = s_dt.hour   + s_dt.minute   / 60
                end_et = end_dt.hour + end_dt.minute / 60
                if s_et < h_end and end_et > h_start:
                    return False
            except Exception:
                continue
        return True

    open_windows = []
    days = [("Friday", friday), ("Saturday", saturday), ("Sunday", sunday)]

    for day_name, date in days:
        events = events_for_day(date)
        blocks_to_check = ["evening"] if day_name == "Friday" else ["morning", "afternoon", "evening"]

        # Build human-readable context from that day's events
        timed_events = [e["summary"] for e in events if not e["all_day"]]
        all_day_events = [e["summary"] for e in events if e["all_day"]]
        all_titles = timed_events + all_day_events

        if not all_titles:
            context = "Wide open day"
        elif len(all_titles) == 1:
            context = f"Just {all_titles[0]}"
        else:
            context = f"After {', '.join(all_titles[:2])}"

        for block in blocks_to_check:
            h_start, h_end = BLOCKS[block]
            if block_is_free(events, h_start, h_end):
                open_windows.append({
                    "day":        day_name,
                    "window":     block,
                    "start_time": f"{h_start}:00",
                    "end_time":   f"{h_end}:00",
                    "context":    context,
                })

    return open_windows


# ── Event date parsing ────────────────────────────────────────────────────────

def parse_event_date(date_str):
    """Parse Airtable event dates. Handles ISO (2026-04-18) and 'Apr 18, 2026'."""
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_event_date_range(date_str):
    """Parse an Airtable event date or date range into (start, end).
    Handles single dates and ranges like 'Jul 22-26, 2026' or
    'Sep 11-Nov 1, 2026'. Returns (None, None) if unparseable."""
    if not date_str:
        return (None, None)

    s = date_str.strip()

    single = parse_event_date(s)
    if single:
        return (single, single)

    if "-" not in s:
        return (None, None)

    # Expect "<left>-<right>, <year>" shape
    if "," not in s:
        return (None, None)

    left_part, year_part = s.rsplit(",", 1)
    year_part = year_part.strip()
    halves = left_part.split("-", 1)
    if len(halves) != 2:
        return (None, None)

    left  = halves[0].strip()   # e.g. "Jul 22" or "Sep 11"
    right = halves[1].strip()   # e.g. "26" or "Nov 1"

    start = parse_event_date(f"{left}, {year_part}")

    end = parse_event_date(f"{right}, {year_part}")
    if end is None:
        # Right side is just a day number — borrow month from the left side.
        left_tokens = left.split()
        if left_tokens:
            month = left_tokens[0]
            end = parse_event_date(f"{month} {right}, {year_part}")

    if start and end and end >= start:
        return (start, end)
    return (None, None)


# ── Main ──────────────────────────────────────────────────────────────────────

def is_correct_schedule_slot(expected_et_hour):
    """For scheduled GitHub Actions runs, only proceed if the current ET hour matches.

    We deploy two crons (EDT + EST) so a 10am ET run actually lands at 10am ET
    year-round. Whichever one fires at the wrong UTC offset exits cleanly.
    Manual runs (workflow_dispatch) always proceed.
    """
    if os.environ.get("GITHUB_EVENT_NAME", "") != "schedule":
        return True
    now_et = datetime.datetime.now(ET)
    if now_et.hour == expected_et_hour:
        return True
    print(f"⏭️  Scheduled run at {now_et.strftime('%H:%M %Z')} — not the {expected_et_hour:02d}:00 ET slot, exiting.")
    return False


def main():
    if not is_correct_schedule_slot(4):
        sys.exit(0)

    print("📡 Fetching Airtable data…")

    # Restaurants — exclude Vetoed and Nope/Swap feedback
    restaurants = fetch_airtable(RESTAURANTS_BASE_ID,
                                  filter_formula="AND(NOT({Vetoed}='Yes'), NOT({Feedback}='Nope'), NOT({Feedback}='Swap'))")

    # Events — all records; filter by date and Feedback in Python
    all_events_raw = fetch_airtable(EVENTS_BASE_ID)
    all_events = [e for e in all_events_raw if e.get("Feedback", "") not in ("Nope", "Swap")]

    # Friends — Invite to Weekends = Yes, or Tier 1 & 2
    friends = fetch_airtable(FRIENDS_BASE_ID,
                              filter_formula="OR({Invite to Weekends}='Yes', {Tier}<=2)")

    print(f"   Restaurants: {len(restaurants)}")
    print(f"   Events (raw): {len(all_events)}")
    print(f"   Friends (T1+T2): {len(friends)}")

    # ── Weather ──
    print("🌤️  Fetching weekend weather…")
    weather, friday, saturday, sunday = get_weekend_weather()

    weekend_label = f"{saturday.strftime('%B %d')} – {sunday.strftime('%B %d, %Y')}"
    print(f"   Weekend: {weekend_label}")

    # ── Google Calendar ──
    print("📅 Fetching Google Calendar events…")
    calendar_events = get_weekend_calendar(friday, saturday, sunday)
    open_windows    = find_open_windows(calendar_events, friday, saturday, sunday)
    print(f"   Open windows: {len(open_windows)}")

    # ── Filter Airtable events ──
    today = datetime.date.today()
    cutoff_radar = today + datetime.timedelta(days=75)

    radar_events = []
    for e in all_events:
        d = parse_event_date(e.get("Date", ""))
        if d is None or d < today:
            continue
        if d <= cutoff_radar:
            radar_events.append(e)

    radar_events.sort(key=lambda e: parse_event_date(e.get("Date", "")) or datetime.date.max)

    print(f"   Radar events (15–75 days): {len(radar_events)}")

    # ── Radar calendar — detect conflicts for "Coming Up" cards ──
    print("📅 Fetching radar-window calendar events (15–75 days)…")
    radar_calendar = get_radar_calendar(today)

    # Index calendar events by ISO date for O(1) lookup
    cal_by_date = {}
    for ce in radar_calendar:
        cal_by_date.setdefault(ce["date"], []).append(ce)

    for e in radar_events:
        start, end = parse_event_date_range(e.get("Date", ""))
        conflicts = []
        if start and end:
            d = start
            while d <= end:
                for ce in cal_by_date.get(d.isoformat(), []):
                    entry = {
                        "summary":  ce["summary"],
                        "calendar": ce["calendar"],
                        "all_day":  ce["all_day"],
                    }
                    if not ce["all_day"] and "time" in ce:
                        entry["time"] = ce["time"]
                    conflicts.append(entry)
                d += datetime.timedelta(days=1)
        e["calendar_conflicts"] = conflicts

    # ── Build Claude prompt ──
    system_prompt = """You are generating a Weekend Brief HTML page for the Stevenson family in Charlotte, NC.
Return ONLY the complete, self-contained HTML. No markdown, no code fences, no explanation."""

    friends_summary = [
        {
            "name":               f.get("Name", ""),
            "kids_at_ccd":        f.get("Kids at CCD", ""),
            "invite_to_weekends": f.get("Invite to Weekends", ""),
            "tier":               f.get("Tier", ""),
            "connection":         f.get("Connection", ""),
        }
        for f in friends
    ]

    if len(radar_events) > RADAR_EVENT_CAP:
        print(f"   ⚠️  Trimming radar events: {len(radar_events)} → {RADAR_EVENT_CAP} (raise WB_RADAR_CAP to keep more)")
    radar_events_for_prompt = radar_events[:RADAR_EVENT_CAP]

    # NOTE: We do NOT ask Claude to write the feedback JS. It kept dropping
    # `mode: "no-cors"`, which breaks the cross-origin POST to Apps Script.
    # Instead we inject a guaranteed-correct shim after Claude returns HTML.
    # Claude just needs to call sendFeedback(type, name, vote, currentPerson).

    # Static design requirements — identical every run, sent as a cached prompt block.
    static_instructions = """## DESIGN REQUIREMENTS

Produce a complete, self-contained, mobile-first HTML file. Key requirements:

1.  **Auth gate** — Wrap the ENTIRE brief in `<div id="main-content" style="display:none">…</div>`.
    Before it, render a centered login screen with id="auth-gate" on a navy-to-blue gradient background
    (#1b2838 → #2d4a6f → #3a7bd5), containing the title "Weekend Brief", a short tagline, and ONE button
    with id="passkey-login-btn" that reads "Sign in with Face ID / Touch ID". The button's onclick should
    call `handlePasskeyLogin()`. Do NOT write any auth JavaScript yourself — it will be injected after you
    finish. Do NOT include a password field anywhere.

2.  **Section 1 — Header**
    Navy-to-blue gradient (#1b2838 → #2d4a6f → #3a7bd5). Shows "Weekend Brief",
    date pill (e.g. "Apr 19 – 20"), and John/Sara person toggle.

3.  **Section 2 — Weather Strip**
    3-day forecast bar directly below the header: Fri / Sat / Sun.
    Each day: icon, high/low, rain %. Always visible, not in a tab.

4.  **Tab bar — 2 tabs:** "This Weekend" | "Coming Up"
    Sits directly below the weather strip.

5.  **Tab 1 — "This Weekend"** contains two sub-sections stacked vertically:

    **5a. "Your Weekend" Timeline** (top of tab, always shown first)
    A clean day-by-day list of calendar events merged from all 3 calendars.
    Format example:

        FRIDAY
          ├─ 5:00 PM – 10:00 PM  → Nothing planned ✨

        SATURDAY
          ├─ 8:15 AM   Will soccer                     [S]
          ├─ 11:00 AM  Cam soccer + Cam t-ball          [F]
          ├─ 3:00 PM – 10:00 PM  → Nothing planned ✨

        SUNDAY
          └─ All day free ✨

    Design rules:
    - Compact, no cards — just a clean list with comfortable tap targets
    - Each calendar event: time + title + location if available
    - Tag each event with a subtle badge: [J] John, [S] Sara, [F] Family
    - "Nothing planned" / "All day free" lines: lighter color with ✨ —
      these correspond to open windows and are the visual hooks for suggestions below
    - If CALENDAR EVENTS is empty, show "Calendar sync coming soon" in soft gray

    **5b. "Suggestions"** (directly below the timeline, same tab)

6.  **Suggestions section** (inside "This Weekend" tab, below timeline)**
    One suggestion card per open window (max 4–5 total), generated by Claude
    based on: the open window context, weather that day, restaurants list,
    events list, friends list, and John vs. Sara mode.

    Card format:
    - Emoji + "FRIDAY EVENING" / "SATURDAY AFTERNOON" etc. as the window label
    - Bold suggestion title (restaurant name or activity)
    - 2–3 sentence body: why this fits (weather, what came before, vibe)
    - Detail chips: neighborhood, price range, kids-friendly or date night indicator
    - "Who to invite" chip on family/group suggestions (name from Friends list)
    - Feedback row: ❤️ Love / 👎 Nope / 👀 Interested / 🔄 Swap

    Suggestion rules:
    - Friday evening free → date night unless Sat AM is packed (then suggest rest)
    - Sat afternoon free after busy morning → low-key family activity or easy dinner
    - Sunday all day free → adventure or group hangout, brunch pick
    - Rainy forecast → indoor options; beautiful day → outdoor / patio
    - John mode = family/kids emphasis; Sara mode = date-night emphasis
    - Each card MUST have: data-record-id="<_record_id from source JSON>",
      data-name="<Name>", data-type="restaurant" or "event"

7.  **Tab 2 — "Coming Up"**
    All events from 15–75 days out, sorted by date. Compact radar-cards.
    Each: date, event name, venue, price range, "Who to invite" chip where relevant.
    Each card MUST have: data-record-id="<_record_id>", data-name="<Name>", data-type="event".
    Feedback row on each card.

    **Calendar conflict awareness (IMPORTANT):**
    - If an event has a non-empty `calendar_conflicts` array, show a subtle conflict indicator on the card.
    - Use a small banner or badge: "⚠️ You have [summary] ([calendar]) that day" in a warm amber/yellow tone.
    - If multiple conflicts, show the first one and "+N more" if needed.
    - If `calendar_conflicts` is empty, show a subtle green "✅ Calendar looks clear" indicator.
    - This helps the family decide whether to pursue an event or skip it because they're already booked.

8.  **Feedback behavior (CRITICAL)**
    - Four buttons per card: Love it ❤️ / Nope 👎 / Interested 👀 / Swap 🔄
    - Every button tap: (1) toggle visual selected state, (2) call sendFeedback(type, name, vote, currentPerson).
    - vote strings: "love", "nope", "interested", "swap" — all lowercase.
    - type and name come from the card's data-type and data-name attributes.
    - DO NOT define sendFeedback yourself — it will be injected after you finish. Just call it.

9.  **Feedback footer** — sticky bottom bar showing reaction count only.

10. **CSS palette:**
    - Header gradient: #1b2838 → #2d4a6f → #3a7bd5
    - Background: #f5f5f7
    - Cards: white, border-radius 18px, subtle shadow
    - Timeline section: white card, clean list, "nothing planned" lines in #9ca3af
    - Suggestion window labels: Friday evening=#1b2838, Saturday=#1a6fb5, Sunday=#8b3a9f
    - Tags: Saturday=#e8f4fd/#1a6fb5, Sunday=#fef3e2/#b5761a,
            Date Night=#f5e6f8/#8b3a9f, Family=#fff3e0/#e65100,
            Event=#e8fde8/#1a6b2a
    - Calendar badges [J] [S] [F]: small pill, #e5e7eb background, #6b7280 text

11. **JS** — Do NOT write ANY JavaScript. All UI handlers
    (setPerson, switchTab, handleFeedback, showToast) plus the
    feedback shim and passkey auth are injected by Python after
    your HTML is generated. Do not include any `<script>` tags.

Write vivid, specific Charlotte copy. Two young boys (Will and Cam). Mix of family days and date nights.
Tone: knowledgeable friend, not a concierge.
"""

    # Dynamic data — changes every run, sent uncached.
    dynamic_data = f"""Generate the Weekend Brief HTML for the weekend of {weekend_label}.

## WEATHER DATA (Charlotte, NC)
{json.dumps(weather, indent=2)}

## CALENDAR EVENTS (John + Sara + Family)
{json.dumps(calendar_events, indent=2)}

## OPEN WINDOWS (free time slots this weekend)
{json.dumps(open_windows, indent=2)}

## COMING UP — EVENTS (15–75 days out, for the "Coming Up" tab)
{json.dumps(radar_events_for_prompt, indent=2)}

## RESTAURANTS (full list — use for Suggestions)
{json.dumps(restaurants, indent=2)}

## FRIENDS / FAMILIES (for "who to invite" callouts)
{json.dumps(friends_summary, indent=2)}
"""

    print(f"🤖 Calling Claude API to generate HTML (model: {CLAUDE_MODEL})…")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": static_instructions, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": dynamic_data},
            ],
        }],
    )

    html = message.content[0].text.strip()

    # Strip accidental code fences
    for fence in ("```html", "```"):
        if html.startswith(fence):
            html = html[len(fence):]
    if html.endswith("```"):
        html = html[:-3]
    html = html.strip()

    # ── Inject UI shim (tabs, person toggle, feedback handler, toast) ──
    # Defined here (not in the LLM prompt) so it can never be truncated by
    # token limits. Robust to past variations in the LLM-generated HTML
    # (e.g. handleFeedback may be called with 2 or 4 args; toast id may
    # be 'toast' or 'toast-msg').
    ui_shim = """
<script>
/* Injected by generate_brief.py — UI handlers. */
(function() {
  var currentPerson = 'john';
  var totalReactions = 0;

  window.setPerson = function(p) {
    currentPerson = p;
    var j = document.getElementById('btn-john');
    var s = document.getElementById('btn-sara');
    if (j) j.classList.toggle('active', p === 'john');
    if (s) s.classList.toggle('active', p === 'sara');
    if (p === 'sara') document.body.classList.add('sara-mode');
    else document.body.classList.remove('sara-mode');
  };

  window.switchTab = function(tabId) {
    document.querySelectorAll('.tab-btn').forEach(function(btn) {
      var oc = btn.getAttribute('onclick') || '';
      var dt = btn.getAttribute('data-tab');
      var match = (dt === tabId)
        || (oc.indexOf("'" + tabId + "'") !== -1)
        || (oc.indexOf('"' + tabId + '"') !== -1);
      btn.classList.toggle('active', match);
    });
    document.querySelectorAll('.tab-content').forEach(function(tc) {
      tc.classList.toggle('active', tc.id === 'tab-' + tabId);
    });
  };

  window.handleFeedback = function() {
    var btn = arguments[0], type, name, vote;
    if (arguments.length >= 4) {
      type = arguments[1]; name = arguments[2]; vote = arguments[3];
    } else {
      vote = arguments[1];
      var card = btn.closest('[data-record-id], .suggestion-card, .coming-card');
      type = (card && card.dataset.type) ? card.dataset.type : 'unknown';
      name = (card && card.dataset.name) ? card.dataset.name : 'unknown';
    }
    var row = btn.parentElement;
    var wasSelected = btn.classList.contains('selected-' + vote);
    row.querySelectorAll('.fb-btn').forEach(function(b) { b.className = 'fb-btn'; });
    if (!wasSelected) {
      btn.classList.add('selected-' + vote);
      totalReactions++;
      if (typeof window.sendFeedback === 'function') {
        window.sendFeedback(type, name, vote, currentPerson);
      }
      var emoji = { love: '❤️', nope: '👎', interested: '👀', swap: '🔄' };
      showToast((emoji[vote] || '') + ' Got it!');
    } else {
      totalReactions = Math.max(0, totalReactions - 1);
    }
    var counter = document.getElementById('reaction-count');
    if (counter) counter.textContent = totalReactions;
  };

  window.showToast = function(msg) {
    var t = document.getElementById('toast-msg') || document.getElementById('toast');
    if (!t) return;
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(function() { t.classList.remove('show'); }, 2000);
  };
})();
</script>
"""

    # ── Inject feedback shim (guaranteed-correct sendFeedback) ──
    feedback_shim = f"""
<script>
/* Injected by generate_brief.py — do not rely on Claude to write this. */
(function() {{
  window.FEEDBACK_ENDPOINT = {json.dumps(FEEDBACK_ENDPOINT)};

  var lastCard = null;
  document.addEventListener('click', function(e) {{
    var card = e.target.closest('[data-record-id]');
    if (card) lastCard = card;
  }}, true);  /* capture phase — runs before onclick handlers */

  window.sendFeedback = function(type, name, vote, person) {{
    if (!window.FEEDBACK_ENDPOINT) return;
    var recordId = lastCard ? lastCard.getAttribute('data-record-id') : null;
    var authedUser = window.AUTHENTICATED_USER || null;
    fetch(window.FEEDBACK_ENDPOINT, {{
      method: "POST",
      mode: "no-cors",
      body: JSON.stringify({{
        type: type,
        name: name,
        vote: vote,
        person: person,
        authenticatedUser: authedUser,
        recordId: recordId
      }})
    }}).then(function() {{ if (typeof showToast === 'function') showToast('✓ Sent'); }})
      .catch(function() {{ if (typeof showToast === 'function') showToast('⚠ No connection'); }});
  }};
  window.postFeedback = window.sendFeedback; /* alias */
}})();
</script>
"""
    # ── Inject passkey auth shim ──
    auth_shim = f"""
<script>
/* Injected by generate_brief.py — WebAuthn passkey auth. */
(function() {{
  var AUTH_API = {json.dumps(PASSKEY_AUTH_URL)};
  if (!AUTH_API) {{ console.warn('PASSKEY_AUTH_URL not set; auth disabled.'); return; }}

  function b64urlToBuf(s) {{
    var pad = '='.repeat((4 - (s.length % 4)) % 4);
    var bin = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes.buffer;
  }}
  function bufToB64url(buf) {{
    var bytes = new Uint8Array(buf), s = '';
    for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
  }}

  function showApp() {{
    var gate = document.getElementById('auth-gate');
    var main = document.getElementById('main-content');
    if (gate) gate.style.display = 'none';
    if (main) main.style.display = 'block';
  }}

  async function checkSession() {{
    try {{
      var r = await fetch(AUTH_API + '/verify', {{ credentials: 'include' }});
      var d = await r.json();
      if (d.authenticated) {{
        window.AUTHENTICATED_USER = d.user || null;
        showApp();
      }}
    }} catch (e) {{ /* stay on login */ }}
  }}

  window.handlePasskeyLogin = async function() {{
    if (!window.PublicKeyCredential) {{
      alert('This browser does not support passkeys. Use Safari, Chrome, or Edge on a modern device.');
      return;
    }}
    try {{
      var beginResp = await fetch(AUTH_API + '/login/begin', {{
        method: 'POST', credentials: 'include',
        headers: {{ 'Content-Type': 'application/json' }},
        body: '{{}}'
      }});
      var options = await beginResp.json();
      if (!beginResp.ok) throw new Error(options.error || 'Begin failed');

      options.challenge = b64urlToBuf(options.challenge);
      if (options.allowCredentials) {{
        options.allowCredentials = options.allowCredentials.map(function(c) {{
          return Object.assign({{}}, c, {{ id: b64urlToBuf(c.id) }});
        }});
      }}

      var cred = await navigator.credentials.get({{ publicKey: options }});
      var body = {{
        id: cred.id,
        rawId: bufToB64url(cred.rawId),
        type: cred.type,
        response: {{
          clientDataJSON:    bufToB64url(cred.response.clientDataJSON),
          authenticatorData: bufToB64url(cred.response.authenticatorData),
          signature:         bufToB64url(cred.response.signature),
          userHandle: cred.response.userHandle ? bufToB64url(cred.response.userHandle) : null
        }},
        clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {{}}
      }};
      var completeResp = await fetch(AUTH_API + '/login/complete', {{
        method: 'POST', credentials: 'include',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(body)
      }});
      var result = await completeResp.json();
      if (result.authenticated) {{
        window.AUTHENTICATED_USER = result.user || null;
        showApp();
      }} else {{
        alert('Authentication failed. Try again.');
      }}
    }} catch (e) {{
      console.error('Passkey login error:', e);
      alert('Passkey login failed. Make sure a passkey is registered for this site.');
    }}
  }};

  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', checkSession);
  }} else {{
    checkSession();
  }}
}})();
</script>
"""

    if "</body>" in html:
        html = html.replace("</body>", ui_shim + feedback_shim + auth_shim + "</body>")
    else:
        html += ui_shim + feedback_shim + auth_shim

    # ── Save ──
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    os.makedirs("personal-assistant", exist_ok=True)
    with open("personal-assistant/weekend_brief.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ Weekend Brief written for {weekend_label}")
    usage = message.usage
    cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read   = getattr(usage, "cache_read_input_tokens", 0) or 0
    print(f"   Tokens: {usage.input_tokens} in / {usage.output_tokens} out "
          f"(cache: {cache_read} read, {cache_create} write)")


if __name__ == "__main__":
    main()
