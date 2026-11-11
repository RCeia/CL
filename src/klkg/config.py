import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

CONFIG_ENV_VAR = "KLKG_CONFIG"
DEFAULT_CONFIG = Path("config") / "settings.yaml"


def find_config(start: Path | None = None) -> Path:
    explicit = os.environ.get(CONFIG_ENV_VAR)
    if explicit:
        return Path(explicit).resolve()
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / DEFAULT_CONFIG
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"{DEFAULT_CONFIG} not found from {current}; set {CONFIG_ENV_VAR}")


@dataclass(frozen=True)
class Settings:
    root: Path
    data: dict[str, Any]

    def __getitem__(self, section: str) -> dict[str, Any]:
        return self.data[section]

    def path(self, key: str) -> Path:
        return self.root / self.data["paths"][key]

    def value(self, section: str, key: str, env: str | None = None) -> Any:
        if env and os.environ.get(env):
            return os.environ[env]
        return self.data[section][key]


def load_settings(config_path: str | Path | None = None) -> Settings:
    path = Path(config_path).resolve() if config_path else find_config()
    root = path.parent.parent
    load_dotenv(root / ".env")
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return Settings(root=root, data=data)


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is not set; see .env.example")
    return value


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    tmp.replace(path)


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
