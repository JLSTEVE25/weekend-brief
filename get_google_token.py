#!/usr/bin/env python3
"""
get_google_token.py — One-time Google OAuth consent helper
===========================================================
Run this ONCE on your Mac to authorize Google Calendar access
and get a refresh token to store as a GitHub Secret.

Prerequisites:
  pip install google-auth-oauthlib

Steps:
  1. Go to console.cloud.google.com → your project → APIs & Services →
     Credentials → Create Credentials → OAuth 2.0 Client ID
     - Application type: Desktop app
     - Download the JSON (or just copy the Client ID + Client Secret)
  2. Run: python get_google_token.py
  3. Enter your Client ID and Client Secret when prompted
  4. A browser window opens — sign in with jlstevenson2@gmail.com
  5. Approve calendar access
  6. Copy the 3 values printed to your terminal into GitHub Secrets:
       Settings → Secrets → Actions → New repository secret
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def main():
    print()
    print("=== Google OAuth Token Helper ===")
    print()
    print("You need your OAuth 2.0 Client ID and Client Secret from Google Cloud Console.")
    print("(console.cloud.google.com → APIs & Services → Credentials)")
    print()

    client_id     = input("Paste your Client ID:     ").strip()
    client_secret = input("Paste your Client Secret: ").strip()

    if not client_id or not client_secret:
        print("ERROR: Client ID and Client Secret are required.")
        return

    client_config = {
        "installed": {
            "client_id":     client_id,
            "client_secret": client_secret,
            "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
        }
    }

    print()
    print("Opening browser for authorization… (sign in as jlstevenson2@gmail.com)")
    print()

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0)

    print()
    print("=" * 60)
    print("SUCCESS! Add these 3 values as GitHub Secrets:")
    print("(repo Settings → Secrets → Actions → New repository secret)")
    print("=" * 60)
    print()
    print(f"Secret name:  GOOGLE_CLIENT_ID")
    print(f"Secret value: {client_id}")
    print()
    print(f"Secret name:  GOOGLE_CLIENT_SECRET")
    print(f"Secret value: {client_secret}")
    print()
    print(f"Secret name:  GOOGLE_REFRESH_TOKEN")
    print(f"Secret value: {creds.refresh_token}")
    print()
    print("Done! You will NOT need to run this script again.")
    print("The refresh token is long-lived (does not expire unless revoked).")
    print()
    print("=" * 60)
    print("NEXT STEPS:")
    print("=" * 60)
    print()
    print("(a) You already ran this script — that was step 1.")
    print("(b) You already completed the Google consent flow in the browser.")
    print("(c) Copy the GOOGLE_REFRESH_TOKEN value printed above.")
    print("(d) Go to: github.com/JLSTEVE25/weekend-brief →")
    print("    Settings → Secrets and variables → Actions")
    print("    → Find GOOGLE_REFRESH_TOKEN → Update secret")
    print("    → Paste the new refresh token and save.")
    print()
    print("The new token now includes both calendar.readonly AND gmail.send,")
    print("which is required for the Thursday Recap email to work.")
    print()


if __name__ == "__main__":
    main()
