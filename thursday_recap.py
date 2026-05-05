#!/usr/bin/env python3
"""
Thursday Recap — Weekend Brief
================================
Runs every Thursday at 7 PM ET via GitHub Actions.

Reads the Feedback Log from Airtable (last 7 days), calls Claude to write
a friendly recap email, then sends it to John and Sara via the Gmail API.

Required GitHub Secrets (same as generate_brief.py, plus Gmail scope):
  AIRTABLE_API_KEY      — Airtable Personal Access Token
  ANTHROPIC_API_KEY     — Claude API key
  GOOGLE_CLIENT_ID      — OAuth 2.0 Client ID
  GOOGLE_CLIENT_SECRET  — OAuth 2.0 Client Secret
  GOOGLE_REFRESH_TOKEN  — Refresh token with calendar.readonly + gmail.send scopes
"""

import os
import sys
import json
import base64
import datetime
from zoneinfo import ZoneInfo
import requests
import anthropic
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest

# ── Config ───────────────────────────────────────────────────────────────────
AIRTABLE_API_KEY     = os.environ["AIRTABLE_API_KEY"]
ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_CLIENT_ID     = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

FEEDBACK_LOG_BASE_ID = os.environ.get("WB_AT_FEEDBACK_BASE", "appvI8vByeBsxegHZ")
FEEDBACK_LOG_TABLE   = os.environ.get("WB_AT_FEEDBACK_TABLE", "Feedback Log")

RECIPIENTS = ["jlstevenson2@gmail.com", "sara.smith.stevenson@gmail.com"]

ET = ZoneInfo("America/New_York")

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


# ── Google Auth ──────────────────────────────────────────────────────────────

def get_google_creds():
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    creds.refresh(GoogleRequest())
    return creds


# ── Airtable ─────────────────────────────────────────────────────────────────

def fetch_feedback_log():
    """Fetch all feedback from the last 7 days."""
    since = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat() + "Z"
    filter_formula = f"IS_AFTER({{Timestamp}}, '{since}')"

    records, params = [], {"filterByFormula": filter_formula}
    table_encoded = requests.utils.quote(FEEDBACK_LOG_TABLE)
    url = f"https://api.airtable.com/v0/{FEEDBACK_LOG_BASE_ID}/{table_encoded}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    while True:
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
        params["offset"] = offset

    return [r.get("fields", {}) for r in records]


def categorize_feedback(records):
    """Group feedback by Name+Type. Surfaces:
      - both_loved      → ACTION items (book/plan these)
      - both_noped      → drop from next week
      - disagreements   → they voted on it but differently
      - swaps           → at least one swap request
      - john_solo       → John voted, Sara didn't (broken out by vote)
      - sara_solo       → Sara voted, John didn't (broken out by vote)
    """
    from collections import defaultdict

    votes = defaultdict(lambda: {"John": set(), "Sara": set()})
    for r in records:
        name   = r.get("Name", "").strip()
        type_  = r.get("Type", "").strip()
        person = r.get("Person", "").strip()
        vote   = r.get("Vote", "").strip()
        if name and type_ and person and vote:
            votes[(name, type_)][person].add(vote)

    both_loved, both_noped, disagreements, swaps = [], [], [], []
    john_solo = {"loved": [], "noped": [], "interested": [], "swap": []}
    sara_solo = {"loved": [], "noped": [], "interested": [], "swap": []}

    VOTE_BUCKETS = {"Love": "loved", "Nope": "noped", "Interested": "interested", "Swap": "swap"}

    def solo_buckets_for(vote_set):
        return [VOTE_BUCKETS[v] for v in vote_set if v in VOTE_BUCKETS]

    for (name, type_), people in votes.items():
        john_votes = people.get("John", set())
        sara_votes = people.get("Sara", set())

        item = {"name": name, "type": type_,
                "john": sorted(john_votes), "sara": sorted(sara_votes)}

        # PAIRED — both voted on the same item
        if john_votes and sara_votes:
            if "Swap" in john_votes or "Swap" in sara_votes:
                swaps.append(item)
            elif "Love" in john_votes and "Love" in sara_votes:
                both_loved.append(item)
            elif "Nope" in john_votes and "Nope" in sara_votes:
                both_noped.append(item)
            elif john_votes != sara_votes:
                disagreements.append(item)
            continue

        # SOLO — only one of them voted
        if john_votes and not sara_votes:
            for bucket in solo_buckets_for(john_votes):
                john_solo[bucket].append({"name": name, "type": type_})
        elif sara_votes and not john_votes:
            for bucket in solo_buckets_for(sara_votes):
                sara_solo[bucket].append({"name": name, "type": type_})

    return {
        "both_loved":    both_loved,
        "both_noped":    both_noped,
        "disagreements": disagreements,
        "swaps":         swaps,
        "john_solo":     john_solo,
        "sara_solo":     sara_solo,
        "total":         len(votes),
    }


# ── Claude ───────────────────────────────────────────────────────────────────

