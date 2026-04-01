"""
Anthropic Claude API client.

Model routing:
- claude-sonnet-4-6: Fast queries, inbox triage, simple drafts
- claude-opus-4-6: Complex reasoning, ambiguous queries, tone calibration
"""
import structlog
from anthropic import AsyncAnthropic
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.claude.prompts import SYSTEM_PROMPT

logger = structlog.get_logger(__name__)
settings = get_settings()

# Model constants
SONNET = "claude-sonnet-4-6"
OPUS = "claude-opus-4-6"

_client: AsyncAnthropic | None = None


def get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def ask_claude(
    user_message: str,
    context: str = "",
    system_override: str | None = None,
    model: str = SONNET,
    max_tokens: int = 1024,
) -> str:
    """
    Send a message to Claude and return the text response.

    Args:
        user_message: The user's request or question.
        context: Retrieved memory/context to prepend to the message.
        system_override: Use a different system prompt (e.g., for brief generation).
        model: Which Claude model to use.
        max_tokens: Maximum tokens in the response.
    """
    client = get_client()
    system = system_override or SYSTEM_PROMPT

    full_message = f"{context}\n\n{user_message}" if context else user_message

    logger.info("Calling Claude", model=model, message_preview=user_message[:100])

    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": full_message}],
    )

    text = response.content[0].text
    logger.info("Claude responded", tokens_used=response.usage.output_tokens)
    return text


async def ask_claude_complex(
    user_message: str,
    context: str = "",
    system_override: str | None = None,
    max_tokens: int = 2048,
) -> str:
    """Use Opus for complex reasoning tasks."""
    return await ask_claude(
        user_message=user_message,
        context=context,
        system_override=system_override,
        model=OPUS,
        max_tokens=max_tokens,
    )


async def generate_embedding(text: str) -> list[float]:
    """
    Generate a text embedding for semantic search.
    Uses text-embedding-3-small for cost efficiency.
    """
    import openai
    # Note: embeddings use OpenAI's API via Supabase's built-in support
    # For simplicity in V1, we'll use a hash-based fallback or skip embeddings
    # and add proper embedding generation in Phase 2.
    # For now, return a placeholder.
    raise NotImplementedError(
        "Embedding generation not configured. "
        "Add your embedding provider in Phase 2."
    )
