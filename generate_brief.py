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
import time
import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Apify API — optional; if not set, reservation availability is skipped.
APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN", "")

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


# ── Reservation Availability (Apify) ─────────────────────────────────────────

def query_apify(platform, url, date, token, party_size=4):
    """Call an Apify Actor to get reservation slots for one restaurant on one date.
    Returns a list of time strings like ['7:00 PM', '7:30 PM']."""
    if platform == 'Resy':
        actor_id = 'clearpath~resy-api'
        input_data = {
            'urls': [url],
            'date': date.strftime('%Y-%m-%d'),
            'partySize': party_size,
            'includeAvailability': True,
        }
    elif platform == 'OpenTable':
        actor_id = 'canadesk~opentable'
        input_data = {
            'urls': [url],
            'date': date.strftime('%Y-%m-%d'),
            'partySize': party_size,
        }
    else:
        return []

    try:
        run_resp = requests.post(
            f'https://api.apify.com/v2/acts/{actor_id}/runs',
            params={'token': token},
            json=input_data,
            timeout=15,
        )
        run_resp.raise_for_status()
        run_id = run_resp.json()['data']['id']

        for _ in range(12):
            time.sleep(5)
            status_resp = requests.get(
                f'https://api.apify.com/v2/actor-runs/{run_id}',
                params={'token': token},
                timeout=10,
            )
            status = status_resp.json()['data']['status']
            if status == 'SUCCEEDED':
                break
            if status in ('FAILED', 'ABORTED', 'TIMED-OUT'):
                return []
        else:
            return []

        items_resp = requests.get(
            f'https://api.apify.com/v2/actor-runs/{run_id}/dataset/items',
            params={'token': token},
            timeout=10,
        )
        items = items_resp.json()

        slots = []
        for item in items:
            time_str = item.get('time') or item.get('start', '')
            if time_str:
                slots.append(time_str)
        return slots

    except Exception as e:
        print(f"   ⚠️  Apify query failed for {platform}: {e}")
        return []


def get_reservation_times(restaurant, friday, saturday, apify_token):
    """Check reservation availability for one restaurant on Friday + Saturday.
    Returns a dict with slots, platform info, and error state."""
    platform = restaurant.get('Reservation_Platform', 'None')
    platform_url = restaurant.get('Platform_URL', '')
    phone = restaurant.get('Phone', '')

    if platform in ('None', '', None) or not platform_url:
        return {
            'friday_slots': [],
            'saturday_slots': [],
            'platform': None,
            'booking_url': None,
            'phone': phone,
            'error': 'no_platform',
        }

    if platform == 'Tock':
        return {
            'friday_slots': [],
            'saturday_slots': [],
            'platform': 'Tock',
            'booking_url': platform_url,
            'phone': phone,
            'error': 'tock_no_scrape',
        }

    friday_slots = query_apify(platform, platform_url, friday, apify_token)
    saturday_slots = query_apify(platform, platform_url, saturday, apify_token)

    return {
        'friday_slots': friday_slots,
        'saturday_slots': saturday_slots,
        'platform': platform,
        'booking_url': platform_url,
        'phone': phone,
        'error': None,
    }


def fetch_all_reservations(restaurants, friday, saturday, apify_token):
    """Query reservation availability for all restaurants in parallel.
    Returns a dict keyed by restaurant Name."""
    results = {}

    candidates = [
        r for r in restaurants
        if r.get('Reservation_Platform', 'None') not in ('None', '', None)
        and r.get('Platform_URL')
    ]

    if not candidates:
        return results

    print(f"   Checking reservations for {len(candidates)} restaurants…")

    def _query(r):
        return r.get('Name', ''), get_reservation_times(r, friday, saturday, apify_token)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_query, r): r for r in candidates}
        for future in as_completed(futures):
            try:
                name, data = future.result()
                if name:
                    results[name] = data
            except Exception as e:
                r = futures[future]
                print(f"   ⚠️  Reservation check failed for {r.get('Name', '?')}: {e}")

    print(f"   Got reservation data for {len(results)} restaurants")
    return results


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


# ── Render helpers (template-based output) ──────────────────────────────────

def get_weather_icon(code):
    icons = {0: "☀️", 1: "🌤", 2: "⛅", 3: "☁️", 45: "🌫", 48: "🌫",
             51: "🌦", 53: "🌦", 55: "🌧", 61: "🌧", 63: "🌧", 65: "🌧",
             71: "🌨", 73: "🌨", 75: "❄️", 80: "🌦", 81: "🌧", 82: "⛈",
             95: "⛈", 96: "⛈", 99: "⛈"}
    return icons.get(code, "🌤")


