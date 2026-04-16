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

# Google Apps Script feedback endpoint — set once deployed (GitHub Secret: FEEDBACK_ENDPOINT).
# Leave blank until then; feedback buttons still work visually, just won't write back yet.
FEEDBACK_ENDPOINT = os.environ.get("FEEDBACK_ENDPOINT", "")

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

    # Flatten record_id into fields so Claude sees it alongside name, price, etc.
    # This lets the prompt require data-record-id="recXXX" on each card.
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

    # Restaurants — exclude Vetoed and Nope/Swap feedback
    restaurants = fetch_airtable(RESTAURANTS_BASE_ID,
                                  filter_formula="AND(NOT({Vetoed}='Yes'), NOT({Feedback}='Nope'), NOT({Feedback}='Swap'))")

    # Events — all records; filter by date and Feedback in Python
    all_events_raw = fetch_airtable(EVENTS_BASE_ID)
    # Exclude Nope/Swap events upfront
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

    # Build a friends lookup for matching to events
    friends_summary = [
        {
            "name": f.get("Name", ""),
            "kids_at_ccd": f.get("Kids at CCD", ""),
            "invite_to_weekends": f.get("Invite to Weekends", ""),
            "tier": f.get("Tier", ""),
            "connection": f.get("Connection", ""),
        }
        for f in friends
    ]

    # NOTE: We do NOT ask Claude to write the feedback JS. It kept dropping
    # `mode: "no-cors"`, which breaks the cross-origin POST to Apps Script.
    # Instead we inject a guaranteed-correct shim after Claude returns HTML
    # (see "Feedback shim" block below). Claude just needs to call
    # sendFeedback(type, name, vote, currentPerson) — our shim defines it.

    user_prompt = f"""
Generate the Weekend Brief HTML for the weekend of {weekend_label}.

## WEATHER DATA (Charlotte, NC)
{json.dumps(weather, indent=2)}

## THIS WEEKEND EVENTS (next 14 days)
{json.dumps(this_weekend_events, indent=2)}

## COMING UP — EVENTS (15–75 days out)
{json.dumps(radar_events[:25], indent=2)}

## RESTAURANTS (full list — use for curated picks this weekend)
{json.dumps(restaurants, indent=2)}

## FRIENDS / FAMILIES (for "who to invite" callouts on events)
{json.dumps(friends_summary, indent=2)}

## DESIGN REQUIREMENTS

Produce a complete, self-contained, mobile-first HTML file. Key requirements:

1.  **Password gate** — passcode is "stevenson". On correct entry, show the main app div.

2.  **Header** — navy-to-blue gradient. Shows "Weekend Brief", date pill (e.g. "Apr 19 – 20"),
    John/Sara person toggle, and a 3-day weather strip (Fri/Sat/Sun) from the weather data above.

3.  **Tab bar — 2 tabs only:** "This Weekend" | "Coming Up"

4.  **This Weekend tab**
    - Calendar callout (green card) if there's a notable event this weekend.
    - 3–4 curated rec-cards: restaurant/activity combos suited to the weather and family context.
      Each card has: tag pill (Saturday/Sunday/Date Night/Family), title, 2–3 sentence body,
      detail chips (neighborhood, price, kids-friendly), feedback row.
    - Each card MUST have: data-record-id="<_record_id from the source JSON>", data-name="<Name field>", data-type="restaurant" or "event". The _record_id is non-negotiable — the feedback loop depends on it.
    - John mode = kids-friendly places; Sara mode = date-night emphasis.

5.  **Coming Up tab** — all upcoming events from 15–75 days out, sorted by date.
    - Use radar-cards (compact layout). Each shows: date, event name, venue, price range.
    - For each event, add a "Who to invite" chip if friends match:
      Kids Friendly event → suggest families where Kids at CCD = Yes.
      Date Night / adult event → suggest couples (no kids mention).
      Match by name from the friends list.
    - Each card MUST have: data-record-id="<_record_id>", data-name="<Name>", data-type="event".
    - Feedback row on each card.

6.  **Feedback behavior (CRITICAL)**
    - Four buttons per card: Love it ❤️ / Nope 👎 / Interested 👀 / Swap 🔄
    - Every button tap: (1) toggle visual selected state, (2) call sendFeedback(type, name, vote, currentPerson).
    - vote strings: "love", "nope", "interested", "swap" — all lowercase.
    - type ("restaurant" or "event") and name come from the card's data-type and data-name attributes.
    - Nope and Swap → item disappears from next Monday's brief automatically (handled server-side).
    - Love → item gets priority placement next week.
    - Interested → item stays visible, flagged for follow-up.
    - DO NOT define sendFeedback yourself — it will be injected into the page after you finish. Just call it.

7.  **Feedback footer** — sticky bottom bar showing reaction count only. No "Copy Feedback" button —
    feedback is sent automatically on every tap via sendFeedback().

8.  **CSS palette:**
    - Header gradient: #1b2838 → #2d4a6f → #3a7bd5
    - Background: #f5f5f7
    - Cards: white, border-radius 18px, subtle shadow
    - Tags: Saturday=#e8f4fd/#1a6fb5, Sunday=#fef3e2/#b5761a,
            Date Night=#f5e6f8/#8b3a9f, Family=#fff3e0/#e65100,
            Event=#e8fde8/#1a6b2a, Concert=#e8eaf6/#283593

9.  **JS** — password unlock, tab switching, John/Sara toggle, feedback toggling,
    reaction count, showToast(). No copyFeedback(). No external libraries. All inline.

Write vivid, specific Charlotte copy. Two young boys. Mix of family days and date nights.
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
    # This runs AFTER Claude's HTML, so it overrides whatever Claude wrote.
    # - mode:"no-cors" is required (CORS blocks the cross-origin POST otherwise)
    # - Record ID capture: a capture-phase click listener remembers the last
    #   card with a data-record-id that was clicked, and sendFeedback includes
    #   it in the payload so Apps Script can update by ID (not fragile name lookup).
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
