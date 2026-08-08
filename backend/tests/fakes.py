"""In-process doubles for every external dependency.

These implement just enough of each interface for the code under test, and
each records its calls so tests can assert on the interaction rather than
only on the result.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from app.services.llm import LLMMessage, LLMResponse
from app.services.storage import StoredObject, decrypt_bytes, encrypt_bytes
from app.services.whatsapp import WhatsAppMessage, WhatsAppProvider, WhatsAppResult


class FakeRedis:
    """Minimal async Redis: enough for the fixed-window rate limiter."""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.expiries: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.expiries[key] = seconds
        return True

    async def get(self, key: str) -> str | None:
        value = self.store.get(key)
        return None if value is None else str(value)

    async def set(self, key: str, value: Any, **_: Any) -> bool:
        self.store[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        return sum(self.store.pop(k, None) is not None for k in keys)

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class FakeStorage:
    """S3 stand-in backed by a dict, preserving the real encryption path."""

    def __init__(self, bucket: str = "test-recordings") -> None:
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.fail_upload = False

    def ensure_bucket(self) -> None:
        return None

    @staticmethod
    def build_key(business_id: Any, call_id: Any, extension: str = "opus") -> str:
        return f"recordings/{business_id}/{call_id}.{extension}.enc"

    def upload(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "audio/ogg",
        encrypt: bool = True,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        if self.fail_upload:
            from app.core.errors import ExternalServiceError

            raise ExternalServiceError("Recording upload failed: injected failure")

        checksum = hashlib.sha256(data).hexdigest()
        body = encrypt_bytes(data) if encrypt else data
        self.objects[key] = body
        self.metadata[key] = {"sha256": checksum, "encrypted": str(encrypt).lower()}
        return StoredObject(
            bucket=self.bucket,
            key=key,
            size_bytes=len(body),
            checksum_sha256=checksum,
            encrypted=encrypt,
        )

    def download(self, key: str, *, decrypt: bool = True) -> bytes:
        if key not in self.objects:
            from app.core.errors import NotFoundError

            raise NotFoundError(f"Object {key} not found.")
        body = self.objects[key]
        return decrypt_bytes(body) if decrypt else body

    def presigned_url(self, key: str, *, expires_seconds: int = 900) -> str:
        return f"https://storage.test/{self.bucket}/{key}?expires={expires_seconds}"

    def delete(self, key: str) -> bool:
        self.metadata.pop(key, None)
        return self.objects.pop(key, None) is not None


class FakeWhatsAppProvider(WhatsAppProvider):
    """Records outbound WhatsApp messages; can be told to fail."""

    name = "fake"

    def __init__(self) -> None:
        self.sent: list[WhatsAppMessage] = []
        self.fail = False
        self.counter = 0

    async def send(self, message: WhatsAppMessage) -> WhatsAppResult:
        self.sent.append(message)
        if self.fail:
            return WhatsAppResult(message_id="", accepted=False, error="injected provider failure")
        self.counter += 1
        return WhatsAppResult(message_id=f"fake-wa-{self.counter}", accepted=True, raw={"ok": True})


class FakeLLMClient:
    """Deterministic OpenRouter stand-in.

    ``replies`` is consumed in order; once exhausted, ``default_reply`` is
    returned. ``json_replies`` does the same for ``chat_json``. Setting
    ``raise_error`` makes every call fail, which is how the fallback and
    degradation paths get exercised.
    """

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.json_replies: list[dict] = []
        self.default_reply = "Theek hai, main aapki madad karta hoon."
        self.default_json: dict = {}
        self.calls: list[list[dict[str, str]]] = []
        self.raise_error = False
        self.model = "fake/test-model:free"

    async def chat(
        self,
        messages: list[LLMMessage] | list[dict[str, str]],
        *,
        temperature: float = 0.4,
        max_tokens: int = 400,
        models: list[str] | None = None,
        response_format: dict | None = None,
    ) -> LLMResponse:
        self.calls.append([m.to_dict() if isinstance(m, LLMMessage) else m for m in messages])
        if self.raise_error:
            from app.core.errors import ExternalServiceError

            raise ExternalServiceError("Injected LLM failure")

        content = self.replies.pop(0) if self.replies else self.default_reply
        return LLMResponse(
            content=content,
            model=(models or [self.model])[0],
            latency_ms=12,
            prompt_tokens=20,
            completion_tokens=10,
            finish_reason="stop",
            raw={"fake": True},
        )

    async def chat_json(
        self,
        messages: list[LLMMessage] | list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 400,
    ) -> tuple[dict, LLMResponse]:
        """Mirrors the real client, which returns ``(parsed, raw_response)``."""
        self.calls.append([m.to_dict() if isinstance(m, LLMMessage) else m for m in messages])
        if self.raise_error:
            from app.core.errors import ExternalServiceError

            raise ExternalServiceError("Injected LLM failure")

        payload = self.json_replies.pop(0) if self.json_replies else dict(self.default_json)
        response = LLMResponse(
            content=json.dumps(payload),
            model=self.model,
            latency_ms=8,
            prompt_tokens=20,
            completion_tokens=10,
            finish_reason="stop",
        )
        return payload, response

    def queue_json(self, *payloads: dict) -> None:
        self.json_replies.extend(payloads)

    def last_prompt(self) -> str:
        """Flattened text of the most recent prompt, for assertions."""
        return json.dumps(self.calls[-1]) if self.calls else ""


class Clock:
    """Monotonic stand-in for latency assertions that must not be flaky."""

    def __init__(self, start: float = 0.0, step: float = 0.05) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now

    @staticmethod
    def real() -> float:
        return time.perf_counter()
