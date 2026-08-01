from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import time
from pathlib import Path
from typing import Any

import psutil

from stb import __version__
from stb.config import Settings
from stb.supervisor import Supervisor


def run_case(
    supervisor: Supervisor,
    context_id: str,
    method: str,
    args: dict[str, Any],
    iterations: int,
) -> dict[str, Any]:
    durations = []
    response_bytes = []
    input_bytes = []
    npi_calls = []
    npi_ms = []
    python_ms = []
    serialization_ms = []
    transport_ms = []
    scanned = []
    returned = []
    statuses: dict[str, int] = {}
    for _ in range(iterations):
        started = time.perf_counter()
        result = supervisor.call(context_id, method, args)
        durations.append((time.perf_counter() - started) * 1000)
        statuses[result.get("status", "complete")] = (
            statuses.get(result.get("status", "complete"), 0) + 1
        )
        receipt = result.get("receipt") or {}
        metrics = receipt.get("metrics") or {}
        limits = receipt.get("limits") or {}
        for source, sink, key in (
            (metrics, input_bytes, "input_bytes"),
            (metrics, response_bytes, "response_bytes"),
            (metrics, npi_calls, "npi_calls"),
            (metrics, npi_ms, "npi_ms"),
            (metrics, python_ms, "python_ms"),
            (metrics, serialization_ms, "serialization_ms"),
            (metrics, transport_ms, "transport_ms"),
            (limits, scanned, "scanned"),
            (limits, returned, "returned"),
        ):
            if source.get(key) is not None:
                sink.append(float(source[key]))
    durations.sort()
    p95 = durations[min(len(durations) - 1, math.ceil(0.95 * len(durations)) - 1)]
    return {
        "method": method,
        "iterations": iterations,
        "statuses": statuses,
        "min_ms": durations[0],
        "median_ms": statistics.median(durations),
        "p95_ms": p95,
        "mean_ms": statistics.fmean(durations),
        "max_ms": durations[-1],
        "metrics": {
            "input_bytes": int(max(input_bytes)) if input_bytes else 0,
            "response_bytes": int(max(response_bytes)) if response_bytes else 0,
            "npi_calls": int(sum(npi_calls)),
            "npi_ms": sum(npi_ms),
            "python_ms": sum(python_ms),
            "serialization_ms": sum(serialization_ms),
            "transport_ms": sum(transport_ms),
            "scanned": int(sum(scanned)),
            "returned": int(sum(returned)),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument(
        "--output", type=Path, default=Path("benchmarks/fake-baseline.json")
    )
    args = parser.parse_args()
    settings = Settings(
        backend="fake",
        allowed_roots=str(Path.cwd()),
        artifact_root=Path(".stb/artifacts"),
    )
    supervisor = Supervisor(settings)
    process = psutil.Process()
    try:
        supervisor.open_context("benchmark", backend="fake")
        cases = [
            run_case(
                supervisor,
                "benchmark",
                "object_resolve",
                {"name": "top.u_core.req"},
                args.iterations,
            ),
            run_case(
                supervisor,
                "benchmark",
                "object_query",
                {"scope": "top", "limit": 100},
                args.iterations,
            ),
        ]
    finally:
        supervisor.close_all()
    output = {
        "benchmark_version": "stb.bench.v1",
        "timestamp": time.time(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "stb_version": __version__,
            "backend": "fake",
            "launcher": settings.launcher,
            "rss_bytes": process.memory_info().rss,
            "cpu_times": process.cpu_times()._asdict(),
        },
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
