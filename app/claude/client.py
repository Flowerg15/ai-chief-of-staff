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
        _client = AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            timeout=60.0,  # 60s timeout for Claude API calls
        )
    return _client


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def ask_claude(
    user_message: str,
    context: str = "",
    system_override: str | None = None,
    model: str = SONNET,
    max_tokens: int = 1024,
    conversation_history: list[dict] | None = None,
) -> str:
    """
    Send a message to Claude and return the text response.

    Args:
        user_message: The user's request or question.
        context: Retrieved memory/context to prepend to the message.
        system_override: Use a different system prompt (e.g., for brief generation).
        model: Which Claude model to use.
        max_tokens: Maximum tokens in the response.
        conversation_history: Optional list of {"role": "user"|"assistant", "content": "..."} dicts
                              for multi-turn conversations. If provided, used as the messages array.
    """
    client = get_client()
    system = system_override or SYSTEM_PROMPT

    full_message = f"{context}\n\n{user_message}" if context else user_message

    logger.info("Calling Claude", model=model, message_preview=user_message[:100])

    # Build messages array
    if conversation_history:
        # Use the full conversation history, append current message at the end
        messages = list(conversation_history)
        messages.append({"role": "user", "content": full_message})
    else:
        messages = [{"role": "user", "content": full_message}]

    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
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
    Uses Anthropic's Voyage embedding model via their API.
    Returns a 1024-dim vector compatible with pgvector.

    Falls back to a simple hash-based embedding if the API call fails,
    so the app never crashes on embedding generation.
    """
    try:
        import httpx

        async with httpx.AsyncClient(timeout=15.0) as http:
            response = await http.post(
                "https://api.anthropic.com/v1/embeddings",
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2024-10-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "voyage-3-lite",
                    "input": [text[:8000]],  # Cap input length
                },
            )

            if response.status_code == 200:
                data = response.json()
                return data["data"][0]["embedding"]
            else:
                logger.warning("Embedding API error, using fallback", status=response.status_code)

    except Exception as e:
        logger.warning("Embedding generation failed, using fallback", error=str(e))

    # Fallback: deterministic hash-based pseudo-embedding (1536 dims to match schema)
    # Not semantically meaningful, but prevents crashes
    import hashlib
    h = hashlib.sha512(text.encode()).hexdigest()
    # Expand hash to fill 1536 dimensions with values between -1 and 1
    values = []
    for i in range(0, len(h), 2):
        byte_val = int(h[i:i+2], 16)
        values.append((byte_val - 128) / 128.0)
    # Repeat to fill 1536 dims
    while len(values) < 1536:
        values.extend(values[:1536 - len(values)])
    return values[:1536]
