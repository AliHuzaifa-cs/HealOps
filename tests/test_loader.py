from pathlib import Path

import pytest

from healops.exceptions import ConfigError, HealOpsError
from healops.loader import load_config, parse_config
from healops.types import ActionType, PolicyVerdict

EXAMPLE = Path(__file__).parent.parent / "examples" / "selfhealing.example.yaml"


def write(tmp_path, text):
    path = tmp_path / "selfhealing.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_example_config_is_valid():
    config = load_config(EXAMPLE)
    assert config.dry_run is True
    assert config.thresholds.latency_ms == 500
    reload_policy = config.policy_for(ActionType.RELOAD_MODEL)
    assert reload_policy.verdict is PolicyVerdict.ALLOW
    redeploy = config.policy_for(ActionType.REQUEST_REDEPLOYMENT)
    assert redeploy.verdict is PolicyVerdict.DENY


def test_empty_file_gives_safe_defaults(tmp_path):
    config = load_config(write(tmp_path, ""))
    assert config.dry_run is True


def test_comment_only_file_gives_safe_defaults(tmp_path):
    config = load_config(write(tmp_path, "# nothing set yet\n"))
    assert config.dry_run is True


def test_file_saved_with_a_bom_is_read(tmp_path):
    path = tmp_path / "selfhealing.yaml"
    path.write_bytes(b"\xef\xbb\xbfdry_run: false\n")
    assert load_config(path).dry_run is False


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope.yaml")


def test_broken_yaml_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(write(tmp_path, "dry_run: [unclosed\n"))


def test_top_level_list_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="top level"):
        load_config(write(tmp_path, "- a\n- b\n"))


def test_typo_error_names_the_exact_key(tmp_path):
    text = "thresholds:\n  latency_msec: 500\n"
    with pytest.raises(ConfigError, match=r"thresholds\.latency_msec"):
        load_config(write(tmp_path, text))


def test_all_problems_are_listed_together(tmp_path):
    text = "thresholds:\n  latency_ms: -5\n  error_rate: 9\n"
    with pytest.raises(ConfigError) as info:
        load_config(write(tmp_path, text))
    message = str(info.value)
    assert "thresholds.latency_ms" in message
    assert "thresholds.error_rate" in message


def test_yaml_cannot_run_code(tmp_path):
    text = "dry_run: !!python/object/apply:os.system ['echo hacked']\n"
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_parse_config_accepts_a_plain_dict():
    config = parse_config({"dry_run": False})
    assert config.dry_run is False


def test_parse_config_reports_errors_as_config_error():
    with pytest.raises(ConfigError):
        parse_config({"check_interval_seconds": 0})


def test_config_error_is_a_healops_error():
    assert issubclass(ConfigError, HealOpsError)