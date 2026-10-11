"""Turn a YAML file or a plain dict into a validated HealOpsConfig."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from healops.config import HealOpsConfig
from healops.exceptions import ConfigError


def parse_config(data: Mapping[str, Any]) -> HealOpsConfig:
    """Validate already-loaded data and return a HealOpsConfig."""
    try:
        return HealOpsConfig.model_validate(dict(data))
    except ValidationError as error:
        problems = [
            f"  - {'.'.join(str(part) for part in item['loc']) or '(top level)'}: "
            f"{item['msg']}"
            for item in error.errors()
        ]
        raise ConfigError("invalid configuration:\n" + "\n".join(problems)) from error


def load_config(path: str | Path) -> HealOpsConfig:
    """Read a YAML file and return a validated HealOpsConfig.

    An empty file is allowed and gives the safe defaults.
    """
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise ConfigError(f"cannot read config file {file_path}: {error}") from error

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigError(f"{file_path} is not valid YAML: {error}") from error

    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ConfigError(
            f"{file_path} must contain key: value settings at the top level"
        )
    return parse_config(data)