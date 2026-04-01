from functools import lru_cache
from supabase import create_client, Client
from app.config import get_settings


@lru_cache
def get_supabase() -> Client:
    """Returns a cached Supabase client using the service role key."""
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_service_key)


async def store_system_value(key: str, value: dict) -> None:
    """Upsert a value in the system_state table."""
    client = get_supabase()
    client.table("system_state").upsert({"key": key, "value": value}).execute()


async def get_system_value(key: str) -> dict | None:
    """Retrieve a value from system_state."""
    client = get_supabase()
    result = client.table("system_state").select("value").eq("key", key).maybe_single().execute()
    return result.data["value"] if result.data else None
