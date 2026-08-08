"""OpenRouter LLM client.

All model calls go through OpenRouter using its **free-tier** models, tried in
order until one answers (free models are rate limited, so a fallback chain is
essential). The client is deliberately small: chat completion, JSON-mode
completion, and helpers for the three inference tasks the voice pipeline needs
— language detection, intent classification and sentiment.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Retryable upstream conditions — rate limit, bad gateway, timeouts.
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


@dataclass(slots=True)
class LLMMessage:
    role: str  # system | user | assistant
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class LLMResponse:
    content: str
    model: str
    latency_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class OpenRouterClient:
    """Thin async client over the OpenRouter chat-completions API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        models: list[str] | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.openrouter_api_key
        self.base_url = (base_url or settings.openrouter_base_url).rstrip("/")
        self.models = list(models or settings.openrouter_models)
        self.timeout = timeout or settings.openrouter_timeout_seconds
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # OpenRouter attributes usage to the calling app via these headers.
            "HTTP-Referer": settings.openrouter_app_url,
            "X-Title": settings.openrouter_app_name,
        }

    async def chat(
        self,
        messages: list[LLMMessage] | list[dict[str, str]],
        *,
        temperature: float = 0.4,
        max_tokens: int = 400,
        models: list[str] | None = None,
        response_format: dict | None = None,
    ) -> LLMResponse:
        """Complete a chat, walking the free-model fallback chain on failure."""
        if not self.api_key:
            raise ExternalServiceError(
                "OPENROUTER_API_KEY is not configured; cannot reach the LLM."
            )

        payload_messages = [m.to_dict() if isinstance(m, LLMMessage) else m for m in messages]
        chain = models or self.models
        if not chain:
            raise ExternalServiceError("No OpenRouter models are configured.")

        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        owns_client = self._client is None
        last_error: str = "no models attempted"

        try:
            for model in chain:
                body: dict[str, Any] = {
                    "model": model,
                    "messages": payload_messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if response_format is not None:
                    body["response_format"] = response_format

                started = time.perf_counter()
                try:
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        json=body,
                        headers=self._headers(),
                    )
                except httpx.HTTPError as exc:
                    last_error = f"{model}: transport error ({exc})"
                    logger.warning("OpenRouter transport error on %s: %s", model, exc)
                    continue

                latency_ms = int((time.perf_counter() - started) * 1000)

                if response.status_code in RETRYABLE_STATUS:
                    last_error = f"{model}: HTTP {response.status_code}"
                    logger.warning(
                        "OpenRouter model %s unavailable (HTTP %s), trying next",
                        model,
                        response.status_code,
                    )
                    continue
                if response.status_code >= 400:
                    last_error = f"{model}: HTTP {response.status_code} {response.text[:200]}"
                    logger.warning("OpenRouter rejected request on %s: %s", model, last_error)
                    continue

                parsed = self._parse(response.json(), model, latency_ms)
                if parsed is None:
                    last_error = f"{model}: empty completion"
                    continue
                return parsed

            raise ExternalServiceError(
                "Every configured OpenRouter model failed.",
                details={"last_error": last_error, "models": chain},
            )
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    def _parse(body: dict, model: str, latency_ms: int) -> LLMResponse | None:
        choices = body.get("choices") or []
        if not choices:
            return None
        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            return None
        usage = body.get("usage") or {}
        return LLMResponse(
            content=content,
            model=body.get("model", model),
            latency_ms=latency_ms,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            finish_reason=choices[0].get("finish_reason"),
            raw=body,
        )

    async def chat_json(
        self,
        messages: list[LLMMessage] | list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 300,
    ) -> tuple[dict, LLMResponse]:
        """Complete and parse a JSON object response.

        Free models often ignore ``response_format`` and wrap JSON in prose or
        fences, so the payload is extracted defensively.
        """
        response = await self.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        return extract_json(response.content), response


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model response. ``{}`` if there is none."""
    if not text:
        return {}
    candidate = text.strip()

    fenced = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass

    start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(candidate[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass
    return {}


_client: OpenRouterClient | None = None


def get_llm_client() -> OpenRouterClient:
    global _client
    if _client is None:
        _client = OpenRouterClient()
    return _client


def set_llm_client(client: OpenRouterClient | None) -> None:
    """Inject a client (tests use this to supply a stub)."""
    global _client
    _client = client
