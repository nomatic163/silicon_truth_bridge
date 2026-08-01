import os

import pytest

from stb.config import Settings
from stb.supervisor import Supervisor
from tests.real_env import require_verdi_home


pytestmark = pytest.mark.skipif(
    os.environ.get("STB_RUN_REAL_NPI") != "1",
    reason="set STB_RUN_REAL_NPI=1 to run licensed Verdi integration tests",
)


def test_real_waveform_round_trip(tmp_path) -> None:
    verdi_home = require_verdi_home()
    demo_fsdb = verdi_home / (
        "share/NPI/example/via_examples/NPI_Libraries/FSDB_Library/"
        "npi_fsdb_sig_value_at/demoL1.fsdb"
    )
    supervisor = Supervisor(
        Settings(
            backend="verdi",
            verdi_home=verdi_home,
            allowed_roots=str(verdi_home),
            artifact_root=tmp_path / "artifacts",
        )
    )
    try:
        supervisor.open_context(
            "real",
            backend="verdi",
            wave_specs=[{"wave_id": "demo", "path": str(demo_fsdb)}],
        )
        resolved = supervisor.call(
            "real",
            "object_resolve",
            {
                "model": "waveform",
                "wave_id": "demo",
                "name": "tb_CPUsystem.CLOCK1",
            },
        )
        assert resolved["data"]["ref"]["npi_type"] == "SIGNAL"

        values = supervisor.call(
            "real",
            "wave_value",
            {
                "wave_id": "demo",
                "signals": ["tb_CPUsystem.CLOCK1", "missing"],
                "times": ["200ns"],
            },
        )
        assert values["data"]["values"][0]["value"]["value"] == "0"
        assert values["data"]["values"][0]["time"]["unit"] == "ns"
        assert values["data"]["values"][1]["error_code"] == "signal_not_dumped"

        stats = supervisor.call(
            "real",
            "wave_compute",
            {
                "operation": "statistics",
                "wave_id": "demo",
                "signals": ["tb_CPUsystem.CLOCK1"],
                "start": "0ns",
                "end": "200ns",
            },
        )
        assert stats["data"]["items"][0]["data"]["transition_count"] == 4

        first_page = supervisor.call(
            "real",
            "wave_changes",
            {
                "wave_id": "demo",
                "signals": ["tb_CPUsystem.CLOCK1"],
                "start": "0ns",
                "end": "200ns",
                "direction": "forward",
                "max_changes": 2,
            },
        )
        assert len(first_page["data"]["signals"][0]["changes"]) == 2
        assert first_page["data"]["next_cursor"]
        second_page = supervisor.call(
            "real",
            "wave_changes",
            {
                "wave_id": "demo",
                "signals": ["tb_CPUsystem.CLOCK1"],
                "start": "0ns",
                "end": "200ns",
                "direction": "forward",
                "max_changes": 2,
                "cursor": first_page["data"]["next_cursor"],
            },
        )
        assert second_page["data"]["signals"][0]["changes"]
        replayed_second_page = supervisor.call(
            "real",
            "wave_changes",
            {
                "wave_id": "demo",
                "signals": ["tb_CPUsystem.CLOCK1"],
                "start": "0ns",
                "end": "200ns",
                "direction": "forward",
                "max_changes": 2,
                "cursor": first_page["data"]["next_cursor"],
            },
        )
        assert replayed_second_page["data"] == second_page["data"]
        first_times = {
            row["time"]["raw_ticks"]
            for row in first_page["data"]["signals"][0]["changes"]
        }
        second_times = {
            row["time"]["raw_ticks"]
            for row in second_page["data"]["signals"][0]["changes"]
        }
        assert first_times.isdisjoint(second_times)

        evaluated = supervisor.call(
            "real",
            "wave_compute",
            {
                "operation": "evaluate_window",
                "wave_id": "demo",
                "start": "0ns",
                "end": "200ns",
                "max_points": 20,
                "expression": {
                    "expr_version": "stb.expr.v1",
                    "root": {
                        "op": "logic.eq",
                        "args": [
                            {"signal": "tb_CPUsystem.CLOCK1"},
                            {"literal": "1'b1"},
                        ],
                    },
                },
            },
        )
        assert {row["value"]["value"] for row in evaluated["data"]["rows"]} == {
            "0",
            "1",
        }

        query_args = {
            "model": "waveform",
            "wave_id": "demo",
            "scope": "tb_CPUsystem",
            "limit": 10,
        }
        first_query = supervisor.call("real", "object_query", query_args)
        cached_query = supervisor.call("real", "object_query", query_args)
        assert first_query["receipt"]["metrics"]["cache_misses"] == 1
        assert cached_query["receipt"]["metrics"]["cache_hits"] == 1

        reloaded = supervisor.call(
            "real",
            "wave_manage",
            {"action": "reload", "wave_id": "demo"},
        )
        assert reloaded["data"]["wave_generation"] == 2
        post_reload_query = supervisor.call("real", "object_query", query_args)
        assert post_reload_query["receipt"]["metrics"]["cache_misses"] == 1
        stale_cursor = supervisor.call(
            "real",
            "wave_changes",
            {
                "wave_id": "demo",
                "signals": ["tb_CPUsystem.CLOCK1"],
                "start": "0ns",
                "end": "200ns",
                "direction": "forward",
                "max_changes": 2,
                "cursor": first_page["data"]["next_cursor"],
            },
        )
        assert stale_cursor["status"] == "failed"
        assert stale_cursor["error"]["code"] == "cursor_expired"
    finally:
        supervisor.close_all()
