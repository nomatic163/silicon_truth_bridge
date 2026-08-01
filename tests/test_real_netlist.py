import os

import pytest

from stb.config import Settings
from stb.supervisor import Supervisor
from tests.real_env import require_verdi_home

pytestmark = pytest.mark.skipif(
    os.environ.get("STB_RUN_REAL_NPI") != "1",
    reason="set STB_RUN_REAL_NPI=1 to run licensed Verdi integration tests",
)


def test_real_netlist_query_and_connectivity(tmp_path) -> None:
    verdi_home = require_verdi_home()
    rtl = verdi_home / (
        "share/NPI/example/via_examples/NPI_Libraries/Connection/"
        "npi_nl_sig_hdl_2_reg_conn/example.v"
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
            "design",
            backend="verdi",
            design_spec={
                "argv": [
                    "-sv",
                    str(rtl),
                    "-npi_nl_rtl_opt",
                    "DetailRTL+DetailMux+GenBlock",
                ]
            },
        )
        queried = supervisor.call(
            "design",
            "object_query",
            {
                "model": "netlist",
                "scope": "TOP",
                "semantic_classes": ["register"],
                "limit": 10,
            },
        )
        assert len(queried["data"]["objects"]) == 2

        direct = supervisor.call(
            "design",
            "connectivity_direct",
            {"kind": "driver", "signals": ["TOP.wout"]},
        )
        driver = direct["data"]["items"][0]["objects"][0]
        assert driver["ref"]["npi_type"] == "INSTPORT"
        assert driver["owner"]["semantic_class"] == "register"

        trace = supervisor.call(
            "design",
            "trace",
            {"kind": "driver", "roots": ["TOP.wout"], "max_depth": 4, "max_nodes": 20},
        )
        assert trace["data"]["nodes"]
        assert trace["data"]["edges"]
        assert any(
            node["origin"] == "npi_language" for node in trace["data"]["nodes"]
        )

        language = supervisor.call(
            "design",
            "object_resolve",
            {"model": "language", "name": "TOP.wout"},
        )
        assert language["data"]["ref"]["model"] == "language"
        assert language["data"]["ref"]["object_id"].startswith("lang-1-")
        released = supervisor.call(
            "design",
            "release_objects",
            {"object_ids": [language["data"]["ref"]["object_id"]]},
        )
        assert released["data"]["released"]

        language_query_args = {
            "model": "language",
            "scope": "TOP",
            "limit": 2,
            "max_scan": 100,
        }
        language_query = supervisor.call(
            "design", "object_query", language_query_args
        )
        cached_language_query = supervisor.call(
            "design", "object_query", language_query_args
        )
        assert language_query["receipt"]["metrics"]["cache_misses"] == 1
        assert cached_language_query["receipt"]["metrics"]["cache_hits"] == 1
        object_id = language_query["data"]["objects"][0]["ref"]["object_id"]
        supervisor.call(
            "design",
            "release_objects",
            {"object_ids": [object_id]},
        )
        refreshed_language_query = supervisor.call(
            "design", "object_query", language_query_args
        )
        assert refreshed_language_query["receipt"]["metrics"]["cache_misses"] == 1

        source = supervisor.call(
            "design",
            "source_context",
            {
                "reference": queried["data"]["objects"][0]["ref"],
                "before_lines": 1,
                "after_lines": 1,
                "max_lines": 10,
            },
        )
        assert "always" in source["data"]["text"]
        assert source["data"]["fingerprint"].startswith("meta:file:")
        assert source["data"]["change_status"] == "unchanged"

        root = supervisor.call(
            "design",
            "object_resolve",
            {"model": "netlist", "name": "TOP"},
        )["data"]["ref"]
        first = supervisor.call(
            "design",
            "object_traverse",
            {
                "roots": [root],
                "relation": "netlist.inst.children",
                "depth": 2,
                "max_nodes": 1,
            },
        )
        assert first["status"] == "partial"
        assert first["data"]["truncated"]
        assert first["data"]["next_cursor"]
        resumed = supervisor.call(
            "design",
            "object_traverse",
            {
                "roots": [root],
                "relation": "netlist.inst.children",
                "depth": 2,
                "max_nodes": 1,
                "cursor": first["data"]["next_cursor"],
            },
        )
        resumed_again = supervisor.call(
            "design",
            "object_traverse",
            {
                "roots": [root],
                "relation": "netlist.inst.children",
                "depth": 2,
                "max_nodes": 1,
                "cursor": first["data"]["next_cursor"],
            },
        )
        assert resumed["data"] == resumed_again["data"]
        mismatch = supervisor.call(
            "design",
            "object_traverse",
            {
                "roots": [root],
                "relation": "netlist.inst.children",
                "depth": 1,
                "max_nodes": 1,
                "cursor": first["data"]["next_cursor"],
            },
        )
        assert mismatch["status"] == "failed"
        assert mismatch["error"]["code"] == "cursor_mismatch"
        supervisor.reload_context("design")
        stale = supervisor.call(
            "design",
            "object_traverse",
            {
                "roots": [root],
                "relation": "netlist.inst.children",
                "depth": 2,
                "max_nodes": 1,
                "cursor": first["data"]["next_cursor"],
            },
        )
        assert stale["status"] == "failed"
        assert stale["error"]["code"] == "cursor_expired"
    finally:
        supervisor.close_all()