def generate_recap_email(categories, weekend_label):
    """Ask Claude to write the recap email body as plain text."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""
Write a short, friendly Thursday recap email for John and Sara Stevenson's Weekend Brief.
Tone: warm, direct, like a smart friend — not a newsletter.

Weekend being recapped: {weekend_label}

FEEDBACK SUMMARY:
{json.dumps(categories, indent=2)}

Email structure (skip any section that has no items — do NOT write "None!"):

1. One-line intro (e.g. "Here's how this week's picks landed:")

2. 🎯 ACTION — both agreed
   Use the items in `both_loved`. This is the most important section.
   Phrase as a clear next step, e.g. "Book a table at <name>" for restaurants
   or "Get tickets / put on the calendar for <name>" for events.
   Lead the email with this section if it's non-empty.

3. ❤️ John liked these (no vote from Sara yet)
   List items from `john_solo.loved` and `john_solo.interested` together.
   Phrase as: "Worth running by Sara — she hasn't weighed in yet."

4. ❤️ Sara liked these (no vote from John yet)
   Same idea, from `sara_solo.loved` and `sara_solo.interested`.
   Phrase as: "Worth running by John."

5. 🤔 Split decisions
   From `disagreements` — name the item and who voted what
   (e.g. "Coquette: John ❤️, Sara 👎").

6. 👎 Both passed
   From `both_noped`. One short line: "Dropping from next week: <names>".

7. 🔄 Swap requests
   From `swaps`. One short line each.

8. 💡 2-3 fresh ideas for next weekend
   Suggest by category (date night, family activity, brunch). Charlotte-specific.

9. One closing line.

Rules:
- Skip empty sections entirely.
- Keep total length under 220 words.
- Plain text only — no markdown, no HTML, no bullet symbols beyond simple dashes.
- "ACTION — both agreed" is the headline. Make it stand out at the top of the email.
- Sign off as: — Weekend Brief Bot
"""

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


# ── Gmail Send ───────────────────────────────────────────────────────────────

def send_email(creds, subject, body, recipients):
    """Send a plain-text email via the Gmail API."""
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"]    = "jlstevenson2@gmail.com"
    msg["To"]      = ", ".join(recipients)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type":  "application/json",
    }
    resp = requests.post(url, headers=headers, json={"raw": raw})
    resp.raise_for_status()
    return resp.json()


# ── Main ──────────────────────────────────────────────────────────────────────

def is_correct_schedule_slot(expected_et_hour):
    """Mirror of the Monday-brief gate: skip schedule firings outside the target ET hour."""
    if os.environ.get("GITHUB_EVENT_NAME", "") != "schedule":
        return True
    now_et = datetime.datetime.now(ET)
    if now_et.hour == expected_et_hour:
        return True
    print(f"⏭️  Scheduled run at {now_et.strftime('%H:%M %Z')} — not the {expected_et_hour:02d}:00 ET slot, exiting.")
    return False


def main():
    if not is_correct_schedule_slot(19):
        sys.exit(0)

    print("📋 Fetching Feedback Log from Airtable…")
    records = fetch_feedback_log()
    print(f"   Feedback rows (last 7 days): {len(records)}")

    # Label for the weekend just passed (last Saturday)
    today = datetime.date.today()
    last_saturday = today - datetime.timedelta(days=(today.weekday() + 2) % 7)
    last_sunday   = last_saturday + datetime.timedelta(days=1)
    weekend_label = f"{last_saturday.strftime('%B %d')} – {last_sunday.strftime('%B %d')}"

    creds = get_google_creds()

    if not records:
        print("   No feedback this week — sending a short heads-up email instead of skipping.")
        body = (
            f"No feedback came in for the weekend of {weekend_label}.\n\n"
            "Either nobody tapped the buttons, or the feedback endpoint isn't recording.\n"
            "If you expected votes, check that FEEDBACK_ENDPOINT is set and the Apps Script is deployed.\n\n"
            "— Weekend Brief Bot"
        )
        subject = f"Weekend Brief Recap — {weekend_label} (no feedback this week)"
        result = send_email(creds, subject, body, RECIPIENTS)
        print(f"   Sent heads-up. Message ID: {result.get('id', 'unknown')}")
        return

    categories = categorize_feedback(records)
    js = categories["john_solo"]
    ss = categories["sara_solo"]
    print(f"   🎯 Both loved (ACTION): {len(categories['both_loved'])}")
    print(f"   Both noped: {len(categories['both_noped'])}")
    print(f"   Disagreements: {len(categories['disagreements'])}")
    print(f"   Swaps: {len(categories['swaps'])}")
    print(f"   John solo — loved: {len(js['loved'])}, interested: {len(js['interested'])}, "
          f"noped: {len(js['noped'])}, swap: {len(js['swap'])}")
    print(f"   Sara solo — loved: {len(ss['loved'])}, interested: {len(ss['interested'])}, "
          f"noped: {len(ss['noped'])}, swap: {len(ss['swap'])}")

    print(f"🤖 Generating recap email via Claude (model: {CLAUDE_MODEL})…")
    body = generate_recap_email(categories, weekend_label)

    subject = f"Weekend Brief Recap — {weekend_label}"
    print(f"   Subject: {subject}")

    print("📧 Sending email via Gmail API…")
    result = send_email(creds, subject, body, RECIPIENTS)
    print(f"   Sent! Message ID: {result.get('id', 'unknown')}")
    print("✅ Thursday recap complete.")


if __name__ == "__main__":
    main()
