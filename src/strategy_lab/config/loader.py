from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


ENV_PATTERN = re.compile(r"^\$\{([^}:]+)(?::([^}]*))?\}$")


class ProjectConfig(BaseModel):
    name: str = "stock_strategy_lab"
    artifacts_dir: Path = Path("artifacts")
    experiments_dir: Path = Path("artifacts/experiments")
    strategy_registry_dir: Path = Path("artifacts/strategy_registry")
    default_timezone: str = "Asia/Shanghai"


class LoggingConfig(BaseModel):
    level: str = "INFO"


class AppConfig(BaseModel):
    root_dir: Path
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    raw: dict[str, Any] = Field(default_factory=dict)

    def resolve_project_path(self, path: Path | str) -> Path:
        value = Path(path)
        if value.is_absolute():
            return value
        return self.root_dir / value


def _resolve_env_value(value: Any) -> Any:
    if isinstance(value, str):
        match = ENV_PATTERN.match(value)
        if not match:
            return value
        name, default = match.groups()
        return os.getenv(name, default or "")
    if isinstance(value, dict):
        return {key: _resolve_env_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_env_value(item) for item in value]
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return _resolve_env_value(data)


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "pyproject.toml").exists() and (candidate / "configs").exists():
            return candidate
    return current


def load_app_config(root_dir: Path | None = None) -> AppConfig:
    root = (root_dir or find_project_root()).resolve()
    load_dotenv(root / ".env")
    configs_dir = root / "configs"
    raw: dict[str, Any] = {}

    app_yaml = _load_yaml(configs_dir / "app.yaml")
    raw.update(app_yaml)

    project = ProjectConfig(**app_yaml.get("project", {}))
    logging = LoggingConfig(**app_yaml.get("logging", {}))
    return AppConfig(root_dir=root, project=project, logging=logging, raw=raw)


def load_config_file(name: str, root_dir: Path | None = None) -> dict[str, Any]:
    root = (root_dir or find_project_root()).resolve()
    load_dotenv(root / ".env")
    filename = name if name.endswith(".yaml") else f"{name}.yaml"
    return _load_yaml(root / "configs" / filename)
