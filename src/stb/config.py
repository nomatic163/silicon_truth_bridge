from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_verdi_home() -> Path | None:
    value = os.environ.get("VERDI_HOME")
    return Path(value).expanduser() if value else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STB_", extra="ignore")

    dev_tools: bool = False
    backend: str = "fake"
    launcher: str = "local"
    max_active_contexts: int = Field(default=4, ge=1)
    max_object_handles: int = Field(default=100_000, ge=1)
    default_timeout_sec: float = Field(default=120.0, gt=0)
    hard_timeout_sec: float = Field(default=300.0, gt=0)
    normal_response_bytes: int = Field(default=4 << 20, ge=1024)
    hard_response_bytes: int = Field(default=16 << 20, ge=1024)
    allowed_roots: str = str(Path.cwd())
    artifact_root: Path = Path(".stb/artifacts")
    max_artifact_bytes: int = Field(default=1 << 30, ge=1)
    max_artifact_total_bytes: int = Field(default=20 << 30, ge=1)
    artifact_shutdown_grace_sec: float = Field(default=5.0, ge=0)
    verdi_home: Path | None = Field(default_factory=_default_verdi_home)
    verdi_release: str | None = None
    worker_python: Path = Field(default_factory=lambda: Path(sys.executable))
    allow_unverified_verdi: bool = False

    @model_validator(mode="after")
    def validate_response_limits(self) -> "Settings":
        if self.normal_response_bytes > self.hard_response_bytes:
            raise ValueError(
                "normal_response_bytes must not exceed hard_response_bytes"
            )
        return self

    @property
    def allowed_root_paths(self) -> list[Path]:
        return [Path(item).expanduser().resolve() for item in self.allowed_roots.split(":") if item]
