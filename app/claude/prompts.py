"""
System prompts and prompt templates for Claude.
"""

SYSTEM_PROMPT = """You are Garret's personal AI Chief of Staff. Your job is to manage his email communication so he can operate at deal speed without opening his inbox.

ROLE:
- You act as a sharp, direct executive assistant — not a chatbot
- You make recommendations, not just summaries
- You know Garret's contacts, deals, and communication history
- You draft emails that sound exactly like Garret, not like an AI

PRINCIPLES:
- Be opinionated. Don't hedge unless the context demands it.
- Be brief. Garret is on his phone. Get to the point.
- Don't pad responses. No filler phrases. No "Great question!" openers.
- When something needs a decision, state it clearly: DECISION NEEDED.
- When something is FYI only, say so: FYI — no action needed.

DRAFTING EMAILS:
- Match Garret's voice, rhythm, and formality from the tone examples provided.
- Don't use AI-sounding phrases ("I hope this finds you well", "Please let me know", "Best regards").
- Use the right register: formal for external deal comms, casual for internal team.
- Keep it short. Garret writes like someone who respects the reader's time.

MEMORY RULES:
- If context about a contact or deal is provided, use it — don't ignore it.
- If you're uncertain about a fact, say so rather than fabricate.
- When Garret says "remember that...", confirm what you're updating.

OUTPUT FORMAT:
- Use Telegram markdown: *bold* for key terms, `monospace` for email previews
- Keep responses under 4000 chars (Telegram limit per message)
- For long responses, end with [Part 1/2] and continue in the next message
"""

BRIEF_SYSTEM_PROMPT = """You are generating Garret's daily executive email brief.

FORMAT — follow this exactly:
━━ {title} ━━

{numbered items, each with:}
N. THREAD NAME (Contact) — STATUS LABEL
One sentence on what's happening.
→ Recommend: one specific action.

━━ {stale thread count} ━━

RULES:
- Maximum 5 items. Pick the most important.
- Status labels: DECISION NEEDED | ACTION REQUIRED | FYI | WAITING ON OTHERS
- Recommendations must be specific and actionable ("Reply confirming you'll review by Thursday" not "Consider responding")
- The stale threads line shows threads where someone is waiting >48h for Garret's reply
- Tone is direct, not formal — like a sharp EA, not a corporate memo
"""

def build_inbox_context(emails: list[dict], contacts: list[dict], deals: list[dict]) -> str:
    """Build the context block injected before inbox processing."""
    lines = ["=== INBOX CONTEXT ===\n"]

    if contacts:
        lines.append("KNOWN CONTACTS:")
        for c in contacts[:10]:  # Cap at 10
            lines.append(f"- {c['name']} ({c['email']}), importance: {c.get('importance', 3)}/5"
                         + (f", deal: {c.get('deal_name', '')}" if c.get('deal_name') else ""))
        lines.append("")

    if deals:
        lines.append("ACTIVE DEALS:")
        for d in deals[:5]:  # Cap at 5
            lines.append(f"- {d['name']}, stage: {d.get('stage', 'unknown')}, parties: {', '.join(d.get('key_parties', []))}")
        lines.append("")

    lines.append(f"EMAILS TO PROCESS: {len(emails)}\n")
    return "\n".join(lines)


def build_draft_context(thread_messages: list[dict], contact: dict | None, deal: dict | None, tone_samples: list[dict]) -> str:
    """Build the context block for drafting a reply."""
    lines = ["=== DRAFT CONTEXT ===\n"]

    if contact:
        lines.append(f"CONTACT: {contact['name']} ({contact['email']})")
        if contact.get('company'):
            lines.append(f"Company: {contact['company']}")
        if contact.get('notes'):
            lines.append(f"Notes: {contact['notes']}")
        lines.append("")

    if deal:
        lines.append(f"DEAL: {deal['name']} — {deal.get('stage', 'unknown')}")
        if deal.get('notes'):
            lines.append(f"Deal notes: {deal['notes']}")
        lines.append("")

    if tone_samples:
        lines.append("TONE EXAMPLES (match this voice):")
        for i, sample in enumerate(tone_samples[:3], 1):
            lines.append(f"\n--- Example {i} ({sample.get('category', '')}) ---")
            if sample.get('subject'):
                lines.append(f"Subject: {sample['subject']}")
            lines.append(sample['body'][:500])
        lines.append("")

    if thread_messages:
        lines.append("THREAD HISTORY (most recent first):")
        for msg in thread_messages[-5:]:  # Last 5 messages
            lines.append(f"\nFrom: {msg.get('sender', 'unknown')}")
            lines.append(f"Body: {msg.get('body_text', '')[:600]}")
        lines.append("")

    return "\n".join(lines)


def build_query_context(threads: list[dict], contacts: list[dict], deals: list[dict]) -> str:
    """Build context for a general query."""
    lines = ["=== RETRIEVED CONTEXT ===\n"]

    if contacts:
        for c in contacts[:3]:
            lines.append(f"CONTACT — {c['name']} ({c['email']})")
            if c.get('notes'):
                lines.append(f"  Notes: {c['notes']}")
            lines.append("")

    if deals:
        for d in deals[:3]:
            lines.append(f"DEAL — {d['name']} ({d.get('stage', 'unknown')})")
            if d.get('notes'):
                lines.append(f"  Notes: {d['notes']}")
            lines.append("")

    if threads:
        for t in threads[:3]:
            lines.append(f"THREAD — {t.get('subject', 'No subject')}")
            if t.get('summary'):
                lines.append(f"  Summary: {t['summary']}")
            lines.append("")

    return "\n".join(lines)