def render_weather_html(weather):
    if not weather:
        return '<div class="weather-day"><span class="weather-day-label">No data</span></div>'
    html_parts = []
    for day in weather:
        rain_pct = day.get("rain_pct", 0)
        rain_class = ' high-rain' if rain_pct >= 50 else ''
        html_parts.append(f'''      <div class="weather-day">
        <div class="weather-day-label">{day["day"]}</div>
        <div class="weather-icon">{day.get("icon", "🌤")}</div>
        <div class="weather-temps">{day["high"]}° <span class="weather-low">{day["low"]}°</span></div>
        <div class="weather-rain{rain_class}">💧 {rain_pct}%</div>
      </div>''')
    return "\n".join(html_parts)


def render_timeline_html(calendar_events, open_windows):
    if not calendar_events and not open_windows:
        return '    <div class="calendar-empty">Calendar sync coming soon</div>'
    days = {"FRIDAY": [], "SATURDAY": [], "SUNDAY": []}
    day_map = {"fri": "FRIDAY", "sat": "SATURDAY", "sun": "SUNDAY",
               "friday": "FRIDAY", "saturday": "SATURDAY", "sunday": "SUNDAY"}
    for ev in calendar_events:
        start = ev.get("start", "")
        if "T" in start:
            try:
                dt = datetime.datetime.fromisoformat(start)
                day_key = dt.strftime("%A").upper()
                sort_key = dt.strftime("%H:%M")
            except ValueError:
                continue
        else:
            try:
                dt = datetime.date.fromisoformat(start)
                day_key = dt.strftime("%A").upper()
                sort_key = "00:00"
            except ValueError:
                continue
        if day_key in days:
            ev["_sort_key"] = sort_key
            ev["_time_display"] = dt.strftime("%-I:%M %p").lstrip("0") if "T" in start else "All day"
            days[day_key].append(ev)
    for w in open_windows:
        day_key = day_map.get(w.get("day", "").lower(), "")
        if day_key in days:
            block = w.get("window", "evening")
            label = "Nothing planned ✨" if block != "morning" else "Morning free ✨"
            raw_time = w.get("start_time", "17:00")
            try:
                t = datetime.datetime.strptime(raw_time, "%H:%M")
                display_time = t.strftime("%-I:%M %p").lstrip("0")
            except ValueError:
                display_time = raw_time
            days[day_key].append({
                "_is_free": True, "_sort_key": raw_time,
                "_time_display": display_time, "label": label,
            })
    html_parts = ['    <div class="timeline-card">']
    for day_name in ["FRIDAY", "SATURDAY", "SUNDAY"]:
        events = days[day_name]
        if not events:
            html_parts.append(f'      <div class="timeline-day-header">{day_name}</div>')
            html_parts.append('      <div class="timeline-event free-window"><div class="timeline-connector"><div class="timeline-dot"></div></div><div class="timeline-body"><span class="free-text">All day free ✨</span></div></div>')
            continue
        events.sort(key=lambda e: e.get("_sort_key", "99:99"))
        html_parts.append(f'      <div class="timeline-day-header">{day_name}</div>')
        for ev in events:
            if ev.get("_is_free"):
                html_parts.append(f'''      <div class="timeline-event free-window">
        <div class="timeline-connector"><div class="timeline-dot"></div></div>
        <div class="timeline-time">{ev["_time_display"]}</div>
        <div class="timeline-body"><span class="free-text">{ev["label"]}</span></div>
      </div>''')
            else:
                cal = ev.get("calendar", "family")
                badge_class = "john" if "john" in cal.lower() else "sara" if "sara" in cal.lower() else "family"
                badge_letter = "J" if badge_class == "john" else "S" if badge_class == "sara" else "F"
                time_str = ev.get("_time_display", "All day")
                location_html = f'<div class="timeline-location">{ev["location"]}</div>' if ev.get("location") else ''
                html_parts.append(f'''      <div class="timeline-event has-event">
        <div class="timeline-connector"><div class="timeline-dot"></div></div>
        <div class="timeline-time">{time_str}</div>
        <div class="timeline-body">
          <div class="timeline-title">{ev.get("summary", "Event")}</div>
          {location_html}
        </div>
        <div class="cal-badge {badge_class}">{badge_letter}</div>
      </div>''')
    html_parts.append('    </div>')
    return "\n".join(html_parts)


