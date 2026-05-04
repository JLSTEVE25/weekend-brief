#!/usr/bin/env python3
"""
notify_failure.py
=================
Called by GitHub Actions when a workflow step fails. Sends a short email to
John via Gmail using the same OAuth refresh token the other scripts use.

Required env (set in the workflow's `if: failure()` step):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
  WORKFLOW_NAME      — e.g. "Generate Weekend Brief"
  RUN_URL            — link to the failed Actions run
  ALERT_RECIPIENT    — defaults to jlstevenson2@gmail.com

This script never raises — if the alert itself fails, we print and exit 0
so the workflow's failure status remains intact (it failed for the *real*
reason, not because the alerter died).
"""

import os
import base64
import sys
import requests
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest


def main():
    try:
        creds = Credentials(
            token=None,
            refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            scopes=["https://www.googleapis.com/auth/gmail.send"],
        )
        creds.refresh(GoogleRequest())

        workflow = os.environ.get("WORKFLOW_NAME", "Weekend Brief workflow")
        run_url  = os.environ.get("RUN_URL", "(no URL)")
        to_addr  = os.environ.get("ALERT_RECIPIENT", "jlstevenson2@gmail.com")

        body = (
            f"A scheduled Weekend Brief workflow failed.\n\n"
            f"Workflow: {workflow}\n"
            f"Run:      {run_url}\n\n"
            "Open the link, scroll to the failing step, and check the logs.\n"
            "Common causes: expired Google refresh token, Anthropic API key revoked,\n"
            "Airtable PAT rotated, or a network blip.\n\n"
            "— Weekend Brief Bot"
        )

        msg = MIMEText(body, "plain")
        msg["Subject"] = f"⚠️ {workflow} failed"
        msg["From"]    = "jlstevenson2@gmail.com"
        msg["To"]      = to_addr

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        resp = requests.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={
                "Authorization": f"Bearer {creds.token}",
                "Content-Type":  "application/json",
            },
            json={"raw": raw},
            timeout=20,
        )
        resp.raise_for_status()
        print(f"📧 Failure alert sent to {to_addr}")
    except Exception as ex:
        print(f"⚠️  Could not send failure alert: {ex}", file=sys.stderr)


if __name__ == "__main__":
    main()
