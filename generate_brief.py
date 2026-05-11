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

from render_weekend import (
    render_weather_html, render_timeline_html,
    render_weekend_ideas_html, render_date_night_html,
)
from render_coming_up import render_coming_up_html

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


# ── Reservation Availability ───────────���─────────────────────────────────────

APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN", "")


def build_reservation_index(restaurants):
    """Build a dict keyed by restaurant Name with platform/URL/phone for deep links."""
    index = {}
    for r in restaurants:
        name = r.get('Name', '')
        if not name:
            continue
        platform = r.get('Reservation_Platform', '')
        platform_url = r.get('Platform_URL', '')
        phone = r.get('Phone', '')
        if platform in ('None', '', None):
            platform = None
        index[name] = {
            'platform': platform,
            'booking_url': platform_url,
            'phone': phone,
            'error': 'no_platform' if not platform else None,
        }
    return index


def check_availability_apify(platform, platform_url, date, party_size, token):
    """Check one restaurant on one date via Apify. Returns list of time strings."""
    if platform == 'Resy':
        actor_id = 'clearpath~resy-api'
        input_data = {
            'startUrls': [platform_url],
            'date': date.strftime('%Y-%m-%d'),
            'partySize': party_size,
            'includeAvailability': True,
        }
    elif platform == 'OpenTable':
        actor_id = 'canadesk~opentable'
        input_data = {
            'startUrls': [platform_url],
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
        print(f"   ⚠️  Apify check failed for {platform}: {e}")
        return []


def check_date_night_availability(date_night_restaurants, reservation_index,
                                  target_date, token):
    """Check availability for Claude's ranked restaurant picks.
    Returns the list with _reservation data attached (slots or deep-link fallback).
    Checks in parallel, then picks the first 3 with available slots."""
    if not token:
        return date_night_restaurants[:3]

    candidates = date_night_restaurants[:5]
    date_str = target_date.isoformat()

    def _check(r):
        name = r.get('name', '')
        info = reservation_index.get(name, {})
        platform = info.get('platform')
        booking_url = info.get('booking_url', '')
        phone = info.get('phone', '')
        party_size = r.get('party_size', 2)

        if not platform or not booking_url:
            return name, []

        if platform == 'Tock':
            return name, []

        slots = check_availability_apify(platform, booking_url, target_date,
                                         party_size, token)
        return name, slots

    print(f"   Checking availability for {len(candidates)} date night picks…")
    results = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_check, r): r for r in candidates}
        for future in as_completed(futures):
            try:
                name, slots = future.result()
                results[name] = slots
            except Exception as e:
                r = futures[future]
                print(f"   ⚠️  Check failed for {r.get('name', '?')}: {e}")
                results[r.get('name', '')] = []

    # Pick first 3 with available slots; fill remainder with deep-link fallbacks
    with_slots = []
    without_slots = []
    for r in candidates:
        name = r.get('name', '')
        slots = results.get(name, [])
        if slots:
            with_slots.append((r, slots))
        else:
            without_slots.append(r)

    final = []
    for r, slots in with_slots[:3]:
        name = r.get('name', '')
        info = reservation_index.get(name, {})
        r['_reservation'] = {
            'platform': info.get('platform'),
            'booking_url': info.get('booking_url', ''),
            'phone': info.get('phone', ''),
            'error': None,
            'friday_date': date_str,
            'saturday_date': date_str,
            'party_size': r.get('party_size', 2),
            'friday_slots': slots if target_date.weekday() == 4 else [],
            'saturday_slots': slots if target_date.weekday() == 5 else [],
        }
        final.append(r)

    # Fill to 3 with fallbacks (deep link only)
    for r in without_slots:
        if len(final) >= 3:
            break
        name = r.get('name', '')
        info = reservation_index.get(name, {})
        r['_reservation'] = {
            'platform': info.get('platform'),
            'booking_url': info.get('booking_url', ''),
            'phone': info.get('phone', ''),
            'error': None,
            'friday_date': date_str,
            'saturday_date': date_str,
            'party_size': r.get('party_size', 2),
            'friday_slots': [],
            'saturday_slots': [],
        }
        final.append(r)

    available_count = len(with_slots)
    print(f"   {available_count}/{len(candidates)} have open slots")
    return final


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


