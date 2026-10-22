"""Ask a real model to diagnose each toy fault, and report what happens.

Works with a local Ollama model (the default) or any other provider:
    python examples/live_ollama_check.py
    python examples/live_ollama_check.py --model llama3.2:3b --runs 5
    python examples/live_ollama_check.py --provider anthropic --model <model> --key-env ANTHROPIC_API_KEY
    python examples/live_ollama_check.py --provider openai_compatible --model <model> \
        --base-url https://generativelanguage.googleapis.com \
        --chat-path /v1beta/openai/chat/completions --key-env GEMINI_API_KEY --no-json-mode
The key is read from the environment variable you name, never typed here.

It prints, per fault, whether the model gave a usable answer, which action it
proposed and whether that was the cure that really fixes the fault. Small local
models often answer badly; that is exactly what the fallback is for.
"""

import argparse
import time

from healops.agents.base import DiagnosisContext
from healops.agents.factory import build_agent, build_provider
from healops.agents.llm import LLMDiagnosisAgent
from healops.config import HealOpsConfig, LLMConfig
from healops.exceptions import InvalidLLMOutput, LLMError
from healops.testing import COMPONENT, Fault, detect, heal, make_toy_system
from healops.types import ActionType

# The action that really cures each fault in the toy app (None: nothing automatic).
CURES = {
    Fault.CRASH: ActionType.RESTART_COMPONENT,
    Fault.SLOW: ActionType.RESTART_COMPONENT,
    Fault.MODEL_CORRUPT: ActionType.RELOAD_MODEL,
    Fault.BAD_MODEL: ActionType.ROLLBACK_MODEL,
    Fault.DRIFT: None,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--provider",
        default="ollama",
        choices=["ollama", "openai", "anthropic", "openai_compatible"],
    )
    parser.add_argument("--model", default="llama3.2:3b")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--chat-path", default=None)
    parser.add_argument("--key-env", default=None, help="NAME of the environment variable")
    parser.add_argument("--no-json-mode", action="store_true")
    parser.add_argument("--runs", type=int, default=3, help="answers to ask for per fault")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    llm = LLMConfig(
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        chat_path=args.chat_path,
        api_key_env=args.key_env,
        json_mode=not args.no_json_mode,
        timeout_seconds=args.timeout,
    )
    provider = build_provider(llm)
    agent = LLMDiagnosisAgent(provider, component=COMPONENT)
    usable = right = total = 0

    print(f"Model: {args.provider}/{args.model}, {args.runs} run(s) per fault\n")
    for fault, cure in CURES.items():
        system = make_toy_system()
        system.app.inject(fault)
        incident = detect(system)[0]
        context = DiagnosisContext(
            incident=incident,
            available_actions=system.application.capabilities.supported_actions(),
        )
        print(f"{fault.value}  (incident: {incident.incident_type.value}, "
              f"cure: {cure.value if cure else 'none, a person must decide'})")
        for run in range(1, args.runs + 1):
            total += 1
            started = time.perf_counter()
            try:
                result = agent.diagnose(context)
            except InvalidLLMOutput as problem:
                outcome = f"REJECTED: {problem}"
            except LLMError as problem:
                print(f"  run {run}: UNREACHABLE: {problem}")
                print("\nCheck that the service is reachable, the model name is right and the key variable is set.\nFor Ollama: is it running, and is the model downloaded (ollama list)?")
                return 1
            else:
                usable += 1
                proposed = result.proposal.action_type if result.proposal else None
                is_right = proposed == cure or (
                    cure is None and proposed in (ActionType.ESCALATE, ActionType.RUN_DIAGNOSTIC)
                )
                right += is_right
                outcome = (
                    f"ok, proposed {proposed.value if proposed else 'nothing'}, "
                    f"confidence {result.diagnosis.confidence:.2f}"
                    f"{'  <- matches the cure' if is_right else ''}"
                )
            print(f"  run {run}: {outcome}  [{time.perf_counter() - started:.1f}s]")

    print(f"\nUsable answers: {usable}/{total}   Right action: {right}/{total}")

    print("\nWhole system on a CRASH, with the rule-based safety net:")
    config = HealOpsConfig.model_validate(
        {
            "min_confidence_for_auto": 0.3,
            "thresholds": {"consecutive_breaches": 2},
            "llm": llm.model_dump(exclude_none=True),
            "policies": {"restart_component": {"verdict": "allow", "cooldown_seconds": 0}},
            "verification": {"settle_seconds": 0, "checks": 2, "interval_seconds": 0},
        }
    )
    full = make_toy_system(config, agent=build_agent(config, component=COMPONENT))
    full.app.inject(Fault.CRASH)
    outcome = heal(full, detect(full)[0])
    print(f"  status: {outcome.status.value}")
    print(f"  diagnosis: {outcome.diagnosis.summary if outcome.diagnosis else '-'}")
    print(f"  actions run on the app: {full.app.actions or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())