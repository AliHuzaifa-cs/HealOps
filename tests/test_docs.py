"""The documentation must stay true: every option documented, every example working."""

import ast
import re
import tomllib
from pathlib import Path

import pytest

import healops
from healops import config as config_module
from healops.guard import ALL_METHODS

ROOT = Path(__file__).parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
CONFIG_DOC = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
ADAPTER_DOC = (ROOT / "docs" / "adapter-guide.md").read_text(encoding="utf-8")
DOCS = {
    "README.md": README,
    "docs/configuration.md": CONFIG_DOC,
    "docs/adapter-guide.md": ADAPTER_DOC,
    "docs/safety-and-limits.md": (ROOT / "docs" / "safety-and-limits.md").read_text(
        encoding="utf-8"
    ),
}


def code_blocks(text, language="python"):
    return re.findall(rf"```{language}\n(.*?)```", text, flags=re.DOTALL)


@pytest.mark.parametrize(
    "model",
    [
        config_module.HealOpsConfig,
        config_module.ThresholdsConfig,
        config_module.MetricBaseline,
        config_module.VerificationConfig,
        config_module.ActionPolicy,
        config_module.AdapterTimeoutsConfig,
        config_module.LLMConfig,
        config_module.EmailConfig,
    ],
)
def test_every_config_option_is_in_the_reference(model):
    missing = [name for name in model.model_fields if f"`{name}`" not in CONFIG_DOC]
    assert missing == [], f"{model.__name__} options missing from docs/configuration.md"


def test_the_reference_does_not_document_options_that_do_not_exist():
    known = set()
    for name in dir(config_module):
        obj = getattr(config_module, name)
        if isinstance(obj, type) and hasattr(obj, "model_fields"):
            known |= set(obj.model_fields)
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", CONFIG_DOC, flags=re.MULTILINE))
    assert documented - known == set()


def test_documented_defaults_match_the_code():
    defaults = {
        "dry_run": "true",
        "check_interval_seconds": "30",
        "min_confidence_for_auto": "0.6",
        "approval_timeout_seconds": "3600",
        "max_recovery_attempts": "3",
        "max_calls_per_hour": "20",
        "max_emails_per_hour": "6",
        "cooldown_minutes": "15",
        "read_seconds": "10",
        "action_seconds": "120",
        "max_stuck_calls": "3",
    }
    for option, shown in defaults.items():
        row = re.search(rf"^\| `{option}` \| `?{re.escape(shown)}`? \|", CONFIG_DOC, re.MULTILINE)
        assert row, f"{option} should be documented with default {shown}"
    config = config_module.HealOpsConfig()
    assert config.dry_run is True
    assert config.check_interval_seconds == 30
    assert config.min_confidence_for_auto == 0.6
    assert config.approval_timeout_seconds == 3600
    assert config.max_recovery_attempts == 3
    assert config.adapter_timeouts.read_seconds == 10
    assert config.adapter_timeouts.action_seconds == 120
    assert config.adapter_timeouts.max_stuck_calls == 3
    assert config_module.LLMConfig(provider="ollama", model="m").max_calls_per_hour == 20


def test_every_adapter_method_is_in_the_guide():
    for method in ALL_METHODS:
        assert f"`{method}(" in ADAPTER_DOC or f"`{method}()`" in ADAPTER_DOC, method


@pytest.mark.parametrize("name", sorted(DOCS))
def test_python_examples_are_valid_python(name):
    for block in code_blocks(DOCS[name]):
        ast.parse(block)


@pytest.mark.parametrize("name", sorted(DOCS))
def test_yaml_examples_are_accepted_by_the_config(name):
    import yaml

    for block in code_blocks(DOCS[name], "yaml"):
        data = yaml.safe_load(block)
        if not isinstance(data, dict):
            continue
        # A snippet may show one section whose own required keys are part of the example.
        known = {k: v for k, v in data.items() if k in config_module.HealOpsConfig.model_fields}
        config_module.HealOpsConfig.model_validate(known)


def test_the_readme_quickstart_is_the_example_file():
    example = (ROOT / "examples" / "quickstart.py").read_text(encoding="utf-8")
    body = example.split('"""', 2)[2].strip()
    [quickstart] = [b for b in code_blocks(README) if "ToyApp" in b]
    assert quickstart.strip() == body


def test_the_marked_examples_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ran = 0
    for block in code_blocks(ADAPTER_DOC):
        if block.startswith("# run-in-tests"):
            exec(compile(block, "adapter-guide.md", "exec"), {})  # noqa: S102 - our own doc
            ran += 1
    assert ran == 1


def test_the_quickstart_really_heals(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    example = (ROOT / "examples" / "quickstart.py").read_text(encoding="utf-8")
    exec(compile(example, "quickstart.py", "exec"), {})  # noqa: S102 - our own example
    out = capsys.readouterr().out
    assert "healthy before: False" in out
    assert "healthy after:  True" in out
    assert "actions taken:  ['restart_component:model-server']" in out


def test_links_between_documents_point_at_files_that_exist():
    for name, text in DOCS.items():
        base = (ROOT / name).parent
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            assert (base / target).resolve().exists(), f"{name} links to missing {target}"


def test_package_metadata_is_ready_for_publishing():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = meta["project"]
    assert project["name"] == "healops"
    assert project["version"] == healops.__version__
    assert project["license"] == "MIT"
    assert project["readme"] == "README.md"
    assert project["requires-python"] == ">=3.11"
    assert project["authors"][0]["name"]
    assert project["classifiers"] and project["keywords"]
    assert "healops" in project["scripts"]
    assert (ROOT / "src" / "healops" / "py.typed").exists()
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")


def test_the_readme_does_not_advertise_the_dashboard_before_it_exists():
    assert "healops[dashboard]" not in README