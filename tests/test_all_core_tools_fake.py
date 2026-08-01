import time

from stb.artifacts import ArtifactManager
from stb.config import Settings
from stb.dispatcher import ToolDispatcher
from stb.supervisor import Supervisor
from stb.tool_inventory import CORE_TOOLS


def test_all_core_tools_execute_with_fake_backend(tmp_path) -> None:
    settings = Settings(
        backend="fake",
        allowed_roots=str(tmp_path),
        artifact_root=tmp_path / "artifacts",
    )
    supervisor = Supervisor(settings)
    artifacts = ArtifactManager(settings.artifact_root)
    dispatcher = ToolDispatcher(settings, supervisor, artifacts)
    called = set()

    def call(tool, request):
        called.add(tool)
        result = dispatcher.dispatch(tool, request)
        assert result.get("status") != "failed", (tool, result)
        return result

    try:
        call(
            "context_manage",
            {
                "action": "open",
                "context_id": "fake",
                "backend": "fake",
                "wave_specs": [{"wave_id": "run", "path": "fake.fsdb"}],
            },
        )
        call("wave_manage", {"action": "status", "context_id": "fake", "wave_id": "run"})
        call("catalog", {"context_id": "fake", "kind": "models"})
        resolved = call(
            "object_resolve",
            {"context_id": "fake", "request": {"name": "top.u_core.req"}},
        )["data"]
        ref = resolved["ref"]
        call(
            "object_get",
            {"context_id": "fake", "request": {"references": [ref], "properties": ["width"]}},
        )
        call(
            "object_query",
            {"context_id": "fake", "request": {"scope": "top", "limit": 10}},
        )
        call(
            "object_traverse",
            {
                "context_id": "fake",
                "request": {
                    "roots": [
                        {
                            "model": "netlist",
                            "context_id": "fake",
                            "worker_generation": 1,
                            "npi_type": "INST",
                            "full_name": "top",
                        }
                    ],
                    "relation": "children",
                    "depth": 2,
                },
            },
        )
        call(
            "connectivity_direct",
            {"context_id": "fake", "request": {"kind": "driver", "signals": ["top.u_core.req"]}},
        )
        call(
            "trace",
            {"context_id": "fake", "request": {"kind": "driver", "roots": ["top.u_core.req"]}},
        )
        call(
            "trace_active_driver",
            {"context_id": "fake", "request": {"signals": ["top.u_core.req"], "wave_id": "run", "time": "10ns"}},
        )
        call(
            "trace_value_origin",
            {"context_id": "fake", "request": {"signals": ["top.u_core.req"], "wave_id": "run", "time": "10ns"}},
        )
        call(
            "wave_value",
            {"context_id": "fake", "request": {"wave_id": "run", "signals": ["top.req"], "times": ["10ns"]}},
        )
        call(
            "wave_changes",
            {
                "context_id": "fake",
                "request": {
                    "wave_id": "run",
                    "signals": ["top.clk"],
                    "start": "0ns",
                    "end": "20ns",
                    "max_changes": 2,
                },
            },
        )
        call(
            "wave_compute",
            {
                "context_id": "fake",
                "request": {
                    "operation": "statistics",
                    "wave_id": "run",
                    "signals": ["top.clk"],
                    "start": "0ns",
                    "end": "20ns",
                },
            },
        )
        call(
            "source_context",
            {"context_id": "fake", "request": {"reference": ref}},
        )
        assertion = call(
            "object_resolve",
            {
                "context_id": "fake",
                "request": {
                    "model": "language",
                    "name": "top.a_req_to_data",
                },
            },
        )["data"]
        call(
            "assertion_structure",
            {
                "context_id": "fake",
                "request": {"reference": assertion["ref"]},
            },
        )
        call(
            "mapping",
            {
                "context_id": "fake",
                "request": {
                    "action": "resolve",
                    "wave_id": "run",
                    "design_full_name": "top.u_core.req",
                    "waveform_full_name": "top.req",
                },
            },
        )
        submitted = call(
            "artifact",
            {
                "action": "export",
                "request": {
                    "context_id": "fake",
                    "method": "object_query",
                    "args": {"scope": "top", "limit": 2},
                },
            },
        )
        for _ in range(100):
            status = call(
                "artifact",
                {"action": "status", "request": {"job_id": submitted["job_id"]}},
            )
            if status["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        assert status["status"] == "completed"
        paged = call(
            "artifact",
            {
                "action": "export",
                "request": {
                    "context_id": "fake",
                    "method": "wave_changes",
                    "args": {
                        "wave_id": "run",
                        "signals": ["top.clk"],
                        "start": "0ns",
                        "end": "20ns",
                        "max_changes": 2,
                    },
                },
            },
        )
        for _ in range(100):
            paged_status = call(
                "artifact",
                {"action": "status", "request": {"job_id": paged["job_id"]}},
            )
            if paged_status["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        assert paged_status["status"] == "completed"
        artifact_data = __import__("json").loads(
            __import__("pathlib").Path(
                paged_status["artifact"]["path"]
            ).read_text(encoding="utf-8")
        )
        assert artifact_data["summary"]["chunk_count"] == 3
        assert called == set(CORE_TOOLS)
    finally:
        supervisor.close_all()
        artifacts.close()
