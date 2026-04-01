import structlog
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.telegram.bot import setup_telegram_bot, shutdown_telegram_bot, handle_telegram_update
from app.scheduler.jobs import start_scheduler, stop_scheduler
from app.database.client import get_supabase

logger = structlog.get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown."""
    logger.info("Starting AI Chief of Staff...")

    # Register Telegram webhook
    await setup_telegram_bot()
    logger.info("Telegram bot online", webhook=settings.webhook_url)

    # Start scheduler (daily briefs)
    start_scheduler()
    logger.info("Scheduler started", morning=settings.brief_time_morning, afternoon=settings.brief_time_afternoon)

    yield

    # Graceful shutdown
    logger.info("Shutting down...")
    stop_scheduler()
    await shutdown_telegram_bot()


app = FastAPI(
    title="AI Chief of Staff",
    description="Personal email management system via Telegram",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url=None,
)


# ─── Health Check ────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """
    Checks connectivity to all critical services.
    Hetzner polls this for uptime monitoring.
    """
    checks: dict[str, str | bool] = {}

    # Supabase
    try:
        client = get_supabase()
        client.table("contacts").select("id").limit(1).execute()
        checks["supabase"] = True
    except Exception as e:
        checks["supabase"] = f"ERROR: {e}"

    # Anthropic (just check key is set)
    checks["anthropic_key_set"] = bool(settings.anthropic_api_key)

    # Telegram (just check token is set)
    checks["telegram_configured"] = bool(settings.telegram_bot_token and settings.telegram_chat_id)

    healthy = all(v is True or v is not False for v in checks.values() if isinstance(v, bool))

    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks": checks,
        },
    )


# ─── Telegram Webhook ─────────────────────────────────────────────────────────

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Receive updates from Telegram."""
    # Verify secret token
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    body = await request.json()
    await handle_telegram_update(body)
    return {"ok": True}


# ─── Gmail OAuth Callback ─────────────────────────────────────────────────────

@app.get("/gmail/oauth/callback")
async def gmail_oauth_callback(code: str, state: str | None = None):
    """Handle Gmail OAuth redirect after user authorises."""
    from app.gmail.auth import exchange_code_for_tokens
    try:
        await exchange_code_for_tokens(code)
        return JSONResponse({"status": "Gmail authorised. You can close this window."})
    except Exception as e:
        logger.error("Gmail OAuth callback failed", error=str(e))
        raise HTTPException(status_code=400, detail=str(e))


# ─── Debug Routes (only in debug mode) ───────────────────────────────────────

if settings.debug:
    @app.get("/debug/trigger-brief")
    async def trigger_brief():
        """Manually trigger a daily brief for testing."""
        from app.workflows.brief import generate_and_send_brief
        await generate_and_send_brief()
        return {"status": "brief triggered"}

    @app.get("/debug/gmail-auth-url")
    async def gmail_auth_url():
        """Get the Gmail OAuth URL for initial setup."""
        from app.gmail.auth import get_auth_url
        url = get_auth_url()
        return {"auth_url": url}
