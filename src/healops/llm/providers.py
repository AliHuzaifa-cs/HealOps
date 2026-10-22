"""Providers for Anthropic, OpenAI-compatible servers (OpenAI, Ollama, ...) and any callable.

All of them speak plain JSON over HTTP through a Transport, send no tools and ask
for deterministic output. API keys are read from an environment variable at call
time; they are never stored in configuration or shown in errors.
"""

import json
import os
from collections.abc import Callable, Mapping

from healops.exceptions import LLMError
from healops.llm.http import (
    MAX_RESPONSE_BYTES,
    HttpRequest,
    HttpResponse,
    Transport,
    check_base_url,
    urllib_transport,
)

ANTHROPIC_URL = "https://api.anthropic.com"
OPENAI_URL = "https://api.openai.com"
OLLAMA_URL = "http://localhost:11434"
ANTHROPIC_VERSION = "2023-06-01"
MAX_PROMPT_CHARS = 100_000


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _check_prompt(system: str, user: str, max_tokens: int) -> None:
    if not isinstance(system, str) or not isinstance(user, str) or not user.strip():
        raise LLMError("the prompt must be non-empty text")
    if len(system) + len(user) > MAX_PROMPT_CHARS:
        raise LLMError("the prompt is too long")
    if not (1 <= max_tokens <= 8192):
        raise LLMError("max_tokens must be between 1 and 8192")


def _send(
    transport: Transport, request: HttpRequest, provider: str
) -> object:
    response: HttpResponse = transport(request)
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise LLMError(f"{provider} reply was too large")
    if response.status != 200:
        raise LLMError(f"{provider} answered with HTTP status {response.status}")
    try:
        return json.loads(response.body)
    except (ValueError, UnicodeDecodeError):
        raise LLMError(f"{provider} reply was not valid JSON") from None


class _KeyedProvider:
    def __init__(
        self,
        model: str,
        base_url: str,
        api_key_env: str | None,
        timeout_seconds: float,
        transport: Transport | None,
        environ: Mapping[str, str] | None,
    ) -> None:
        _require_text(model, "model")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.model = model
        self._api_key_env = api_key_env
        self._timeout = timeout_seconds
        self._transport = transport if transport is not None else urllib_transport
        self._environ = environ if environ is not None else os.environ
        self._base_url = check_base_url(base_url, has_key=api_key_env is not None)

    def _key(self) -> str | None:
        if self._api_key_env is None:
            return None
        key = self._environ.get(self._api_key_env, "").strip()
        if not key:
            raise LLMError(f"environment variable {self._api_key_env} is not set")
        return key

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self.model!r}, base_url={self._base_url!r})"


class OpenAICompatibleProvider(_KeyedProvider):
    """OpenAI, Ollama and every server that offers /v1/chat/completions."""

    def __init__(
        self,
        model: str,
        base_url: str = OPENAI_URL,
        api_key_env: str | None = "OPENAI_API_KEY",
        timeout_seconds: float = 30.0,
        json_mode: bool = True,
        transport: Transport | None = None,
        environ: Mapping[str, str] | None = None,
        name: str = "openai-compatible",
    ) -> None:
        super().__init__(model, base_url, api_key_env, timeout_seconds, transport, environ)
        self.name = name
        self._json_mode = json_mode

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        _check_prompt(system, user, max_tokens)
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        key = self._key()
        if key is not None:
            headers["Authorization"] = f"Bearer {key}"
        request = HttpRequest(
            f"{self._base_url}/v1/chat/completions",
            json.dumps(payload).encode(),
            headers,
            self._timeout,
        )
        data = _send(self._transport, request, self.name)
        try:
            text = data["choices"][0]["message"]["content"]  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"{self.name} reply had an unexpected shape") from None
        if not isinstance(text, str) or not text.strip():
            raise LLMError(f"{self.name} returned no text")
        return text


class AnthropicProvider(_KeyedProvider):
    """Claude, through the Anthropic Messages API."""

    name = "anthropic"

    def __init__(
        self,
        model: str,
        base_url: str = ANTHROPIC_URL,
        api_key_env: str = "ANTHROPIC_API_KEY",
        timeout_seconds: float = 30.0,
        transport: Transport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        _require_text(api_key_env, "api_key_env")
        super().__init__(model, base_url, api_key_env, timeout_seconds, transport, environ)

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        _check_prompt(system, user, max_tokens)
        payload = {
            "model": self.model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._key() or "",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        request = HttpRequest(
            f"{self._base_url}/v1/messages",
            json.dumps(payload).encode(),
            headers,
            self._timeout,
        )
        data = _send(self._transport, request, self.name)
        try:
            blocks = data["content"]  # type: ignore[index]
            text = "".join(b["text"] for b in blocks if b.get("type") == "text")
        except (KeyError, TypeError, AttributeError):
            raise LLMError("anthropic reply had an unexpected shape") from None
        if not text.strip():
            raise LLMError("anthropic returned no text")
        return text


def ollama_provider(
    model: str,
    base_url: str = OLLAMA_URL,
    timeout_seconds: float = 120.0,
    transport: Transport | None = None,
) -> OpenAICompatibleProvider:
    """A local Ollama model. No API key; local models can be slow, hence the long timeout."""
    return OpenAICompatibleProvider(
        model=model,
        base_url=base_url,
        api_key_env=None,
        timeout_seconds=timeout_seconds,
        transport=transport,
        name="ollama",
    )


class CallableProvider:
    """Wraps any function `(system, user, max_tokens) -> str`, for models HealOps has no provider for."""

    def __init__(self, function: Callable[[str, str, int], str], name: str = "custom") -> None:
        if not callable(function):
            raise TypeError("function must be callable")
        _require_text(name, "name")
        self.name = name
        self._function = function

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        _check_prompt(system, user, max_tokens)
        try:
            text = self._function(system, user, max_tokens)
        except LLMError:
            raise
        except Exception as error:  # noqa: BLE001 - a custom model may fail in any way
            raise LLMError(f"{self.name} failed ({type(error).__name__})") from None
        if not isinstance(text, str) or not text.strip():
            raise LLMError(f"{self.name} returned no text")
        return text