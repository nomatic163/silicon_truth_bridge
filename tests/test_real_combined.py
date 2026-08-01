import os
import subprocess
from pathlib import Path

import pytest

from stb.config import Settings
from stb.supervisor import Supervisor
from tests.real_env import require_vcs, require_verdi_home


pytestmark = pytest.mark.skipif(
    os.environ.get("STB_RUN_REAL_NPI") != "1",
    reason="set STB_RUN_REAL_NPI=1 to run licensed Verdi integration tests",
)


def test_real_combined_active_driver_and_origin(tmp_path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "combined"
    verdi_home = require_verdi_home()
    vcs = require_vcs()
    pli = verdi_home / "share/PLI/VCS/linux64"
    subprocess.run(
        [
            str(vcs),
            "-full64",
            "-sverilog",
            str(fixture / "top.sv"),
            str(fixture / "tb.sv"),
            "-P",
            str(pli / "novas.tab"),
            str(pli / "pli.a"),
            "-o",
            str(tmp_path / "simv"),
        ],
        cwd=tmp_path,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [str(tmp_path / "simv")],
        cwd=tmp_path,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    fsdb = tmp_path / "combined.fsdb"
    assert fsdb.is_file()

    supervisor = Supervisor(
        Settings(
            backend="verdi",
            verdi_home=verdi_home,
            allowed_roots=f"{fixture}:{tmp_path}",
            artifact_root=tmp_path / "artifacts",
        )
    )
    try:
        supervisor.open_context(
            "combined",
            backend="verdi",
            design_spec={
                "argv": [
                    "-sv",
                    str(fixture / "top.sv"),
                    str(fixture / "tb.sv"),
                    "-npi_nl_rtl_opt",
                    "DetailRTL+DetailMux+GenBlock",
                ]
            },
            wave_specs=[{"wave_id": "run", "path": str(fsdb)}],
        )
        active = supervisor.call(
            "combined",
            "trace_active_driver",
            {
                "signals": ["tb.dut.dout"],
                "wave_id": "run",
                "time": "25ns",
                "max_nodes": 50,
            },
        )
        assert active["status"] == "complete"
        assert active["data"]["items"][0]["branches"]
        selections = {
            branch["record"]["statement"]["source"]["begin_line"]: branch["selection"]
            for branch in active["data"]["items"][0]["branches"]
        }
        assert selections[12] == "inactive"
        assert selections[14] == "active"
        assert active["data"]["temporal_resolution"] in {
            "exact_sequence",
            "time_bucket",
        }

        origin = supervisor.call(
            "combined",
            "trace_value_origin",
            {
                "signals": ["tb.dut.dout"],
                "wave_id": "run",
                "time": "25ns",
                "max_nodes": 50,
            },
        )
        assert origin["status"] == "complete"
        assert origin["data"]["items"][0]["value"]["ok"]
        assert origin["data"]["items"][0]["hops"]
        hop = origin["data"]["items"][0]["hops"][0]
        assert hop["sampling_event"]["edge"] == "posedge"
        assert hop["sampling_event"]["signal"] == "tb.dut.clk"
        assert hop["sampled_time"]["ticks"] == "25000"
        assert hop["rhs_sampled_values"][0]["value"]["value"] == "1"
    finally:
        supervisor.close_all()
