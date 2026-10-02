from pathlib import Path

from keyword_scanner.config import Config, load_config

PROJECT_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"


def project_config() -> Config:
    return load_config(PROJECT_CONFIG)
