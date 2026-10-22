import json

import pytest

from healops.exceptions import LLMError
from healops.llm import (
    AnthropicProvider,
    CallableProvider,
    LLMProvider,
    OpenAICompatibleProvider,
    ollama_provider,
)
from healops.llm.http import HttpRequest, HttpResponse, check_base_url, urllib_transport


class FakeTransport:
    """Records the request and answers with a prepared response."""

    def __init__(self, status=200, body=None, raw=None, error=None):
        self.requests = []
        self._status = status
        self._raw = raw if raw is not None else json.dumps(body).encode()
        self._error = error

    def __call__(self, request):
        self.requests.append(request)
        if self._error:
            raise self._error
        return HttpResponse(self._status, self._raw)

    @property
    def sent(self):
        return json.loads(self.requests[0].body)


def openai_reply(text="{}"):
    return {"choices": [{"message": {"content": text}}]}


def anthropic_reply(text="{}"):
    return {"content": [{"type": "text", "text": text}]}


# ---------- OpenAI-compatible (OpenAI and Ollama) ----------


def test_openai_provider_sends_the_right_request_and_returns_the_text():
    transport = FakeTransport(body=openai_reply('{"a": 1}'))
    provider = OpenAICompatibleProvider(
        "gpt-x", api_key_env="K", transport=transport, environ={"K": "sk-secret"}
    )
    assert provider.complete("be brief", "hello", 200) == '{"a": 1}'
    request = transport.requests[0]
    assert request.url == "https://api.openai.com/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-secret"
    sent = transport.sent
    assert sent["model"] == "gpt-x"
    assert sent["temperature"] == 0
    assert sent["max_tokens"] == 200
    assert sent["stream"] is False
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
    ]


def test_json_mode_can_be_switched_off():
    transport = FakeTransport(body=openai_reply())
    provider = OpenAICompatibleProvider(
        "m", api_key_env=None, json_mode=False, transport=transport
    )
    provider.complete("s", "u")
    assert "response_format" not in transport.sent


def test_ollama_needs_no_key_and_talks_to_localhost():
    transport = FakeTransport(body=openai_reply("hi"))
    provider = ollama_provider("llama3.2:3b", transport=transport)
    assert provider.name == "ollama"
    assert provider.complete("s", "u") == "hi"
    request = transport.requests[0]
    assert request.url == "http://localhost:11434/v1/chat/completions"
    assert "Authorization" not in request.headers
    assert request.timeout_seconds == 120.0


def test_a_missing_key_is_an_error_that_names_the_variable_only():
    provider = OpenAICompatibleProvider(
        "m", api_key_env="MY_KEY", transport=FakeTransport(body=openai_reply()), environ={}
    )
    with pytest.raises(LLMError, match="MY_KEY is not set"):
        provider.complete("s", "u")


def test_a_blank_key_counts_as_missing():
    provider = OpenAICompatibleProvider(
        "m", api_key_env="K", transport=FakeTransport(body=openai_reply()), environ={"K": "  "}
    )
    with pytest.raises(LLMError):
        provider.complete("s", "u")


@pytest.mark.parametrize(
    "body",
    [{}, {"choices": []}, {"choices": [{}]}, {"choices": [{"message": {"content": None}}]},
     {"choices": [{"message": {"content": "  "}}]}, [], "text"],
)
def test_unexpected_reply_shapes_are_errors_not_crashes(body):
    provider = ollama_provider("m", transport=FakeTransport(body=body))
    with pytest.raises(LLMError):
        provider.complete("s", "u")


# ---------- Anthropic ----------


def test_anthropic_provider_sends_the_right_request_and_joins_text_blocks():
    body = {"content": [{"type": "text", "text": "ab"}, {"type": "other"}, {"type": "text", "text": "cd"}]}
    transport = FakeTransport(body=body)
    provider = AnthropicProvider("claude-x", transport=transport, environ={"ANTHROPIC_API_KEY": "sk-ant"})
    assert provider.complete("sys", "usr", 300) == "abcd"
    request = transport.requests[0]
    assert request.url == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "sk-ant"
    assert request.headers["anthropic-version"] == "2023-06-01"
    sent = transport.sent
    assert sent["system"] == "sys"
    assert sent["messages"] == [{"role": "user", "content": "usr"}]
    assert sent["max_tokens"] == 300
    assert sent["temperature"] == 0


@pytest.mark.parametrize(
    "body",
    [{}, {"content": []}, {"content": [{"type": "other"}]}, {"content": "x"}, {"content": [1]}, []],
)
def test_anthropic_unexpected_shapes_are_errors(body):
    provider = AnthropicProvider("m", transport=FakeTransport(body=body), environ={"ANTHROPIC_API_KEY": "k"})
    with pytest.raises(LLMError):
        provider.complete("s", "u")


def test_anthropic_without_a_key_fails_before_any_request():
    transport = FakeTransport(body=anthropic_reply())
    provider = AnthropicProvider("m", transport=transport, environ={})
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        provider.complete("s", "u")
    assert transport.requests == []


# ---------- shared safety ----------


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_http_errors_give_a_clear_message_without_the_body(status):
    provider = ollama_provider("m", transport=FakeTransport(status=status, raw=b"secret sk-leak"))
    with pytest.raises(LLMError) as caught:
        provider.complete("s", "u")
    assert str(status) in str(caught.value)
    assert "sk-leak" not in str(caught.value)


def test_a_reply_that_is_not_json_is_an_error():
    provider = ollama_provider("m", transport=FakeTransport(raw=b"<html>oops</html>"))
    with pytest.raises(LLMError, match="not valid JSON"):
        provider.complete("s", "u")


