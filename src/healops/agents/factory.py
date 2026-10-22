"""Builds the diagnosis agent that a configuration asks for."""

from collections.abc import Mapping

from healops.agents.base import DiagnosisAgent
from healops.agents.fallback import FallbackAgent
from healops.agents.llm import LLMDiagnosisAgent
from healops.agents.mock import MockDiagnosisAgent
from healops.config import HealOpsConfig, LLMConfig
from healops.llm.base import LLMProvider
from healops.llm.http import Transport
from healops.llm.providers import (
    ANTHROPIC_URL,
    OLLAMA_URL,
    OPENAI_URL,
    AnthropicProvider,
    OpenAICompatibleProvider,
    ollama_provider,
)

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
        timeout_seconds=timeout or 30.0,
        transport=transport,
        environ=environ,
        name=llm.provider.replace("_", "-"),
    )


def build_agent(
    config: HealOpsConfig,
    component: str = "application",
    transport: Transport | None = None,
    environ: Mapping[str, str] | None = None,
) -> DiagnosisAgent:
    """The rule-based agent, or an LLM agent (with a rule-based safety net) if configured."""
    rules = MockDiagnosisAgent(component=component)
    llm = config.llm
    if llm is None:
        return rules
    provider = build_provider(llm, transport=transport, environ=environ)
    agent = LLMDiagnosisAgent(
        provider,
        component=component,
        max_tokens=llm.max_tokens,
        max_confidence=llm.max_confidence,
    )
    if not llm.use_fallback:
        return agent
    return FallbackAgent(agent, rules, confidence_cap=llm.fallback_confidence_cap)