def render_html(weekend_label, weather, calendar_events, open_windows,
                weekend_ideas, date_night, coming_up_notes, radar_events):
    template_path = os.path.join(os.path.dirname(__file__), "template.html")
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()

    today = datetime.date.today()
    friday = today + datetime.timedelta(days=(4 - today.weekday()))
    saturday = friday + datetime.timedelta(days=1)

    weather_html = render_weather_html(weather)
    timeline_html = render_timeline_html(calendar_events, open_windows)
    ideas_html = render_weekend_ideas_html(weekend_ideas)
    dn_html = render_date_night_html(date_night, friday, saturday)
    suggestions_html = ideas_html + "\n" + dn_html
    coming_up_html = render_coming_up_html(radar_events, coming_up_notes)

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

    # ── Reservation deep links (from Airtable platform fields) ──
    reservation_data = build_reservation_index(restaurants)

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
return a JSON object with THREE keys:

### 1. "weekend_ideas" — array of 2-4 activity suggestion objects

These are broader lifestyle recommendations — NOT all restaurant-focused. Think: "here's
what I'd do this weekend." Examples of good ideas:
- "Stay home and watch a movie with the kids — it's going to rain all Saturday"
- "Invite the Blackburns over for a grill out — beautiful weather and you're free all afternoon"
- "Check out the Truist golf tournament at Quail Hollow"
- "Hit the greenway for a family bike ride before it gets hot"
- "Fire up the smoker Saturday morning — you have all day"

Rules:
- Vary the types: home activities, outdoor adventures, social gatherings, local events
- At most ONE can reference a restaurant/bar; the rest should be non-dining activities
- Weather-aware: rainy → indoor; nice weather → outdoor/patio/yard
- Schedule-aware: reference what's on the calendar ("After soccer wraps Saturday morning...")
- Can reference friends from the friends list for "invite" suggestions

Each object:
- "title": short punchy headline (e.g. "Backyard grill night")
- "description": 2-3 sentences, conversational tone, references weather/schedule context
- "emoji": one relevant emoji
- "invite": friend name or null
- "window_day": "friday" | "saturday" | "sunday"
- "window_label": "FRIDAY EVENING", "SATURDAY AFTERNOON", etc.

### 2. "date_night" — object OR null

Only include this if Friday or Saturday evening is free (check open_windows).
If no free evening exists, set "date_night": null.

Structure:
- "intro_text": contextual sentence, e.g. "You're free Friday night. Consider a date night at:"
- "target_day": "friday" or "saturday"
- "restaurants": array of exactly 5 restaurant objects RANKED by preference (best first).
  We check real-time availability and show the first 3 that have open tables.
  Each object:
  - "name": exact Name field from the restaurant record
  - "data_record_id": the _record_id from the restaurant record
  - "neighborhood": from the record
  - "price": e.g. "$$$"
  - "vibe": one short phrase describing the experience (e.g. "Candlelit Southern steakhouse")
  - "party_size": 2 for a couple, 4 if suggesting a double date with friends

Pick 5 restaurants that offer variety (different neighborhoods, cuisines, price points).
Rank them best-fit first. The system will check availability and display the top 3 with open tables.

### 3. "coming_up_notes" — object keyed by event Name

For each event in the COMING UP list, provide a 1-2 sentence note about why it might
be interesting for the family. Key = exact event Name, value = the note string.

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
    weekend_ideas = content.get("weekend_ideas", [])
    date_night = content.get("date_night", None)
    coming_up_notes = content.get("coming_up_notes", {})

    print(f"   Got {len(weekend_ideas)} weekend ideas, "
          f"date_night={'yes' if date_night else 'no'}, "
          f"{len(coming_up_notes)} coming-up notes")

    # Check real-time availability for date night picks (top 3 with open tables)
    if date_night and date_night.get("restaurants"):
        target_day = date_night.get("target_day", "friday")
        target_date = friday if target_day == "friday" else saturday

        if APIFY_API_TOKEN:
            print("🍽️  Checking availability for date night picks…")
            date_night["restaurants"] = check_date_night_availability(
                date_night["restaurants"], reservation_data,
                target_date, APIFY_API_TOKEN)
        else:
            print("   ⚠️  APIFY_API_TOKEN not set — using deep links only.")
            for r in date_night["restaurants"][:3]:
                name = r.get("name", "")
                info = reservation_data.get(name, {})
                r["_reservation"] = {
                    'platform': info.get('platform'),
                    'booking_url': info.get('booking_url', ''),
                    'phone': info.get('phone', ''),
                    'error': None,
                    'friday_date': friday.isoformat(),
                    'saturday_date': saturday.isoformat(),
                    'party_size': r.get('party_size', 2),
                    'friday_slots': [],
                    'saturday_slots': [],
                }
            date_night["restaurants"] = date_night["restaurants"][:3]

    # ── Render HTML from template ──
    html = render_html(weekend_label, weather, calendar_events, open_windows,
                       weekend_ideas, date_night, coming_up_notes, radar_events_for_prompt)

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
