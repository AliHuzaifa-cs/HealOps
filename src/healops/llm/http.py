"""A minimal HTTP layer so providers need no extra packages and tests need no network."""

import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

from healops.exceptions import LLMError

MAX_RESPONSE_BYTES = 1_000_000
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True)
class HttpRequest:
    url: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0

    def __repr__(self) -> str:  # never print headers: they may hold an API key
        return f"HttpRequest(url={self.url!r}, body=<{len(self.body)} bytes>)"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


Transport = Callable[[HttpRequest], HttpResponse]


def check_base_url(base_url: str, has_key: bool) -> str:
    """Accept http(s) URLs only, and never send a key over plain http to a remote host."""
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise LLMError("base_url must be an http or https URL")
    if has_key and parsed.scheme != "https" and parsed.hostname not in _LOCAL_HOSTS:
        raise LLMError("an API key may only be sent over https (or to localhost)")
    return base_url.rstrip("/")


def urllib_transport(request: HttpRequest) -> HttpResponse:
    """The default transport, built on the standard library."""
    if urlparse(request.url).scheme not in ("http", "https"):
        raise LLMError("only http and https URLs are allowed")
    prepared = urllib.request.Request(
        request.url, data=request.body, headers=request.headers, method="POST"
    )
    try:
        with urllib.request.urlopen(
            prepared, timeout=request.timeout_seconds
        ) as reply:
            return HttpResponse(reply.status, reply.read(MAX_RESPONSE_BYTES + 1))
    except urllib.error.HTTPError as error:
        return HttpResponse(error.code, b"")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise LLMError(f"could not reach the model ({type(error).__name__})") from None