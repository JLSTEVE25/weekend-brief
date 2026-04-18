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
import json
import base64
import datetime
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

FEEDBACK_LOG_BASE_ID = "appvI8vByeBsxegHZ"
FEEDBACK_LOG_TABLE   = "Feedback Log"

RECIPIENTS = ["jlstevenson2@gmail.com", "sara.smith.stevenson@gmail.com"]

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
    """Group feedback by Name+Type and categorize vote combinations."""
    from collections import defaultdict

    # Group all votes by (name, type)
    votes = defaultdict(lambda: {"John": set(), "Sara": set()})
    for r in records:
        name   = r.get("Name", "").strip()
        type_  = r.get("Type", "").strip()
        person = r.get("Person", "").strip()
        vote   = r.get("Vote", "").strip()
        if name and type_ and person and vote:
            votes[(name, type_)][person].add(vote)

    both_loved, both_noped, disagreements, swaps = [], [], [], []

    for (name, type_), people in votes.items():
        john_votes = people.get("John", set())
        sara_votes = people.get("Sara", set())

        item = {"name": name, "type": type_,
                "john": sorted(john_votes), "sara": sorted(sara_votes)}

        if "Swap" in john_votes or "Swap" in sara_votes:
            swaps.append(item)
        elif "Love" in john_votes and "Love" in sara_votes:
            both_loved.append(item)
        elif "Nope" in john_votes and "Nope" in sara_votes:
            both_noped.append(item)
        elif john_votes and sara_votes and john_votes != sara_votes:
            disagreements.append(item)

    return {
        "both_loved":    both_loved,
        "both_noped":    both_noped,
        "disagreements": disagreements,
        "swaps":         swaps,
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

Email structure:
1. One-line intro (e.g. "Here's how this week's picks landed:")
2. ❤️ You both loved — list items from both_loved (these were already alerted, just recap)
3. 👎 Both passed — list items from both_noped (note they'll drop from next week)
4. 🤔 Split decisions — list disagreements with who voted what
5. 🔄 Swap requests — list items someone wanted swapped
6. 💡 2-3 fresh ideas for next weekend (suggest by category: date night, family activity, brunch)
7. One closing line

Rules:
- If a category has no items, skip it entirely (don't write "None!")
- Keep it under 200 words total
- Plain text only — no markdown, no HTML, no bullet symbols beyond simple dashes
- Sign off as: — Weekend Brief Bot
"""

    message = client.messages.create(
        model="claude-opus-4-6",
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

def main():
    print("📋 Fetching Feedback Log from Airtable…")
    records = fetch_feedback_log()
    print(f"   Feedback rows (last 7 days): {len(records)}")

    if not records:
        print("   No feedback this week — skipping recap email.")
        return

    categories = categorize_feedback(records)
    print(f"   Both loved: {len(categories['both_loved'])}")
    print(f"   Both noped: {len(categories['both_noped'])}")
    print(f"   Disagreements: {len(categories['disagreements'])}")
    print(f"   Swaps: {len(categories['swaps'])}")

    # Label for the weekend just passed (last Saturday)
    today = datetime.date.today()
    last_saturday = today - datetime.timedelta(days=(today.weekday() + 2) % 7)
    last_sunday   = last_saturday + datetime.timedelta(days=1)
    weekend_label = f"{last_saturday.strftime('%B %d')} – {last_sunday.strftime('%B %d')}"

    print("🤖 Generating recap email via Claude…")
    body = generate_recap_email(categories, weekend_label)

    subject = f"Weekend Brief Recap — {weekend_label}"
    print(f"   Subject: {subject}")

    print("📧 Sending email via Gmail API…")
    creds = get_google_creds()
    result = send_email(creds, subject, body, RECIPIENTS)
    print(f"   Sent! Message ID: {result.get('id', 'unknown')}")
    print("✅ Thursday recap complete.")


if __name__ == "__main__":
    main()