def render_reservation_html(reservation):
    """Render the reservation availability row for a suggestion card."""
    if not reservation:
        return ''

    error = reservation.get('error')
    phone = reservation.get('phone', '')
    booking_url = reservation.get('booking_url', '')
    platform = reservation.get('platform', '')

    if error == 'no_platform':
        if phone:
            return f'    <div class="reservation-row">📞 No online reservations — <a href="tel:{phone}" class="reservation-link">call to book</a></div>'
        return ''

    if error == 'tock_no_scrape':
        if booking_url:
            return f'    <div class="reservation-row">🕐 <a href="{booking_url}" target="_blank" class="reservation-link">Check availability on Tock →</a></div>'
        return ''

    if error:
        if booking_url:
            return f'    <div class="reservation-row">⚠️ Couldn\'t check availability — <a href="{booking_url}" target="_blank" class="reservation-link">check {platform}</a></div>'
        return ''

    fri_slots = reservation.get('friday_slots', [])
    sat_slots = reservation.get('saturday_slots', [])

    if fri_slots or sat_slots:
        parts = []
        if fri_slots:
            parts.append(f'Fri: {", ".join(fri_slots[:4])}')
        if sat_slots:
            parts.append(f'Sat: {", ".join(sat_slots[:4])}')
        slots_text = ' | '.join(parts)
        link_html = f' <a href="{booking_url}" target="_blank" class="reservation-link">Reserve on {platform} →</a>' if booking_url else ''
        return f'    <div class="reservation-row">🕐 {slots_text}{link_html}</div>'

    if phone:
        return f'    <div class="reservation-row">📞 Fully booked this weekend — <a href="tel:{phone}" class="reservation-link">try calling directly</a></div>'
    if booking_url:
        return f'    <div class="reservation-row">😔 Fully booked this weekend — <a href="{booking_url}" target="_blank" class="reservation-link">check {platform}</a></div>'
    return '    <div class="reservation-row">😔 Fully booked this weekend — try calling directly</div>'


def render_suggestion_card(s):
    day = s.get("window_day", "friday").lower()
    window_class = f"window-{day}" if day in ("friday", "saturday", "sunday") else "window-friday"
    chips_html = ""
    for chip in s.get("chips", []):
        chip_type = chip.get("type", "neighborhood")
        chips_html += f'<span class="chip chip-{chip_type}">{chip.get("label", "")}</span>'
    if s.get("invite"):
        chips_html += f'<span class="chip chip-invite">👋 {s["invite"]}</span>'
    data_type = s.get("data_type", "restaurant")
    data_name = s.get("data_name", "")
    safe_name = data_name.replace("'", "&#39;")
    record_id = s.get("data_record_id", "")
    reservation_html = render_reservation_html(s.get("_reservation"))
    feedback_html = (
        '    <div class="feedback-row">\n'
        f'      <button class="feedback-btn" onclick="handleFeedback(this,\'{data_type}\',\'{safe_name}\',\'love\')"><span>❤️</span><span class="fb-label">Love</span></button>\n'
        f'      <button class="feedback-btn" onclick="handleFeedback(this,\'{data_type}\',\'{safe_name}\',\'nope\')"><span>👎</span><span class="fb-label">Nope</span></button>\n'
        f'      <button class="feedback-btn" onclick="handleFeedback(this,\'{data_type}\',\'{safe_name}\',\'interested\')"><span>👀</span><span class="fb-label">Interested</span></button>\n'
        f'      <button class="feedback-btn" onclick="handleFeedback(this,\'{data_type}\',\'{safe_name}\',\'swap\')"><span>🔄</span><span class="fb-label">Swap</span></button>\n'
        '    </div>'
    )
    reservation_line = f'{reservation_html}\n' if reservation_html else ''
    return (
        f'    <div class="suggestion-card" data-record-id="{record_id}" data-name="{data_name}" data-type="{data_type}">\n'
        f'      <div class="suggestion-window-bar {window_class}">{s.get("emoji","🌙")} {s.get("window_label","EVENING")}</div>\n'
        f'      <div class="suggestion-body">\n'
        f'        <div class="suggestion-title">{s.get("title","")}</div>\n'
        f'        <div class="suggestion-desc">{s.get("description","")}</div>\n'
        f'        <div class="chip-row">{chips_html}</div>\n'
        f'{reservation_line}'
        f'{feedback_html}\n'
        f'      </div>\n'
        f'    </div>'
    )


