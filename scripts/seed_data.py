#!/usr/bin/env python3
"""
Seed initial contacts and deals into Supabase.
Edit the CONTACTS and DEALS lists below before running.

Usage:
    python scripts/seed_data.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from app.database.client import get_supabase

# ─── Edit these ─────────────────────────────────────────────────────────────

CONTACTS = [
    {
        "name": "Doug Smith",
        "email": "doug@example.com",
        "importance": 5,
        "company": "Acme Capital",
        "role": "Partner",
        "notes": "Lead on Grafton deal. Prefers short emails. Responds fast.",
    },
    {
        "name": "Jack Chen",
        "email": "jack@example.com",
        "importance": 4,
        "company": "Grafton Holdings",
        "role": "CEO",
        "notes": "Main counterparty on Grafton LOI. Detail-oriented.",
    },
    # Add more contacts...
]

DEALS = [
    {
        "name": "Grafton",
        "stage": "LOI",
        "key_parties": ["Jack Chen", "Grafton Holdings"],
        "notes": "LOI signed Jan 15. Exclusivity 60 days. Close target March.",
    },
    {
        "name": "Rojo",
        "stage": "Prospecting",
        "key_parties": ["Maria Lopez"],
        "notes": "Intro call went well. Following up on deck.",
    },
    # Add more deals...
]

TONE_SAMPLES = [
    {
        "category": "formal_external",
        "subject": "Re: Grafton LOI",
        "body": "Jack — thanks for the updated terms. The shortened exclusivity works for us. Let's plan to connect Thursday to walk through the DD checklist.\n\nGarret",
        "to_name": "Jack Chen",
    },
    {
        "category": "quick_internal",
        "subject": "Re: Q3 numbers",
        "body": "Got it. Can you resend the model with the revised assumptions? Want to review before the call.\n\nG",
        "to_name": "Team",
    },
    {
        "category": "relationship",
        "subject": "Re: Catch up",
        "body": "Good to hear from you. Been heads down on a couple of deals but coming up for air soon. Let's grab coffee next week — Tuesday or Wednesday work?\n\nG",
        "to_name": "Friend",
    },
    # Add more samples from your real sent mail...
]

# ─────────────────────────────────────────────────────────────────────────────

def main():
    client = get_supabase()
    print("Seeding Supabase...")

    if CONTACTS:
        result = client.table("contacts").upsert(CONTACTS, on_conflict="email").execute()
        print(f"  ✓ {len(CONTACTS)} contacts seeded")

    if DEALS:
        result = client.table("deals").upsert(DEALS).execute()
        print(f"  ✓ {len(DEALS)} deals seeded")

    if TONE_SAMPLES:
        result = client.table("tone_samples").upsert(TONE_SAMPLES).execute()
        print(f"  ✓ {len(TONE_SAMPLES)} tone samples seeded")

    print("\nDone. Run setup_gmail.py next to authorise Gmail access.")


if __name__ == "__main__":
    main()
