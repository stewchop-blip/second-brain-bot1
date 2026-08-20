"""
OpenRouter LLM client with JSON-mode, retries, token tracking, structured replies.
"""
import os
import json
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from enum import Enum
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from models import AIReply
from prompts import SYSTEM_PROMPT, parse_ai_reply

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY environment variable is required")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = os.getenv("LLM_MODEL", "openai/gpt-4o-mini")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1200"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.6"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "25.0"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))


class Mode(str, Enum):
    DEBATE = "debate"
    CHECK = "check"
    CHOOSE = "choose"


class OpenRouterError(Exception):
    pass


class RateLimitError(OpenRouterError):
    pass


class ModelError(OpenRouterError):
    pass


class LLMClient:
    """Async client for OpenRouter API with retries and token tracking."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = OPENROUTER_BASE_URL,
        model: str = DEFAULT_MODEL,
        max_tokens: int = MAX_TOKENS,
        temperature: float = TEMPERATURE,
        timeout: float = REQUEST_TIMEOUT,
    ):
        self.api_key = api_key or OPENROUTER_API_KEY
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "LLMClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/second-brain-bot",
                "X-Title": "Second Brain Bot",
            },
            timeout=self.timeout,
        )
        return self

    async def __aexit__(self, *args) -> None:
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if not self._client:
            raise RuntimeError("Client not initialized. Use async context manager.")
        return self._client

    def _build_messages(
        self,
        user_message: str,
        history: Optional[List[Dict[str, str]]] = None,
        system_prompt: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        system = system_prompt if system_prompt is not None else SYSTEM_PROMPT
        messages = [{"role": "system", "content": system}]
        if history:
            for msg in history:
                messages.append({"role": "user", "content": msg["user_message"]})
                messages.append({"role": "assistant", "content": msg["bot_response"]})
        messages.append({"role": "user", "content": user_message})
        return messages

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(MAX_RETRIES),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError, RateLimitError)),
        reraise=True,
    )
    async def complete(
        self,
        user_message: str,
        history: Optional[List[Dict[str, str]]] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> AIReply:
        """Send chat completion request in JSON mode; return structured AIReply."""
        messages = self._build_messages(user_message, history)

        payload = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature or self.temperature,
            "stream": False,
            "response_format": {"type": "json_object"},
        }

        response = await self.client.post("/chat/completions", json=payload)

        if response.status_code == 429:
            raise RateLimitError("Rate limit exceeded")
        if response.status_code == 402:
            raise RateLimitError("Payment required (insufficient credits)")
        if response.status_code >= 400:
            error_data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            error_msg = error_data.get("error", {}).get("message", response.text)
            raise ModelError(f"API error {response.status_code}: {error_msg}")

        data = response.json()
        choice = data["choices"][0]
        raw_content = choice["message"]["content"]
        usage = data.get("usage", {})

        # Parse structured JSON with fallback to plain text
        try:
            schema = parse_ai_reply(raw_content)
            answer = schema.answer
            intent = schema.intent
            needs_clarification = schema.needs_clarification
            next_step = schema.next_step
        except Exception as e:
            logger.warning(f"JSON parse failed, falling back to plain text: {e}")
            answer = raw_content
            intent = "general"
            needs_clarification = False
            next_step = None

        return AIReply(
            intent=intent,
            needs_clarification=needs_clarification,
            answer=answer,
            next_step=next_step,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            cost_usd=data.get("cost") if isinstance(data.get("cost"), (int, float)) else None,
            generation_id=data.get("id"),
        )

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(MAX_RETRIES),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError, RateLimitError)),
        reraise=True,
    )
    async def complete_plain(
        self,
        user_message: str,
        system_prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """Send a plain (non-JSON) completion. Used by the Humanizer.

        Returns the model's text directly — no structured parsing.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature if temperature is not None else self.temperature,
            "stream": False,
        }

        response = await self.client.post("/chat/completions", json=payload)

        if response.status_code == 429:
            raise RateLimitError("Rate limit exceeded")
        if response.status_code == 402:
            raise RateLimitError("Payment required (insufficient credits)")
        if response.status_code >= 400:
            error_data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            error_msg = error_data.get("error", {}).get("message", response.text)
            raise ModelError(f"API error {response.status_code}: {error_msg}")

        data = response.json()
        return data["choices"][0]["message"]["content"].strip()


async def get_llm_client() -> LLMClient:
    return LLMClient()
