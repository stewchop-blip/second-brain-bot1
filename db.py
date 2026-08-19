"""
PostgreSQL database layer using asyncpg.
Tables: users, daily_usage, conversations (history), conversation_state,
referrals, analytics_events.
"""
import asyncpg
import json
import os
import secrets
import logging
from datetime import date, datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any

DATABASE_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("DATABASE_PRIVATE_URL")
    or os.getenv("POSTGRES_URL")
    or os.getenv("POSTGRES_PRIVATE_URL")
)
# NOTE: Do NOT raise here. Railway/Render healthcheck needs the process to
# start and serve GET / even if DB is not yet reachable. We connect lazily.
if not DATABASE_URL:
    logger.warning("DATABASE_URL not set at import time; DB connection deferred until first use.")

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is required to connect to the database")
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10, command_timeout=30)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire():
    pool = await init_pool()
    async with pool.acquire() as conn:
        yield conn


async def init_db() -> None:
    async with acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                first_request_at TIMESTAMPTZ,
                last_request_at TIMESTAMPTZ,
                requests_total INT DEFAULT 0,
                username TEXT,
                first_name TEXT,
                source_param TEXT,
                referral_token TEXT UNIQUE,
                referrer_id BIGINT,
                bonus_balance INT DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_usage (
                user_id BIGINT,
                usage_date DATE,
                successful_requests INT DEFAULT 0,
                PRIMARY KEY (user_id, usage_date)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_state (
                user_id BIGINT PRIMARY KEY,
                context JSONB DEFAULT '[]'::jsonb,
                awaiting_clarification BOOLEAN DEFAULT FALSE,
                current_topic_id TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                referrer_id BIGINT,
                referred_id BIGINT UNIQUE,
                qualified_requests INT DEFAULT 0,
                activated_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (referrer_id, referred_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS analytics_events (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT,
                event_name TEXT,
                timestamp TIMESTAMPTZ DEFAULT NOW(),
                properties JSONB DEFAULT '{}'::jsonb
            )
        """)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                mode TEXT NOT NULL,
                user_message TEXT NOT NULL,
                bot_response TEXT NOT NULL,
                tokens_used INT DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )
        # --- Migrations: add columns that may be missing from older schemas ---
        # CREATE TABLE IF NOT EXISTS does NOT alter existing tables, so we
        # explicitly add columns that the current code expects.
        migrations = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS first_request_at TIMESTAMPTZ;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_request_at TIMESTAMPTZ;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS source_param TEXT;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_token TEXT UNIQUE;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer_id BIGINT;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_balance INT DEFAULT 0;",
        ]
        for stmt in migrations:
            try:
                await conn.execute(stmt)
            except Exception as e:
                logger.warning(f"migration skipped: {stmt} -> {e}")


# ---- Users ----
async def get_or_create_user(
    user_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    source_param: Optional[str] = None,
    referrer_token: Optional[str] = None,
) -> Dict[str, Any]:
    async with acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
        if not row:
            token = secrets.token_urlsafe(8)
            referrer_id = None
            if referrer_token:
                ref = await conn.fetchrow("SELECT user_id FROM users WHERE referral_token = $1", referrer_token)
                if ref and ref["user_id"] != user_id:
                    referrer_id = ref["user_id"]
            await conn.execute("""
                INSERT INTO users (user_id, username, first_name, source_param, referral_token, referrer_id)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, user_id, username, first_name, source_param, token, referrer_id)
            return dict(await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id))
        # update mutable fields
        if username != row["username"] or first_name != row["first_name"]:
            await conn.execute("UPDATE users SET username=$1, first_name=$2 WHERE user_id=$3", username, first_name, user_id)
        return dict(row)


async def get_user(user_id: int) -> Dict[str, Any]:
    async with acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
        return dict(row) if row else {}


async def get_referral_token(user_id: int) -> str:
    async with acquire() as conn:
        row = await conn.fetchrow("SELECT referral_token FROM users WHERE user_id = $1", user_id)
        return row["referral_token"] if row else ""


# ---- Limits ----
async def daily_remaining(user_id: int, daily_limit: int) -> int:
    today = date.today()
    async with acquire() as conn:
        row = await conn.fetchrow("SELECT successful_requests FROM daily_usage WHERE user_id=$1 AND usage_date=$2", user_id, today)
        used = row["successful_requests"] if row else 0
        return max(0, daily_limit - used)


async def bonus_balance(user_id: int) -> int:
    async with acquire() as conn:
        row = await conn.fetchrow("SELECT bonus_balance FROM users WHERE user_id=$1", user_id)
        return row["bonus_balance"] if row else 0


async def can_make_request(user_id: int, daily_limit: int) -> bool:
    return (await daily_remaining(user_id, daily_limit)) > 0 or (await bonus_balance(user_id)) > 0


async def increment_daily(user_id: int) -> int:
    today = date.today()
    async with acquire() as conn:
        await conn.execute("""
            INSERT INTO daily_usage (user_id, usage_date, successful_requests)
            VALUES ($1, $2, 1)
            ON CONFLICT (user_id, usage_date) DO UPDATE SET successful_requests = daily_usage.successful_requests + 1
        """, user_id, today)
        row = await conn.fetchrow("SELECT successful_requests FROM daily_usage WHERE user_id=$1 AND usage_date=$2", user_id, today)
        return row["successful_requests"]


async def increment_total(user_id: int) -> None:
    async with acquire() as conn:
        await conn.execute("UPDATE users SET requests_total = requests_total + 1, last_request_at = NOW() WHERE user_id=$1", user_id)


async def consume_bonus(user_id: int) -> None:
    async with acquire() as conn:
        await conn.execute("UPDATE users SET bonus_balance = bonus_balance - 1 WHERE user_id=$1 AND bonus_balance > 0", user_id)


# ---- Conversation state / context ----
def _coerce_context(raw) -> List[Dict[str, str]]:
    """Always return a list of dicts, regardless of how asyncpg returns JSONB."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(raw, list):
        return raw
    return []


async def get_conversation_state(user_id: int) -> Dict[str, Any]:
    async with acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM conversation_state WHERE user_id=$1", user_id)
        if not row:
            await conn.execute("INSERT INTO conversation_state (user_id) VALUES ($1)", user_id)
            return {"user_id": user_id, "context": [], "awaiting_clarification": False, "current_topic_id": None}
        state = dict(row)
        state["context"] = _coerce_context(state.get("context"))
        return state


async def save_context(user_id: int, context: List[Dict[str, str]], awaiting_clarification: bool = False, current_topic_id: Optional[str] = None) -> None:
    # Serialize to JSON string and cast, so asyncpg never needs a custom codec
    ctx_json = json.dumps(context, ensure_ascii=False)
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO conversation_state (user_id, context, awaiting_clarification, current_topic_id)
            VALUES ($1, $2::jsonb, $3, $4)
            ON CONFLICT (user_id) DO UPDATE SET context=$2::jsonb, awaiting_clarification=$3, current_topic_id=$4
            """,
            user_id, ctx_json, awaiting_clarification, current_topic_id,
        )


