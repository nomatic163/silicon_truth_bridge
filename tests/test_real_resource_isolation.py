import os
import shutil

import pytest

from stb.config import Settings
from stb.supervisor import Supervisor
from tests.real_env import require_verdi_home

pytestmark = pytest.mark.skipif(
    os.environ.get("STB_RUN_REAL_NPI") != "1",
    reason="set STB_RUN_REAL_NPI=1 to run licensed Verdi integration tests",
)


def test_design_wave_and_source_changes_are_isolated(tmp_path) -> None:
    verdi_home = require_verdi_home()
    source_rtl = verdi_home / (
        "share/NPI/example/via_examples/NPI_Libraries/Connection/"
        "npi_nl_sig_hdl_2_reg_conn/example.v"
    )
    source_fsdb = verdi_home / (
        "share/NPI/example/via_examples/NPI_Libraries/FSDB_Library/"
        "npi_fsdb_sig_value_at/demoL1.fsdb"
    )
    rtl = tmp_path / "example.v"
    fsdb = tmp_path / "demo.fsdb"
    shutil.copy2(source_rtl, rtl)
    shutil.copy2(source_fsdb, fsdb)
    supervisor = Supervisor(
        Settings(
            backend="verdi",
            verdi_home=verdi_home,
            allowed_roots=f"{verdi_home}:{tmp_path}",
            artifact_root=tmp_path / "artifacts",
        )
    )
    try:
        supervisor.open_context(
            "isolated",
            backend="verdi",
            design_spec={"argv": ["-sv", str(rtl)]},
            wave_specs=[{"wave_id": "run", "path": str(fsdb)}],
        )
        top = supervisor.call(
            "isolated",
            "object_resolve",
            {"model": "netlist", "name": "TOP", "npi_type": "INST"},
        )
        first_source = supervisor.call(
            "isolated",
            "source_context",
            {"reference": top["data"]["ref"], "max_lines": 5},
        )
        rtl.write_text(rtl.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        design_after_source_change = supervisor.call(
            "isolated",
            "object_resolve",
            {"model": "netlist", "name": "TOP", "npi_type": "INST"},
        )
        assert design_after_source_change["status"] == "complete"
        retained = supervisor.call(
            "isolated",
            "source_context",
            {"reference": top["data"]["ref"], "max_lines": 5},
        )
        assert retained["data"]["source_alignment"] == "stale"
        assert retained["data"]["source_variant"] == "retained_snapshot"
        assert retained["data"]["text"] == first_source["data"]["text"]

        os.utime(fsdb, None)
        design_after_wave_change = supervisor.call(
            "isolated",
            "object_resolve",
            {"model": "netlist", "name": "TOP", "npi_type": "INST"},
        )
        assert design_after_wave_change["status"] == "complete"
        wave_after_change = supervisor.call(
            "isolated",
            "wave_value",
            {
                "wave_id": "run",
                "signals": ["tb_CPUsystem.CLOCK1"],
                "times": ["100ns"],
            },
        )
        assert wave_after_change["status"] == "failed"
        assert wave_after_change["error"]["code"] == "resource_changed"
        reloaded = supervisor.call(
            "isolated", "wave_manage", {"action": "reload", "wave_id": "run"}
        )
        assert reloaded["status"] == "complete"
    finally:
        supervisor.close_all()
