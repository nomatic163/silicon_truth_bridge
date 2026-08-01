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


def test_real_assertion_discovery_and_structure(tmp_path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "assertions"
    verdi_home = require_verdi_home()
    supervisor = Supervisor(
        Settings(
            backend="verdi",
            verdi_home=verdi_home,
            allowed_roots=f"{fixture.resolve()}:{tmp_path}",
            artifact_root=tmp_path / "artifacts",
        )
    )
    try:
        supervisor.open_context(
            "assertions",
            backend="verdi",
            design_spec={
                "argv": [
                    "-sv",
                    str(fixture / "top.sv"),
                    "-npi_nl_rtl_opt",
                    "DetailRTL+DetailMux+GenBlock",
                ]
            },
        )
        capabilities = supervisor.call(
            "assertions",
            "catalog",
            {"kind": "backend_capabilities", "filters": {}},
        )
        assert (
            capabilities["data"]["assertion_structure"]["status"]
            == "available"
        )

        queried = supervisor.call(
            "assertions",
            "object_query",
            {
                "model": "language",
                "scope": "top",
                "npi_types": ["npiAssert"],
                "limit": 10,
                "max_scan": 100,
            },
        )
        assertions = queried["data"]["objects"]
        assert [item["name"] for item in assertions] == [
            "a_fixed_delay",
            "a_range_delay",
        ]

        named = supervisor.call(
            "assertions",
            "assertion_structure",
            {"reference": assertions[0]["ref"]},
        )
        assert named["status"] == "complete"
        named_data = named["data"]
        assert named_data["npi_cross_reference"]["property_type"] == (
            "npiPropertyInst"
        )
        assert named_data["property_anchor"]["ref"]["npi_type"] == (
            "npiPropertyDecl"
        )
        assert named_data["structure"]["fidelity"] == {
            "syntax": "exact",
            "temporal": "exact",
            "dependencies": "exact",
        }
        assert named_data["structure"]["consequent"]["steps"][0][
            "relative_window"
        ] == {"min_cycles": 2, "max_cycles": 2}

        inline = supervisor.call(
            "assertions",
            "assertion_structure",
            {"reference": assertions[1]["ref"]},
        )
        assert inline["status"] == "complete"
        inline_data = inline["data"]
        assert inline_data["npi_cross_reference"]["property_type"] == (
            "npiPropertySpec"
        )
        assert inline_data["property_anchor"] is None
        assert inline_data["structure"]["implication"]["operator"] == "|=>"
        assert inline_data["structure"]["consequent"]["steps"][0][
            "relative_window"
        ] == {"min_cycles": 1, "max_cycles": 3}
        sampled = inline_data["structure"]["antecedent"]["steps"][0][
            "expression"
        ]["sampled_functions"]
        assert sampled[0]["name"] == "$rose"
    finally:
        supervisor.close_all()
