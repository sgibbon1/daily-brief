#!/usr/bin/env python3
"""
One-time authentication setup for daily_brief.py.
Run this once after completing the steps in README.md.
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SCRIPT_DIR = Path(__file__).parent
CREDENTIALS_DIR = SCRIPT_DIR / "credentials"

ND_EMAIL = os.getenv("ND_EMAIL_ADDRESS", "")
ND_GMAIL_CLIENT_ID = os.getenv("ND_GMAIL_CLIENT_ID", "")
ND_GMAIL_CLIENT_SECRET = os.getenv("ND_GMAIL_CLIENT_SECRET", "")
JHU_EMAIL = os.getenv("JHU_EMAIL_ADDRESS", "")
JHU_CLIENT_ID = os.getenv("JHU_AZURE_CLIENT_ID", "")
JHU_TENANT_ID = os.getenv("JHU_AZURE_TENANT_ID", "common")
JHU_CLIENT_SECRET = os.getenv("JHU_AZURE_CLIENT_SECRET", "")


def setup_gmail():
    print("\n── Notre Dame Gmail (alumni.nd.edu) ──────────────────────────")
    if not ND_GMAIL_CLIENT_ID or not ND_GMAIL_CLIENT_SECRET:
        print("ERROR: ND_GMAIL_CLIENT_ID or ND_GMAIL_CLIENT_SECRET not set in .env")
        print("See README.md step 1 for instructions.\n")
        return False

    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
    token_file = CREDENTIALS_DIR / "gmail_token.json"

    client_config = {
        "installed": {
            "client_id": ND_GMAIL_CLIENT_ID,
            "client_secret": ND_GMAIL_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    print("A browser window will open. Sign in with your alumni.nd.edu account.")
    creds = flow.run_local_server(port=0)
    token_file.write_text(creds.to_json())

    # Quick test
    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    print(f"✓ Gmail authenticated as: {profile.get('emailAddress')}")
    print(f"  Token saved to: {token_file}")
    return True


def setup_jhu():
    print("\n── Johns Hopkins Microsoft Graph (alumni.jh.edu) ─────────────")
    if not JHU_CLIENT_ID:
        print("ERROR: JHU_AZURE_CLIENT_ID not set in .env")
        print("See README.md step 2 for instructions.\n")
        return False

    import msal

    token_file = CREDENTIALS_DIR / "jhu_token.json"
    import msal

    token_cache = msal.SerializableTokenCache()

    GRAPH_SCOPES = ["https://graph.microsoft.com/Mail.ReadWrite"]

    if JHU_CLIENT_SECRET:
        app = msal.ConfidentialClientApplication(
            JHU_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{JHU_TENANT_ID}",
            client_credential=JHU_CLIENT_SECRET,
            token_cache=token_cache,
        )
        result = app.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )
    else:
        app = msal.PublicClientApplication(
            JHU_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{JHU_TENANT_ID}",
            token_cache=token_cache,
        )
        print("A browser window will open. Sign in with your alumni.jh.edu account.")
        result = app.acquire_token_interactive(
            scopes=GRAPH_SCOPES, login_hint=JHU_EMAIL
        )

    if "access_token" not in result:
        print(f"ERROR: {result.get('error_description', result)}")
        return False

    token_file.write_text(json.dumps(json.loads(token_cache.serialize()), indent=2))

    import requests
    resp = requests.get(
        "https://graph.microsoft.com/v1.0/me",
        headers={"Authorization": f"Bearer {result['access_token']}"},
    )
    resp.raise_for_status()
    me = resp.json()
    print(f"✓ Microsoft Graph authenticated as: {me.get('mail') or me.get('userPrincipalName')}")
    print(f"  Token saved to: {token_file}")
    return True


def main():
    print("Daily Brief — Authentication Setup")
    print("=" * 50)

    nd_ok = False
    jhu_ok = False

    if ND_EMAIL:
        nd_ok = setup_gmail()
    else:
        print("\nSkipping Gmail setup — ND_EMAIL_ADDRESS not set in .env")

    if JHU_EMAIL and JHU_CLIENT_ID:
        jhu_ok = setup_jhu()
    else:
        print("\nSkipping JHU setup — JHU_EMAIL_ADDRESS or JHU_AZURE_CLIENT_ID not set in .env")

    print("\n── Summary ───────────────────────────────────────────────────")
    print(f"  Gmail (ND):         {'✓ Ready' if nd_ok else '✗ Not configured'}")
    print(f"  Graph API (JHU):    {'✓ Ready' if jhu_ok else '✗ Not configured'}")

    if nd_ok or jhu_ok:
        print("\nSetup complete. You can now run: python3 daily_brief.py")
    else:
        print("\nSetup incomplete. Review errors above and consult README.md.")


if __name__ == "__main__":
    main()
