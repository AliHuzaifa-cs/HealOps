import json

import pytest

from healops.agents.factory import build_agent, build_provider
from healops.agents.fallback import FallbackAgent
from healops.agents.llm import LLMDiagnosisAgent
from healops.agents.mock import MockDiagnosisAgent
from healops.config import HealOpsConfig, LLMConfig
from healops.exceptions import ConfigError
from healops.healing.orchestrator import HandleStatus
from healops.llm import AnthropicProvider, OpenAICompatibleProvider
from healops.llm.http import HttpResponse
from healops.loader import parse_config
from healops.testing import COMPONENT, Fault, detect, heal, make_toy_system


def cfg(**llm):
    return HealOpsConfig.model_validate({"llm": llm}) if llm else HealOpsConfig.model_validate({})


def test_no_llm_section_means_rules_only():
    config = HealOpsConfig.model_validate({})
    assert config.llm is None
    assert isinstance(build_agent(config), MockDiagnosisAgent)


def test_ollama_needs_just_a_model_name():
    config = cfg(provider="ollama", model="llama3.2:3b")
    llm = config.llm
    assert llm.api_key_env is None
    provider = build_provider(llm)
    assert provider.name == "ollama"
    assert provider.model == "llama3.2:3b"


def test_the_defaults_for_openai_and_anthropic_use_the_standard_key_names():
    openai = build_provider(LLMConfig(provider="openai", model="m"), environ={})
    anthropic = build_provider(LLMConfig(provider="anthropic", model="m"), environ={})
    assert isinstance(openai, OpenAICompatibleProvider)
    assert isinstance(anthropic, AnthropicProvider)
    assert "api.openai.com" in repr(openai)
    assert "api.anthropic.com" in repr(anthropic)


def test_any_openai_compatible_server_can_be_used_with_a_base_url():
    config = LLMConfig(
        provider="openai_compatible", model="m", base_url="http://localhost:1234", api_key_env=None
    )
    provider = build_provider(config)
    assert provider.name == "openai-compatible"
    assert "localhost:1234" in repr(provider)


def test_openai_compatible_without_a_base_url_is_refused():
    with pytest.raises(ValueError, match="base_url"):
        LLMConfig(provider="openai_compatible", model="m")


@pytest.mark.parametrize(
    "bad_key_env",
    ["sk-abc123", "my key", "key=value", "sk-ant-api03-xxxxxxxx", "", "1ABC", "a.b"],
)
def test_a_real_key_pasted_where_a_variable_name_belongs_is_refused(bad_key_env):
    with pytest.raises(ValueError, match="NAME of an environment variable"):
        LLMConfig(provider="openai", model="m", api_key_env=bad_key_env)


@pytest.mark.parametrize(
    "bad",
    [
        {"provider": "gemini", "model": "m"},
        {"provider": "ollama", "model": ""},
        {"provider": "ollama", "model": "  "},
        {"provider": "ollama", "model": "m", "timeout_seconds": 0},
        {"provider": "ollama", "model": "m", "max_tokens": 0},
        {"provider": "ollama", "model": "m", "max_confidence": 1.5},
        {"provider": "ollama", "model": "m", "typo_field": 1},
        {"model": "m"},
    ],
)
def test_bad_llm_sections_are_refused_with_a_clear_error(bad):
    with pytest.raises(ConfigError):
        parse_config({"llm": bad})


def test_the_llm_section_loads_from_yaml_data():
    config = parse_config(
        {
            "llm": {
                "provider": "ollama",
                "model": "llama3.2:3b",
                "timeout_seconds": 60,
                "max_confidence": 0.8,
                "use_fallback": False,
            }
        }
    )
    assert config.llm.timeout_seconds == 60
    assert config.llm.use_fallback is False


def test_the_agent_is_an_llm_agent_wrapped_in_a_fallback_by_default():
    agent = build_agent(cfg(provider="ollama", model="m"), component=COMPONENT)
    assert isinstance(agent, FallbackAgent)


def test_the_fallback_can_be_switched_off():
    agent = build_agent(cfg(provider="ollama", model="m", use_fallback=False))
    assert isinstance(agent, LLMDiagnosisAgent)


