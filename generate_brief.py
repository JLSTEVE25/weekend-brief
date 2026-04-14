#!/usr/bin/env python3
"""
Weekend Brief Generator
=======================
Runs every Monday at 10 AM ET via GitHub Actions.

Pulls live data from Airtable (Restaurants, Events, Friends),
fetches Charlotte weekend weather (Open-Meteo — no API key needed),
calls Claude API to generate the full HTML, and commits to the repo.

Required GitHub Secrets:
  AIRTABLE_API_KEY   — Airtable Personal Access Token
  AIRTABLE_BASE_ID   — e.g. appXXXXXXXXXXXXXX  (find in your Airtable URL)
  ANTHROPIC_API_KEY  — Claude API key

Optional: verify AIRTABLE_TABLE_* names match your actual Airtable base.
"""

import os
import json
import datetime
import requests
import anthropic

# ── Config ──────────────────────────────────────────────────────────────────
AIRTABLE_API_KEY  = os.environ["AIRTABLE_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Base IDs and table IDs are hardcoded (not secrets — just structural IDs).
# API key is the only secret needed.
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

    return [r["fields"] for r in records]


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

    # Next Friday (weekday 4). If today is already Mon-Thu, find this week's Friday.
    # If today is Fri/Sat/Sun, find next Friday.
    days_to_friday = (4 - today.weekday()) % 7
    if days_to_friday == 0:
        days_to_friday = 7  # always look ahead
    friday = today + datetime.timedelta(days=days_to_friday)
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

    # Restaurants — exclude vetoed
    restaurants = fetch_airtable(RESTAURANTS_BASE_ID,
                                  filter_formula="NOT({Vetoed}='Yes')")

    # Events — all records; we'll filter by date in Python
    all_events = fetch_airtable(EVENTS_BASE_ID)

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

    # ── Filter events ──
    today = datetime.date.today()
    cutoff_near = today + datetime.timedelta(days=14)   # "this weekend + next"
    cutoff_radar = today + datetime.timedelta(days=75)  # "on our radar"

    this_weekend_events, radar_events = [], []
    for e in all_events:
        d = parse_event_date(e.get("Date", ""))
        if d is None:
            continue
        if d < today:
            continue
        if d <= cutoff_near:
            this_weekend_events.append(e)
        elif d <= cutoff_radar:
            radar_events.append(e)

    # Sort
    this_weekend_events.sort(key=lambda e: parse_event_date(e.get("Date", "")) or datetime.date.max)
    radar_events.sort(key=lambda e: parse_event_date(e.get("Date", "")) or datetime.date.max)

    print(f"   This-weekend events: {len(this_weekend_events)}")
    print(f"   Radar events: {len(radar_events)}")

    # ── Build Claude prompt ──
    system_prompt = """You are generating a Weekend Brief HTML page for the Stevenson family in Charlotte, NC.
Return ONLY the complete, self-contained HTML. No markdown, no code fences, no explanation."""

    user_prompt = f"""
Generate the Weekend Brief HTML for the weekend of {weekend_label}.

## WEATHER DATA (Charlotte, NC)
{json.dumps(weather, indent=2)}

## THIS WEEKEND EVENTS (next 14 days)
{json.dumps(this_weekend_events, indent=2)}

## ON OUR RADAR (15–75 days out)
{json.dumps(radar_events[:20], indent=2)}

## RESTAURANTS (full list — use for curated picks)
{json.dumps(restaurants, indent=2)}

## FRIENDS / FAMILIES (Tier 1 & 2 — for "who to invite" callouts)
{json.dumps(friends, indent=2)}

## DESIGN REQUIREMENTS

Produce a complete, self-contained, mobile-first HTML file that exactly replicates the design
of the existing Weekend Brief. Key requirements:

1.  **Password gate** — passcode is "stevenson". On correct entry, show the main app div.

2.  **Header** — navy-to-blue gradient. Shows "Weekend Brief", date pill (e.g. "Apr 19 – 20"),
    John/Sara person toggle, and a 3-day weather strip (Fri/Sat/Sun) using the weather data above.

3.  **Tab bar** — 3 tabs: "This Weekend" | "On Our Radar" | "Plan Ahead"

4.  **This Weekend tab**
    - Calendar callout (green card) if there's a significant event this weekend
    - 3–4 curated rec-cards: pick the best restaurant/activity combos for the weather.
      Each card has: tag pill (Saturday/Sunday/Date Night/Family), title, body text (2–3 sentences,
      personalized, specific), detail chips (neighborhood, price, kids-friendly), feedback row.
    - John mode = include kids-friendly places; Sara mode = emphasize date-night picks.

5.  **On Our Radar tab** — radar-cards for upcoming events within 75 days + interesting restaurants
    the family hasn't tried. Compact layout.

6.  **Plan Ahead tab** — events 30–75 days out. Concise list with dates and action notes.

7.  **Feedback footer** — sticky bottom bar showing reaction count and a "Copy Feedback" button.

8.  **CSS** — use the existing Weekend Brief palette exactly:
    - Header gradient: #1b2838 → #2d4a6f → #3a7bd5
    - Background: #f5f5f7
    - Cards: white, border-radius 18px, subtle shadow
    - Tag colors: Saturday=#e8f4fd/#1a6fb5, Sunday=#fef3e2/#b5761a,
                  Date Night=#f5e6f8/#8b3a9f, Family=#fff3e0/#e65100,
                  Event=#e8fde8/#1a6b2a, Concert=#e8eaf6/#283593

9.  **JS** — implement: password unlock, tab switching, John/Sara toggle (hide/show cards),
    feedback button toggling (love/nope/swap/interested), reaction count, copyFeedback().

10. The HTML must work offline (no external JS libraries). All CSS and JS inline in <style>/<script>.

Write vivid, specific, local copy. Reference real Charlotte spots by name. Match the tone of
a knowledgeable friend who knows the family well (two young boys, mix of family and date-night needs).
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