def test_an_oversized_reply_is_refused():
    provider = ollama_provider("m", transport=FakeTransport(raw=b"x" * 1_000_002))
    with pytest.raises(LLMError, match="too large"):
        provider.complete("s", "u")


def test_a_network_failure_surfaces_as_an_llm_error():
    provider = ollama_provider("m", transport=FakeTransport(error=LLMError("could not reach the model (URLError)")))
    with pytest.raises(LLMError, match="could not reach"):
        provider.complete("s", "u")


@pytest.mark.parametrize(
    ("system", "user", "tokens"),
    [("s", "", 100), ("s", "   ", 100), ("s", "u", 0), ("s", "u", 9000)],
)
def test_bad_prompts_are_refused_before_sending(system, user, tokens):
    transport = FakeTransport(body=openai_reply())
    provider = ollama_provider("m", transport=transport)
    with pytest.raises(LLMError):
        provider.complete(system, user, tokens)
    assert transport.requests == []


def test_a_too_long_prompt_is_refused_before_sending():
    # Built inside the test: a 100k-character parameter would end up in the test id,
    # and Windows limits environment variables (which pytest uses) to 32,767 characters.
    transport = FakeTransport(body=openai_reply())
    provider = ollama_provider("m", transport=transport)
    with pytest.raises(LLMError, match="too long"):
        provider.complete("s", "x" * 100_001, 100)
    assert transport.requests == []


def test_the_key_never_shows_up_in_repr_or_in_the_request_repr():
    transport = FakeTransport(body=openai_reply())
    provider = OpenAICompatibleProvider(
        "m", api_key_env="K", transport=transport, environ={"K": "sk-topsecret"}
    )
    provider.complete("s", "u")
    assert "sk-topsecret" not in repr(provider)
    assert "sk-topsecret" not in repr(transport.requests[0])


def test_construction_rejects_nonsense():
    with pytest.raises(ValueError):
        OpenAICompatibleProvider("", api_key_env=None)
    with pytest.raises(ValueError):
        OpenAICompatibleProvider("m", api_key_env=None, timeout_seconds=0)
    with pytest.raises(ValueError):
        AnthropicProvider("m", api_key_env=" ")


def test_providers_satisfy_the_protocol():
    for provider in (
        ollama_provider("m"),
        AnthropicProvider("m", environ={}),
        CallableProvider(lambda s, u, t: "x"),
    ):
        assert isinstance(provider, LLMProvider)


# ---------- URL safety ----------


@pytest.mark.parametrize("url", ["", "ftp://x", "file:///etc/passwd", "localhost:11434", "http://"])
def test_only_real_http_urls_are_accepted(url):
    with pytest.raises(LLMError):
        check_base_url(url, has_key=False)


def test_a_key_is_never_sent_over_plain_http_to_a_remote_host():
    with pytest.raises(LLMError, match="https"):
        check_base_url("http://example.com", has_key=True)
    assert check_base_url("http://localhost:8000/", has_key=True) == "http://localhost:8000"
    assert check_base_url("https://example.com/", has_key=True) == "https://example.com"
    assert check_base_url("http://example.com", has_key=False) == "http://example.com"


def test_the_default_transport_refuses_non_http_schemes():
    with pytest.raises(LLMError):
        urllib_transport(HttpRequest("file:///etc/passwd", b""))


# ---------- custom provider ----------


def test_a_custom_function_can_be_a_provider():
    seen = []
    provider = CallableProvider(lambda s, u, t: seen.append((s, u, t)) or "ok", name="mine")
    assert provider.complete("sys", "usr", 50) == "ok"
    assert seen == [("sys", "usr", 50)]
    assert provider.name == "mine"


def test_a_failing_custom_function_gives_an_llm_error_without_its_message():
    def broken(s, u, t):
        raise RuntimeError("password=hunter2")

    provider = CallableProvider(broken, name="mine")
    with pytest.raises(LLMError) as caught:
        provider.complete("s", "u")
    assert "RuntimeError" in str(caught.value)
    assert "hunter2" not in str(caught.value)


def test_a_custom_function_must_return_text():
    provider = CallableProvider(lambda s, u, t: "  ")
    with pytest.raises(LLMError):
        provider.complete("s", "u")
    with pytest.raises(TypeError):
        CallableProvider("not callable")  # type: ignore[arg-type]


# ---------- the real transport, against a tiny local server ----------


def _serve(status, body):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            seen["path"] = self.path
            seen["body"] = json.loads(self.rfile.read(length))
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, seen


def test_the_real_transport_works_end_to_end_against_a_local_server():
    server, seen = _serve(200, openai_reply('{"ok": true}'))
    try:
        provider = ollama_provider("llama3.2:3b", base_url=f"http://127.0.0.1:{server.server_port}")
        assert provider.complete("sys", "usr") == '{"ok": true}'
    finally:
        server.shutdown()
    assert seen["path"] == "/v1/chat/completions"
    assert seen["body"]["model"] == "llama3.2:3b"


def test_the_real_transport_reports_an_http_error_status():
    server, _ = _serve(500, {"error": "boom"})
    try:
        provider = ollama_provider("m", base_url=f"http://127.0.0.1:{server.server_port}")
        with pytest.raises(LLMError, match="500"):
            provider.complete("s", "u")
    finally:
        server.shutdown()


def test_the_real_transport_reports_a_server_that_is_not_running():
    provider = ollama_provider("m", base_url="http://127.0.0.1:1", timeout_seconds=2)
    with pytest.raises(LLMError, match="could not reach"):
        provider.complete("s", "u")