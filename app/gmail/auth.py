"""
Gmail OAuth 2.0 authentication.

Flow:
1. Run `scripts/setup_gmail.py` to get the auth URL
2. Garret visits the URL in a browser and grants access
3. Google redirects to /gmail/oauth/callback with a code
4. We exchange the code for tokens and store them in Supabase
5. The Gmail client uses the stored refresh token for all subsequent calls
"""
import json
import secrets
import structlog
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow

from app.config import get_settings
from app.database.client import store_system_value, get_system_value

logger = structlog.get_logger(__name__)
settings = get_settings()

# CSRF state token for OAuth flow
_oauth_state: str | None = None

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

SUPABASE_KEY = "gmail_oauth_tokens"


def _build_flow() -> Flow:
    client_config = {
        "web": {
            "client_id": settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.gmail_oauth_redirect_uri],
        }
    }
    return Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=settings.gmail_oauth_redirect_uri,
    )


def get_auth_url() -> str:
    """Generate the Google OAuth URL for initial authorisation."""
    global _oauth_state
    flow = _build_flow()
    _oauth_state = secrets.token_urlsafe(32)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=_oauth_state,
    )
    return auth_url


def verify_oauth_state(state: str | None) -> bool:
    """Verify the CSRF state token matches."""
    global _oauth_state
    if _oauth_state is None:
        # First-time setup or manual URL — allow it
        return True
    valid = state == _oauth_state
    _oauth_state = None  # One-time use
    return valid


async def exchange_code_for_tokens(code: str) -> None:
    """
    Exchange an auth code for access + refresh tokens.
    Stores tokens encrypted in Supabase system_state.
    """
    flow = _build_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials

    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
    }

    await store_system_value(SUPABASE_KEY, token_data)
    logger.info("Gmail tokens stored in Supabase")


async def get_credentials() -> Credentials:
    """
    Load Gmail credentials from Supabase and refresh if expired.
    Raises RuntimeError if not yet authorised.
    """
    token_data = await get_system_value(SUPABASE_KEY)
    if not token_data:
        raise RuntimeError(
            "Gmail not authorised. Run scripts/setup_gmail.py to authorise."
        )

    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=token_data["scopes"],
    )

    if creds.expired and creds.refresh_token:
        logger.info("Refreshing Gmail access token")
        creds.refresh(Request())
        # Store the refreshed token
        await store_system_value(SUPABASE_KEY, {
            **token_data,
            "token": creds.token,
        })

    return creds