def test_the_configured_fallback_cap_is_used_when_the_model_server_is_down():
    from healops.exceptions import LLMError

    def down(request):
        raise LLMError("could not reach the model (ConnectionRefusedError)")

    config = cfg(provider="ollama", model="m", fallback_confidence_cap=0.1)
    agent = build_agent(config, component=COMPONENT, transport=down)
    system = make_toy_system(agent=agent)
    system.app.inject(Fault.CRASH)
    result = heal(system, detect(system)[0])
    assert result.status is HandleStatus.PARKED
    assert result.diagnosis.confidence == 0.1
    assert "[FALLBACK" in result.diagnosis.summary


def test_a_whole_configured_system_works_with_a_fake_model_server():
    reply = {
        "summary": "the service is not responding",
        "confidence": 0.7,
        "probable_cause": "the model server hung",
        "evidence_used": ["health = False"],
        "alternatives": [],
        "missing_information": ["raw error logs"],
        "action": "restart_component",
        "rationale": "a restart usually clears a hung process",
        "expected_outcome": "health returns to true",
    }
    sent = []

    def transport(request):
        sent.append(request)
        body = {"choices": [{"message": {"content": json.dumps(reply)}}]}
        return HttpResponse(200, json.dumps(body).encode())

    config = cfg(provider="ollama", model="llama3.2:3b")
    agent = build_agent(config, component=COMPONENT, transport=transport)
    system = make_toy_system(agent=agent)
    system.app.inject(Fault.CRASH)
    result = heal(system, detect(system)[0])
    assert result.status is HandleStatus.RESOLVED
    assert result.diagnosis.summary.startswith("[LLM ollama/llama3.2:3b]")
    assert sent[0].url == "http://localhost:11434/v1/chat/completions"


def test_the_commented_llm_block_in_the_example_config_is_valid_when_uncommented():
    from pathlib import Path

    import yaml

    text = (Path(__file__).parent.parent / "examples" / "selfhealing.example.yaml").read_text(
        encoding="utf-8"
    )
    inside = text.split("# --- llm (optional) ---")[1].split("# --- end llm ---")[0]
    lines = [ln[2:] for ln in inside.splitlines() if ln.startswith("# ") and ln[2:].startswith(("llm:", "  "))]
    active = [ln for ln in lines if not ln.lstrip().startswith("#")]
    config = parse_config(yaml.safe_load("\n".join(active)))
    assert config.llm.provider == "ollama"
    assert config.llm.model == "llama3.2:3b"


# ---------- other OpenAI-style services (for example Gemini) ----------

GEMINI = {
    "provider": "openai_compatible",
    "model": "some-model",
    "base_url": "https://generativelanguage.googleapis.com",
    "chat_path": "/v1beta/openai/chat/completions",
    "api_key_env": "GEMINI_API_KEY",
    "json_mode": False,
}


def test_a_service_with_a_different_path_and_no_json_mode_can_be_configured():
    sent = []

    def transport(request):
        sent.append(request)
        body = {"choices": [{"message": {"content": "{}"}}]}
        return HttpResponse(200, json.dumps(body).encode())

    config = parse_config({"llm": GEMINI})
    provider = build_provider(config.llm, transport=transport, environ={"GEMINI_API_KEY": "k"})
    provider.complete("s", "u")
    request = sent[0]
    assert request.url == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    assert request.headers["Authorization"] == "Bearer k"
    assert "response_format" not in json.loads(request.body)


def test_the_default_path_and_json_mode_are_unchanged():
    sent = []

    def transport(request):
        sent.append(request)
        return HttpResponse(200, json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode())

    config = parse_config({"llm": {"provider": "openai", "model": "m"}})
    build_provider(config.llm, transport=transport, environ={"OPENAI_API_KEY": "k"}).complete(
        "s", "u"
    )
    assert sent[0].url == "https://api.openai.com/v1/chat/completions"
    assert json.loads(sent[0].body)["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("path", ["v1/chat", "/v1/chat?x=1", "/v1/chat#top", "/v1/ chat", ""])
def test_a_chat_path_must_be_a_plain_path(path):
    with pytest.raises(ConfigError):
        parse_config({"llm": {**GEMINI, "chat_path": path}})
    with pytest.raises(ValueError):
        OpenAICompatibleProvider("m", api_key_env=None, chat_path=path)