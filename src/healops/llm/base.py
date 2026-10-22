"""The smallest possible contract for a language model: text in, text out."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """Anything that can answer one prompt. Implementations must raise LLMError on failure."""

    name: str

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> str: ...