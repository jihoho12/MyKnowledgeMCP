"""설정 관리"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    db_path: str = "data/knowledge.db"
    categories: list[str] = field(default_factory=lambda: [
        "general", "network", "os", "database", "web",
        "security", "language", "algorithm", "architecture", "devops",
    ])
    summary_consolidation_threshold: int = 500


def load_config() -> Config:
    """config.toml을 읽어서 Config 객체를 반환합니다."""
    config_paths = [
        Path(os.environ.get("KNOWLEDGE_CONFIG_PATH", "")),
        Path.cwd() / "config.toml",
        Path(__file__).parent.parent.parent / "config.toml",
    ]

    for path in config_paths:
        if path.is_file():
            with open(path, "rb") as f:
                raw = tomllib.load(f)
            return Config(
                db_path=raw.get("database", {}).get("path", Config.db_path),
                categories=raw.get("knowledge", {}).get("categories", Config().categories),
                summary_consolidation_threshold=raw.get("knowledge", {}).get(
                    "summary_consolidation_threshold",
                    Config.summary_consolidation_threshold,
                ),
            )

    return Config()