def render_coming_up_card(event, notes_text):
    date_str = event.get("Date", "")
    d = parse_event_date(date_str)
    if d:
        month_str = d.strftime("%b").upper()
        day_num = d.day
        dow_str = d.strftime("%a")
    else:
        month_str = "TBD"
        day_num = "?"
        dow_str = ""
    name = event.get("Name", "Event")
    venue = event.get("Venue", "")
    record_id = event.get("_record_id", "")
    conflicts = event.get("calendar_conflicts", [])
    if conflicts:
        first = conflicts[0]
        conflict_text = f'⚠️ You have {first.get("summary","")} ({first.get("calendar","")}) that day'
        if len(conflicts) > 1:
            conflict_text += f' +{len(conflicts)-1} more'
        conflict_html = f'<div class="conflict-banner">{conflict_text}</div>'
    else:
        conflict_html = '<div class="clear-banner">✅ Calendar looks clear</div>'
    notes_html = f'<div class="coming-up-notes">{notes_text}</div>' if notes_text else ''
    chips_html = ''
    if event.get("Price"):
        chips_html += f'<span class="chip chip-price">{event["Price"]}</span>'
    if event.get("Neighborhood"):
        chips_html += f'<span class="chip chip-neighborhood">{event["Neighborhood"]}</span>'
    chip_row = f'<div class="chip-row">{chips_html}</div>' if chips_html else ''
    safe_name = name.replace("'", "&#39;")
    feedback_html = (
        '    <div class="feedback-row">\n'
        f'      <button class="feedback-btn" onclick="handleFeedback(this,\'event\',\'{safe_name}\',\'love\')"><span>❤️</span><span class="fb-label">Love</span></button>\n'
        f'      <button class="feedback-btn" onclick="handleFeedback(this,\'event\',\'{safe_name}\',\'nope\')"><span>👎</span><span class="fb-label">Nope</span></button>\n'
        f'      <button class="feedback-btn" onclick="handleFeedback(this,\'event\',\'{safe_name}\',\'interested\')"><span>👀</span><span class="fb-label">Interested</span></button>\n'
        f'      <button class="feedback-btn" onclick="handleFeedback(this,\'event\',\'{safe_name}\',\'swap\')"><span>🔄</span><span class="fb-label">Swap</span></button>\n'
        '    </div>'
    )
    return (
        f'    <div class="coming-up-card" data-record-id="{record_id}" data-name="{name}" data-type="event">\n'
        f'      <div class="coming-up-header">\n'
        f'        <div class="coming-up-date-block">\n'
        f'          <div class="coming-up-month">{month_str}</div>\n'
        f'          <div class="coming-up-day-num">{day_num}</div>\n'
        f'          <div class="coming-up-dow">{dow_str}</div>\n'
        f'        </div>\n'
        f'        <div class="coming-up-info">\n'
        f'          <div class="coming-up-name">{name}</div>\n'
        f'          <div class="coming-up-venue">{venue}</div>\n'
        f'        </div>\n'
        f'      </div>\n'
        f'      <div class="coming-up-body">\n'
        f'        {conflict_html}\n'
        f'        {notes_html}\n'
        f'        {chip_row}\n'
        f'{feedback_html}\n'
        f'      </div>\n'
        f'    </div>'
    )