async def clear_context(user_id: int) -> None:
    async with acquire() as conn:
        await conn.execute("UPDATE conversation_state SET context='[]'::jsonb, awaiting_clarification=FALSE, current_topic_id=NULL WHERE user_id=$1", user_id)


async def set_awaiting_clarification(user_id: int, value: bool) -> None:
    async with acquire() as conn:
        await conn.execute("UPDATE conversation_state SET awaiting_clarification=$2 WHERE user_id=$1", user_id, value)


# ---- Referrals ----
async def record_successful_request_for_referral(user_id: int) -> None:
    """If user was referred, count qualified requests; award bonuses at 3."""
    async with acquire() as conn:
        user = await conn.fetchrow("SELECT referrer_id FROM users WHERE user_id=$1", user_id)
        if not user or not user["referrer_id"]:
            return
        referrer = user["referrer_id"]
        # idempotent: only if not already activated
        existing = await conn.fetchrow("SELECT activated_at FROM referrals WHERE referrer_id=$1 AND referred_id=$2", referrer, user_id)
        if existing and existing["activated_at"]:
            return
        # increment qualified
        await conn.execute("""
            INSERT INTO referrals (referrer_id, referred_id, qualified_requests)
            VALUES ($1, $2, 1)
            ON CONFLICT (referrer_id, referred_id) DO UPDATE SET qualified_requests = referrals.qualified_requests + 1
        """, referrer, user_id)
        row = await conn.fetchrow("SELECT qualified_requests FROM referrals WHERE referrer_id=$1 AND referred_id=$2", referrer, user_id)
        if row and row["qualified_requests"] >= 3:
            # award once
            await conn.execute("UPDATE referrals SET activated_at=NOW() WHERE referrer_id=$1 AND referred_id=$2 AND activated_at IS NULL", referrer, user_id)
            await conn.execute("UPDATE users SET bonus_balance = bonus_balance + 10 WHERE user_id=$1", user_id)
            await conn.execute("UPDATE users SET bonus_balance = bonus_balance + 20 WHERE user_id=$1", referrer)
            return True
    return False


# ---- Analytics ----
async def track_event(user_id: int, event_name: str, properties: Dict[str, Any]) -> None:
    # Serialize to JSON string; asyncpg casts to jsonb without a custom codec
    props_json = json.dumps(properties, ensure_ascii=False, default=str)
    async with acquire() as conn:
        await conn.execute(
            "INSERT INTO analytics_events (user_id, event_name, properties) VALUES ($1, $2, $3::jsonb)",
            user_id, event_name, props_json,
        )


# ---- History (for context window) ----
async def save_conversation(user_id: int, mode: str, user_message: str, bot_response: str, tokens_used: int = 0) -> None:
    async with acquire() as conn:
        await conn.execute(
            "INSERT INTO conversations (user_id, mode, user_message, bot_response, tokens_used) VALUES ($1,$2,$3,$4,$5)",
            user_id, mode, user_message, bot_response, tokens_used,
        )


async def get_recent_conversations(user_id: int, limit: int = 6) -> List[Dict[str, Any]]:
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT mode, user_message, bot_response FROM conversations WHERE user_id=$1 ORDER BY created_at DESC LIMIT $2",
            user_id, limit,
        )
        return [dict(r) for r in reversed(rows)]
