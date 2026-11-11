"""Builds the diagnosis agent that a configuration asks for."""

from collections.abc import Mapping

from healops.agents.base import ComponentSpec, DiagnosisAgent
from healops.agents.fallback import FallbackAgent
from healops.agents.llm import LLMDiagnosisAgent
from healops.agents.mock import DEFAULT_PLAYBOOK, MockDiagnosisAgent
from healops.agents.playbook import PlaybookGuard, TargetRouter
from healops.agents.ratelimit import RateLimitedAgent
from healops.config import HealOpsConfig, LLMConfig
from healops.llm.base import LLMProvider
from healops.llm.http import Transport
from healops.llm.providers import (
    ANTHROPIC_URL,
    DEFAULT_CHAT_PATH,
    OLLAMA_URL,
    OPENAI_URL,
    AnthropicProvider,
    OpenAICompatibleProvider,
    ollama_provider,
)
from healops.types import ActionType, IncidentType

_DEFAULT_KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def build_provider(
    llm: LLMConfig,
    transport: Transport | None = None,
    environ: Mapping[str, str] | None = None,
) -> LLMProvider:
    """Create the provider named in the configuration."""
    timeout = llm.timeout_seconds
    if llm.provider == "ollama":
        return ollama_provider(
            llm.model,
            base_url=llm.base_url or OLLAMA_URL,
            timeout_seconds=timeout or 120.0,
            transport=transport,
        )
    if llm.provider == "anthropic":
        return AnthropicProvider(
            llm.model,
            base_url=llm.base_url or ANTHROPIC_URL,
            api_key_env=llm.api_key_env or _DEFAULT_KEY_ENV["anthropic"],
            timeout_seconds=timeout or 30.0,
            transport=transport,
            environ=environ,
        )
    return OpenAICompatibleProvider(
        llm.model,
        base_url=llm.base_url or OPENAI_URL,
        api_key_env=llm.api_key_env or _DEFAULT_KEY_ENV.get(llm.provider),
        json_mode=llm.json_mode,
        chat_path=llm.chat_path or DEFAULT_CHAT_PATH,
        timeout_seconds=timeout or 30.0,
        transport=transport,
        environ=environ,
        name=llm.provider.replace("_", "-"),
    )


def build_playbook(config: HealOpsConfig) -> dict[IncidentType, tuple[ActionType, ...]]:
    """The built-in playbook with the incident types the configuration replaces."""
    merged = dict(DEFAULT_PLAYBOOK)
    for incident_type, steps in config.playbook.items():
        merged[incident_type] = tuple(steps)
    return merged


def build_agent(
    config: HealOpsConfig,
    component: ComponentSpec = "application",
    transport: Transport | None = None,
    environ: Mapping[str, str] | None = None,
) -> DiagnosisAgent:
    """The rule-based agent, or an LLM agent (with a rule-based safety net) if configured.

    The playbook and the restart targets of the configuration apply to either.
    """
    playbook = build_playbook(config)
    rules = MockDiagnosisAgent(component=component, playbook=playbook)
    llm = config.llm
    agent: DiagnosisAgent = rules
    if llm is not None:
        provider = build_provider(llm, transport=transport, environ=environ)
        model_agent: DiagnosisAgent = LLMDiagnosisAgent(
            provider,
            component=component,
            max_tokens=llm.max_tokens,
            max_confidence=llm.max_confidence,
        )
        if llm.max_calls_per_hour is not None:
            model_agent = RateLimitedAgent(model_agent, llm.max_calls_per_hour, per_seconds=3600.0)
        if llm.enforce_playbook:
            model_agent = PlaybookGuard(model_agent, playbook)
        agent = (
            FallbackAgent(model_agent, rules, confidence_cap=llm.fallback_confidence_cap)
            if llm.use_fallback
            else model_agent
        )
    if config.restart_targets:
        agent = TargetRouter(agent, config.restart_targets)
    return agent
