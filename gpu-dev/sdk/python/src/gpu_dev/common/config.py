"""SDK configuration."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel


_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "gpu-dev" / "config.json"


class GpuDevConfig(BaseModel):
    """Configuration for the GpuDev client.

    Reads from ``~/.config/gpu-dev/config.json`` by default (shared with the CLI).
    All fields can be overridden explicitly.

    Example::

        config = GpuDevConfig(github_user="octocat")
        client = GpuDev(config)
    """
    github_user: str | None = None
    environment: str = "prod"
    region: str | None = None
    default_timeout_minutes: int = 30
    poll_interval_seconds: float = 1.0

    @classmethod
    def from_file(cls, path: Path | None = None) -> GpuDevConfig:
        """Load config from a JSON file, falling back to defaults."""
        p = path or _DEFAULT_CONFIG_PATH
        data: dict = {}
        if p.exists():
            try:
                data = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return cls(
            github_user=data.get("github_user"),
            environment=data.get("environment", "prod"),
            region=data.get("region"),
        )
