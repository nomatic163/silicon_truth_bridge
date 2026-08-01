from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from pathlib import Path


class WorkerLauncher(ABC):
    name = "base"

    @abstractmethod
    def launch(
        self,
        command: list[str],
        log_path: Path,
    ) -> subprocess.Popen[str]:
        raise NotImplementedError


class LocalWorkerLauncher(WorkerLauncher):
    name = "local"

    def launch(
        self,
        command: list[str],
        log_path: Path,
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )


def build_launcher(name: str) -> WorkerLauncher:
    if name == "local":
        return LocalWorkerLauncher()
    raise ValueError(f"unknown worker launcher: {name}")
