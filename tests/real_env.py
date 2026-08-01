from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest


def require_verdi_home() -> Path:
    value = os.environ.get("STB_VERDI_HOME") or os.environ.get("VERDI_HOME")
    if not value:
        pytest.fail(
            "STB_VERDI_HOME or VERDI_HOME is required for real NPI tests",
            pytrace=False,
        )
    path = Path(value).expanduser()
    if not path.is_dir():
        pytest.fail(f"configured Verdi home is not a directory: {path}", pytrace=False)
    return path


def require_vcs() -> Path:
    configured = os.environ.get("STB_VCS")
    resolved = configured or shutil.which("vcs")
    if not resolved:
        pytest.fail(
            "STB_VCS or a vcs executable on PATH is required for combined tests",
            pytrace=False,
        )
    path = Path(resolved).expanduser()
    if not path.is_file():
        pytest.fail(f"configured VCS executable is not a file: {path}", pytrace=False)
    return path
