import os
from pathlib import Path

import pytest

from stb.config import Settings
from stb.supervisor import Supervisor
from tests.real_env import require_verdi_home


pytestmark = pytest.mark.skipif(
    os.environ.get("STB_RUN_REAL_NPI") != "1",
    reason="set STB_RUN_REAL_NPI=1 to run licensed Verdi integration tests",
)


def test_real_svh_macro_evidence(tmp_path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "preproc"
    verdi_home = require_verdi_home()
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
            "preproc",
            backend="verdi",
            design_spec={
                "argv": [
                    "-sv",
                    f"+incdir+{fixture}",
                    str(fixture / "top.sv"),
                    "-npi_nl_rtl_opt",
                    "DetailRTL+DetailMux+GenBlock",
                ]
            },
        )
        trace = supervisor.call(
            "preproc",
            "trace",
            {
                "kind": "driver",
                "roots": ["TOP.dout"],
                "max_depth": 2,
                "max_nodes": 20,
            },
        )
        assert any(
            node["origin"] == "npi_language"
            and node["ref"]["npi_type"] == "npiAssignment"
            for node in trace["data"]["nodes"]
        )
        module = supervisor.call(
            "preproc",
            "object_resolve",
            {"model": "netlist", "name": "TOP", "npi_type": "INST"},
        )
        source = supervisor.call(
            "preproc",
            "source_context",
            {
                "reference": module["data"]["ref"],
                "before_lines": 0,
                "after_lines": 0,
                "max_lines": 20,
                "include_preprocessor": True,
            },
        )
        macros = source["data"]["preprocessor_evidence"]["macros"]
        assert macros
        assert macros[0]["definition"]["file"].endswith("stb_defs.svh")
        assert source["data"]["expansion_context_id"].startswith("exp-")
        expansion = source["data"]["expansion_context"]
        assert expansion["physical_file"].endswith("top.sv")
        assert expansion["macro_environment"]["fingerprint"]
        assert expansion["macro_environment"]["status"] == "bounded_relevant"
        replay = supervisor.call(
            "preproc",
            "source_context",
            {
                "reference": module["data"]["ref"],
                "before_lines": 0,
                "after_lines": 0,
                "max_lines": 20,
                "include_preprocessor": True,
                "expansion_context_id": source["data"]["expansion_context_id"],
            },
        )
        assert replay["data"]["expansion_context_id"] == source["data"]["expansion_context_id"]
    finally:
        supervisor.close_all()
