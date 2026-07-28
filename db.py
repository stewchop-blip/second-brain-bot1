"""
PostgreSQL database layer using asyncpg.
Tables: users, daily_limits, conversations (for history).
"""
import asyncpg
import os
from datetime import date
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required")

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> asyncpg.Pool:
    """Initialize the connection pool."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            command_timeout=30,
        )
    return _pool


async def close_pool() -> None:
    """Close the connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire():
    """Acquire a connection from the pool."""
    pool = await init_pool()
    async with pool.acquire() as conn:
        yield conn


async def init_db() -> None:
    """Create tables if they don't exist."""
    async with acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                first_seen TIMESTAMPTZ DEFAULT NOW(),
                requests_total INT DEFAULT 0,
                username TEXT,
                first_name TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_limits (
                user_id BIGINT,
                date DATE,
                count INT DEFAULT 0,
                PRIMARY KEY (user_id, date)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                mode TEXT NOT NULL,
                user_message TEXT NOT NULL,
                bot_response TEXT NOT NULL,
                tokens_used INT DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_user_id 
            ON conversations(user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_created_at 
            ON conversations(created_at DESC)
        """)


@dataclass
class User:
    user_id: int
    first_seen: str
    requests_total: int
    username: Optional[str]
    first_name: Optional[str]


async def get_or_create_user(user_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> User:
    """Get existing user or create new one."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, first_seen, requests_total, username, first_name FROM users WHERE user_id = $1",
            user_id
        )
        if row:
            # Update username/first_name if changed
            if username != row["username"] or first_name != row["first_name"]:
                await conn.execute(
                    "UPDATE users SET username = $1, first_name = $2 WHERE user_id = $3",
                    username, first_name, user_id
                )
            return User(**row)
        
        await conn.execute(
            "INSERT INTO users (user_id, username, first_name) VALUES ($1, $2, $3)",
            user_id, username, first_name
        )
        return User(
            user_id=user_id,
            first_seen="",
            requests_total=0,
            username=username,
            first_name=first_name
        )


async def check_limit(user_id: int, daily_limit: int) -> bool:
    """Check if user has remaining requests today. Returns True if allowed."""
    today = date.today()
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT count FROM daily_limits WHERE user_id = $1 AND date = $2",
            user_id, today
        )
        count = row["count"] if row else 0
        return count < daily_limit


async def increment_usage(user_id: int) -> int:
    """Increment usage counter for today. Returns remaining count."""
    today = date.today()
    async with acquire() as conn:
        async with conn.transaction():
            # Upsert daily_limits
            await conn.execute("""
                INSERT INTO daily_limits (user_id, date, count)
                VALUES ($1, $2, 1)
                ON CONFLICT (user_id, date) DO UPDATE SET count = daily_limits.count + 1
            """, user_id, today)
            
            # Increment total
            await conn.execute(
                "UPDATE users SET requests_total = requests_total + 1 WHERE user_id = $1",
                user_id
            )
            
            # Get remaining
            row = await conn.fetchrow(
                "SELECT count FROM daily_limits WHERE user_id = $1 AND date = $2",
                user_id, today
            )
            used = row["count"] if row else 0
            return used


async def get_remaining(user_id: int, daily_limit: int) -> int:
    """Get remaining requests for today."""
    today = date.today()
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT count FROM daily_limits WHERE user_id = $1 AND date = $2",
            user_id, today
        )
        used = row["count"] if row else 0
        return max(0, daily_limit - used)


async def save_conversation(
    user_id: int,
    mode: str,
    user_message: str,
    bot_response: str,
    tokens_used: int = 0
) -> None:
    """Save conversation to history."""
    async with acquire() as conn:
        await conn.execute("""
            INSERT INTO conversations (user_id, mode, user_message, bot_response, tokens_used)
            VALUES ($1, $2, $3, $4, $5)
        """, user_id, mode, user_message, bot_response, tokens_used)


async def get_recent_conversations(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """Get recent conversation history for context."""
    async with acquire() as conn:
        rows = await conn.fetch("""
            SELECT mode, user_message, bot_response, created_at
            FROM conversations
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
        """, user_id, limit)
        return [dict(row) for row in reversed(rows)]  # Chronological order


async def get_user_topics(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """Get a simple list of recent conversation first-messages (for history view)."""
    async with acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, user_message, created_at
            FROM conversations
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
        """, user_id, limit * 2)
        # Deduplicate by keeping the first message of each contiguous session
        seen = []
        for r in rows:
            msg = r["user_message"]
            if not seen or seen[-1]["first_message"] != msg:
                seen.append({"id": r["id"], "first_message": msg, "created_at": r["created_at"]})
            if len(seen) >= limit:
                break
        return list(reversed(seen))


async def get_user_stats(user_id: int) -> Dict[str, Any]:
    """Get user statistics."""
    async with acquire() as conn:
        user = await conn.fetchrow(
            "SELECT requests_total, first_seen FROM users WHERE user_id = $1",
            user_id
        )
        today_count = await conn.fetchval(
            "SELECT count FROM daily_limits WHERE user_id = $1 AND date = $2",
            user_id, date.today()
        )
        total_conversations = await conn.fetchval(
            "SELECT COUNT(*) FROM conversations WHERE user_id = $1",
            user_id
        )
        return {
            "requests_total": user["requests_total"] if user else 0,
            "first_seen": user["first_seen"] if user else None,
            "used_today": today_count or 0,
            "total_conversations": total_conversations or 0,
        }