def render_html(weekend_label, weather, calendar_events, open_windows,
                suggestions, coming_up_notes, radar_events):
    template_path = os.path.join(os.path.dirname(__file__), "template.html")
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()
    weather_html = render_weather_html(weather)
    timeline_html = render_timeline_html(calendar_events, open_windows)
    suggestions_html = "\n".join(render_suggestion_card(s) for s in suggestions)
    coming_up_html = "\n".join(
        render_coming_up_card(ev, coming_up_notes.get(ev.get("Name", ""), ""))
        for ev in radar_events
    )
    html = html.replace("{{WEEKEND_LABEL}}", weekend_label)
    html = html.replace("{{WEATHER_STRIP}}", weather_html)
    html = html.replace("{{TIMELINE_HTML}}", timeline_html)
    html = html.replace("{{SUGGESTIONS_HTML}}", suggestions_html)
    html = html.replace("{{COMING_UP_HTML}}", coming_up_html)
    html = html.replace("{{FEEDBACK_ENDPOINT_JS}}", json.dumps(FEEDBACK_ENDPOINT))
    html = html.replace("{{PASSKEY_AUTH_URL_JS}}", json.dumps(PASSKEY_AUTH_URL))
    return html


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

    # ── Reservation availability (Apify) ──
    reservation_data = {}
    if APIFY_API_TOKEN:
        print("🍽️  Checking reservation availability…")
        reservation_data = fetch_all_reservations(restaurants, friday, saturday, APIFY_API_TOKEN)
    else:
        print("   ⚠️  APIFY_API_TOKEN not set — skipping reservation checks.")

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

    # ── Build Claude prompt (JSON output) ──
    system_prompt = """You generate structured content for the Stevenson family Weekend Brief in Charlotte, NC.
Return ONLY valid JSON. No markdown, no code fences, no explanation."""

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

    static_instructions = """## YOUR TASK

Given weather, calendar events, open windows, restaurants, events, and friends data,
return a JSON object with two keys:

### 1. "suggestions" — array of 4-5 suggestion objects

Each suggestion fills one open window (free time slot). Rules:
- Friday evening free → date night unless Saturday AM is packed
- Saturday afternoon free after busy morning → low-key family activity or easy dinner
- Sunday all day free → adventure, group hangout, or brunch
- Rainy forecast → indoor options; nice weather → outdoor / patio
- Mix family and date-night picks

Each suggestion object has these fields:
- "window_label": e.g. "FRIDAY EVENING", "SATURDAY AFTERNOON", "SUNDAY MORNING"
- "window_day": "friday" | "saturday" | "sunday"
- "emoji": one emoji for the window
- "title": bold suggestion name (restaurant or activity)
- "description": 2-3 sentences explaining why it fits (weather, vibe, what came before)
- "chips": array of {type, label} where type is one of: "neighborhood", "price", "saturday", "sunday", "friday", "date-night", "family", "event"
- "invite": friend/family name to invite (or null)
- "data_type": "restaurant" or "event"
- "data_name": the Name field from the source record
- "data_record_id": the _record_id from the source record

### 2. "coming_up_notes" — object keyed by event Name

For each event in the COMING UP list, provide a 1-2 sentence note about why it might
be interesting for the family. Key = exact event Name, value = the note string.

## RESERVATION AVAILABILITY
If reservation data is provided for a restaurant, weave it naturally into the description.
For example: "Tables open at 7 and 7:30 Friday — grab one before they fill up."
If a restaurant is fully booked, suggest calling or trying a different night.
Don't list exact times in the description (those are shown separately in the card).

## TONE
Knowledgeable friend, not a concierge. Vivid, specific Charlotte copy.
Two young boys (Will and Cam). Mix of family days and date nights.
"""

    dynamic_data = f"""Generate content for the weekend of {weekend_label}.

## WEATHER DATA (Charlotte, NC)
{json.dumps(weather, indent=2)}

## CALENDAR EVENTS (John + Sara + Family)
{json.dumps(calendar_events, indent=2)}

## OPEN WINDOWS (free time slots this weekend)
{json.dumps(open_windows, indent=2)}

## COMING UP — EVENTS (15–75 days out)
{json.dumps(radar_events_for_prompt, indent=2)}

## RESTAURANTS (full list — pick from these for suggestions)
{json.dumps(restaurants, indent=2)}

## FRIENDS / FAMILIES (for "who to invite" callouts)
{json.dumps(friends_summary, indent=2)}

## RESERVATION AVAILABILITY (for restaurants with online booking)
{json.dumps(reservation_data, indent=2) if reservation_data else "No reservation data available — APIFY_API_TOKEN not configured."}
"""

    print(f"🤖 Calling Claude API for content JSON (model: {CLAUDE_MODEL})…")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8000,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": static_instructions, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": dynamic_data},
            ],
        }],
    )

    raw = message.content[0].text.strip()
    for fence in ("```json", "```"):
        if raw.startswith(fence):
            raw = raw[len(fence):]
    if raw.endswith("```"):
        raw = raw[:-3]
    raw = raw.strip()

    content = json.loads(raw)
    suggestions = content.get("suggestions", [])
    coming_up_notes = content.get("coming_up_notes", {})

    print(f"   Got {len(suggestions)} suggestions, {len(coming_up_notes)} coming-up notes")

    # Attach reservation data to each suggestion for deterministic HTML rendering
    if reservation_data:
        for s in suggestions:
            name = s.get("data_name", "")
            if name in reservation_data:
                s["_reservation"] = reservation_data[name]

    # ── Render HTML from template ──
    html = render_html(weekend_label, weather, calendar_events, open_windows,
                       suggestions, coming_up_notes, radar_events_for_prompt)

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
