"""
OpenRouter LLM client with retries, token tracking, and structured responses.
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

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY environment variable is required")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = os.getenv("LLM_MODEL", "openai/gpt-4o-mini")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1500"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.7"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "25.0"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))


class Mode(str, Enum):
    DEBATE = "debate"           # Оспорить
    CHECK = "check"             # Проверить сообщение
    CHOOSE = "choose"           # Помочь выбрать


@dataclass
class LLMResponse:
    content: str
    tokens_used: int
    model: str
    finish_reason: str


@dataclass
class Message:
    role: str
    content: str


class OpenRouterError(Exception):
    """Base exception for OpenRouter errors."""
    pass


class RateLimitError(OpenRouterError):
    """Rate limit exceeded."""
    pass


class ModelError(OpenRouterError):
    """Model returned an error."""
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
        system_prompt: str,
        user_message: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> List[Dict[str, str]]:
        """Build messages array for chat completion."""
        messages = [{"role": "system", "content": system_prompt}]
        
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
        system_prompt: str,
        user_message: str,
        history: Optional[List[Dict[str, str]]] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        """Send chat completion request with retries."""
        messages = self._build_messages(system_prompt, user_message, history)
        
        payload = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature or self.temperature,
            "stream": False,
        }
        
        logger.debug(f"Requesting completion: model={payload['model']}, messages={len(messages)}")
        
        response = await self.client.post("/chat/completions", json=payload)
        
        if response.status_code == 429:
            raise RateLimitError("Rate limit exceeded")
        
        if response.status_code >= 400:
            error_data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            error_msg = error_data.get("error", {}).get("message", response.text)
            raise ModelError(f"API error {response.status_code}: {error_msg}")
        
        data = response.json()
        
        choice = data["choices"][0]
        usage = data.get("usage", {})
        
        return LLMResponse(
            content=choice["message"]["content"],
            tokens_used=usage.get("total_tokens", 0),
            model=data.get("model", self.model),
            finish_reason=choice.get("finish_reason", "stop"),
        )
    
    async def complete_simple(
        self,
        system_prompt: str,
        user_message: str,
    ) -> LLMResponse:
        """Simple completion without history."""
        return await self.complete(system_prompt, user_message, history=None)


async def get_llm_client() -> LLMClient:
    """Factory function for dependency injection."""
    return LLMClient()