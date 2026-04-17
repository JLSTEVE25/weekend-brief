#!/usr/bin/env python3
"""
Weekend Brief Generator
=======================
Runs every Monday at 10 AM ET via GitHub Actions.

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
import json
import datetime
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

# Google Apps Script feedback endpoint — set once deployed (GitHub Secret: FEEDBACK_ENDPOINT).
FEEDBACK_ENDPOINT = os.environ.get("FEEDBACK_ENDPOINT", "")

# Google Calendar OAuth — optional; if not set, calendar section is skipped.
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")

CALENDAR_IDS = {
    "John":   "jlstevenson2@gmail.com",
    "Sara":   "sara.smith.stevenson@gmail.com",
    "Family": "family00679441475095031757@group.calendar.google.com",
}

# Base IDs and table IDs are hardcoded (not secrets — just structural IDs).
RESTAURANTS_BASE_ID = "appyUA9SEI4R0grrH"
EVENTS_BASE_ID      = "appQEVLUQt03RUIgE"
FRIENDS_BASE_ID     = "appTGMNTmT9weRbjL"
TABLE_NAME          = "Imported table"   # All three bases use this table name

AT_HEADERS = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

# Charlotte, NC coordinates
LAT, LON = 35.2271, -80.8431


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
    """Return weather dicts for Friday, Saturday, Sunday of the upcoming weekend."""
    today = datetime.date.today()

    days_to_friday = (4 - today.weekday()) % 7
    if days_to_friday == 0:
        days_to_friday = 7  # always look ahead
    friday   = today + datetime.timedelta(days=days_to_friday)
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
                # All-day events flag the whole day as "something's happening"
                # but don't block specific time windows
                continue
            s_str = e["start"]
            end_str = e["end"]
            if "T" not in s_str:
                continue
            try:
                # Normalize timezone offset to compare as UTC offset hours
                s_dt   = datetime.datetime.fromisoformat(s_str.replace("Z", "+00:00"))
                end_dt = datetime.datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                # Convert to naive ET (UTC-4 or UTC-5; use UTC-5 as conservative approximation)
                s_et   = s_dt.hour   + s_dt.utcoffset().total_seconds() / 3600 + 5  # adjust to ET
                end_et = end_dt.hour + end_dt.utcoffset().total_seconds() / 3600 + 5
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
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

    # NOTE: We do NOT ask Claude to write the feedback JS. It kept dropping
    # `mode: "no-cors"`, which breaks the cross-origin POST to Apps Script.
    # Instead we inject a guaranteed-correct shim after Claude returns HTML.
    # Claude just needs to call sendFeedback(type, name, vote, currentPerson).

    user_prompt = f"""
Generate the Weekend Brief HTML for the weekend of {weekend_label}.

## WEATHER DATA (Charlotte, NC)
{json.dumps(weather, indent=2)}

## CALENDAR EVENTS (John + Sara + Family)
{json.dumps(calendar_events, indent=2)}

## OPEN WINDOWS (free time slots this weekend)
{json.dumps(open_windows, indent=2)}

## COMING UP — EVENTS (15–75 days out, for the "Coming Up" tab)
{json.dumps(radar_events[:25], indent=2)}

## RESTAURANTS (full list — use for Suggestions)
{json.dumps(restaurants, indent=2)}

## FRIENDS / FAMILIES (for "who to invite" callouts)
{json.dumps(friends_summary, indent=2)}

## DESIGN REQUIREMENTS

Produce a complete, self-contained, mobile-first HTML file. Key requirements:

1.  **Password gate** — passcode is "stevenson". On correct entry, show the main app div.

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

11. **JS** — password unlock, tab switching, John/Sara toggle, feedback toggling,
    reaction count, showToast(). No external libraries. All inline.

Write vivid, specific Charlotte copy. Two young boys (Will and Cam). Mix of family days and date nights.
Tone: knowledgeable friend, not a concierge.
"""

    print("🤖 Calling Claude API to generate HTML…")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    html = message.content[0].text.strip()

    # Strip accidental code fences
    for fence in ("```html", "```"):
        if html.startswith(fence):
            html = html[len(fence):]
    if html.endswith("```"):
        html = html[:-3]
    html = html.strip()

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
    fetch(window.FEEDBACK_ENDPOINT, {{
      method: "POST",
      mode: "no-cors",
      body: JSON.stringify({{type: type, name: name, vote: vote, person: person, recordId: recordId}})
    }}).then(function() {{ if (typeof showToast === 'function') showToast('✓ Sent'); }})
      .catch(function() {{ if (typeof showToast === 'function') showToast('⚠ No connection'); }});
  }};
  window.postFeedback = window.sendFeedback; /* alias */
}})();
</script>
"""
    if "</body>" in html:
        html = html.replace("</body>", feedback_shim + "</body>")
    else:
        html += feedback_shim

    # ── Save ──
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    os.makedirs("personal-assistant", exist_ok=True)
    with open("personal-assistant/weekend_brief.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ Weekend Brief written for {weekend_label}")
    print(f"   Tokens used: {message.usage.input_tokens} in / {message.usage.output_tokens} out")


if __name__ == "__main__":
    main()
