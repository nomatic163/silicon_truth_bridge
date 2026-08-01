from __future__ import annotations

from typing import Any

from stb.backends.fake import FakeBackend
from stb.service import StbService


class LargeResponseBackend(FakeBackend):
    def object_resolve(self, args: dict[str, Any]) -> Any:
        return {"payload": "x" * 4096}


def test_worker_response_limit_fails_without_silent_truncation() -> None:
    service = StbService(
        LargeResponseBackend("rtl"),
        "rtl",
        normal_response_bytes=1024,
        hard_response_bytes=2048,
    )
    result = service.object_resolve({"name": "top"})
    assert result["status"] == "failed"
    assert result["error"]["code"] == "limit_exceeded"
    assert result["error"]["details"]["response_bytes"] > 2048
    assert "artifact.export" in result["error"]["details"]["remediation"]
    assert result["receipt"]["limits"]["termination_reason"] == "response_byte_limit"


def test_response_receipt_reports_byte_and_latency_dimensions() -> None:
    service = StbService(FakeBackend("rtl"), "rtl")
    result = service.object_resolve({"name": "top"})
    metrics = result["receipt"]["metrics"]
    assert metrics["input_bytes"] > 0
    assert metrics["response_bytes"] > 0
    assert metrics["python_ms"] >= 0
    assert metrics["serialization_ms"] >= 0
    assert metrics["total_ms"] >= metrics["python_ms"]

    queried = service.object_query({"scope": "top", "limit": 2})
    assert queried["receipt"]["limits"]["returned"] == 2


def test_truncated_results_are_reported_as_partial() -> None:
    service = StbService(
        FakeBackend("rtl", wave_specs=[{"wave_id": "run", "path": "fake.fsdb"}]),
        "rtl",
    )
    args = {
        "wave_id": "run",
        "signals": ["top.clk"],
        "start": "0fs",
        "end": "20ns",
        "max_changes": 2,
    }
    result = service._call(
        "wave_changes",
        lambda: service.backend.wave_changes(args),
        {
            **args,
        },
    )
    assert result["status"] == "partial"
    assert result["data"]["truncated"]
    assert result["receipt"]["limits"]["termination_reason"] == "transition_limit"
    assert result["receipt"]["limits"]["returned"] == 2


def test_multi_signal_wave_change_cursor_does_not_replay_done_signals() -> None:
    service = StbService(
        FakeBackend("rtl", wave_specs=[{"wave_id": "run", "path": "fake.fsdb"}]),
        "rtl",
    )
    args = {
        "wave_id": "run",
        "signals": ["top.req", "top.clk"],
        "start": "0fs",
        "end": "20ns",
        "max_changes": 2,
    }

    first = service._call("wave_changes", lambda: service.backend.wave_changes(args), args)
    second_args = {**args, "cursor": first["data"]["next_cursor"]}
    second = service._call(
        "wave_changes",
        lambda: service.backend.wave_changes(second_args),
        second_args,
    )
    third_args = {**args, "cursor": second["data"]["next_cursor"]}
    third = service._call(
        "wave_changes",
        lambda: service.backend.wave_changes(third_args),
        third_args,
    )

    req_pages = [
        page["data"]["signals"][0]["changes"]
        for page in (first, second, third)
    ]
    req_times = [
        change["time"]["raw_ticks"]
        for page in req_pages
        for change in page
    ]
    assert req_times == ["0", "7000000", "17000000"]
    assert third["data"]["signals"][0]["changes"] == []
    assert third["data"]["signals"][1]["changes"]
