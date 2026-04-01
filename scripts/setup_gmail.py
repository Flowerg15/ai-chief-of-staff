#!/usr/bin/env python3
"""
Gmail OAuth Setup Script.

Run this once from your local machine to authorise Gmail access.
It opens a browser, you grant access, and the tokens are stored in Supabase.

Usage:
    python scripts/setup_gmail.py

Prerequisites:
    - .env file configured with GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, etc.
    - Your Supabase project is running
    - The PUBLIC_URL server is reachable (or use --local for a local callback)
"""
import sys
import os
import webbrowser

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from app.gmail.auth import get_auth_url


def main():
    try:
        url = get_auth_url()
        print("\n" + "="*60)
        print("GMAIL OAUTH SETUP")
        print("="*60)
        print("\nOpening your browser to authorise Gmail access...")
        print("\nIf the browser doesn't open, visit this URL manually:\n")
        print(url)
        print("\nAfter authorising, Google will redirect to your server's")
        print("/gmail/oauth/callback endpoint, which will store your tokens.")
        print("\nNote: Make sure your server is running and PUBLIC_URL is set correctly.")
        print("="*60 + "\n")
        webbrowser.open(url)
    except Exception as e:
        print(f"\nError: {e}")
        print("\nMake sure your .env is configured correctly.")
        sys.exit(1)


if __name__ == "__main__":
    main()
