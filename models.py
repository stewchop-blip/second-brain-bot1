"""
Data models for Second Brain Bot 1.5.
Single chat core: one AI call returns structured JSON.
"""
from typing import Literal, Optional
from dataclasses import dataclass, field
from pydantic import BaseModel, Field


# Allowed intents (strict set per spec)
Intent = Literal["general", "message_check", "idea_check", "choice"]


class AIReplySchema(BaseModel):
    """Pydantic schema for the structured JSON the model returns."""
    intent: Intent
    needs_clarification: bool = False
    answer: str
    next_step: Optional[str] = None

    model_config = {"extra": "ignore"}


@dataclass
class AIReply:
    """Rich reply object returned by the AI service."""
    intent: str
    needs_clarification: bool
    answer: str
    next_step: Optional[str]
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    generation_id: Optional[str] = None


@dataclass
class ChatMessage:
    """A single message in the conversation context."""
    role: str  # "user" or "assistant"
    content: str
