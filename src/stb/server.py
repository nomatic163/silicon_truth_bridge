from __future__ import annotations

import atexit
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from stb.artifacts import ArtifactManager
from stb.config import Settings
from stb.dispatcher import ToolDispatcher
from stb.schemas import (
    CORE_PAYLOAD_MODELS,
    CatalogFilters,
    DesignSpec,
    WaveSpec,
)
from stb.supervisor import Supervisor


def _payload(value: BaseModel | dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python", exclude_none=True, by_alias=True)
    return value


def build_server(
    settings: Settings | None = None,
    supervisor: Supervisor | None = None,
) -> FastMCP:
    settings = settings or Settings()
    supervisor = supervisor or Supervisor(settings)
    artifacts = ArtifactManager(
        settings.artifact_root,
        settings.max_artifact_bytes,
        settings.max_artifact_total_bytes,
        settings.artifact_shutdown_grace_sec,
    )
    dispatcher = ToolDispatcher(settings, supervisor, artifacts)
    atexit.register(supervisor.close_all)
    atexit.register(artifacts.close)
    mcp = FastMCP(
        "silicon_truth_bridge",
        instructions=(
            "Read-only deterministic evidence from Verdi design databases and FSDB. "
            "All reasoning remains with the calling agent."
        ),
    )

    @mcp.tool()
    def context_manage(
        action: Literal["open", "reload", "close", "list", "status", "release_objects"],
        context_id: str | None = None,
        backend: Literal["fake", "verdi"] | None = None,
        design_spec: DesignSpec | None = None,
        wave_specs: list[WaveSpec] | None = None,
        object_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Open, reload, close, list, get status, or release context objects."""
        return dispatcher.dispatch(
            "context_manage",
            {
                "action": action,
                "context_id": context_id,
                "backend": backend,
                "design_spec": _payload(design_spec),
                "wave_specs": [_payload(spec) for spec in wave_specs]
                if wave_specs is not None
                else None,
                "object_ids": object_ids,
            },
        )

    @mcp.tool()
    def wave_manage(
        action: Literal["attach", "reload", "detach", "list", "status"],
        context_id: str,
        wave_id: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:
        """Attach, reload, detach, list, or get status for waveform resources."""
        return dispatcher.dispatch(
            "wave_manage",
            {
                "action": action,
                "context_id": context_id,
                "wave_id": wave_id,
                "path": path,
            },
        )

    @mcp.tool()
    def catalog(
        context_id: str,
        kind: Literal[
            "models",
            "object_types",
            "semantic_classes",
            "properties",
            "relations",
            "backend_capabilities",
            "wave_operations",
            "operators",
            "limits",
        ],
        filters: CatalogFilters | None = None,
    ) -> dict[str, Any]:
        """Discover context-specific models, properties, relations, and operators."""
        return dispatcher.dispatch(
            "catalog",
            {"context_id": context_id, "kind": kind, "filters": _payload(filters) or {}},
        )

    def register_worker_tool(name: str, description: str) -> None:
        request_model = CORE_PAYLOAD_MODELS[name]

        def operation(context_id: str, request: BaseModel) -> dict[str, Any]:
            return dispatcher.dispatch(
                name,
                {"context_id": context_id, "request": _payload(request)},
            )

        operation.__name__ = name
        operation.__doc__ = description
        operation.__annotations__ = {
            "context_id": str,
            "request": request_model,
            "return": dict[str, Any],
        }
        mcp.tool(name=name)(operation)

    register_worker_tool(
        "object_resolve",
        "Resolve an exact object. For waveform signals set model='waveform', "
        "wave_id, and name; netlist is the default model.",
    )
    register_worker_tool(
        "object_get",
        "Read properties for exact ObjectRef entries returned by resolve or query.",
    )
    register_worker_tool(
        "object_query",
        "Run a bounded declarative object query. Set model, scope, npi_types or "
        "semantic_classes; waveform queries also require wave_id.",
    )
    register_worker_tool(
        "object_traverse",
        "Traverse a cataloged relation from ObjectRef roots with bounded depth.",
    )
    register_worker_tool(
        "connectivity_direct",
        "Return one-hop driver or load evidence for exact design signal names.",
    )
    register_worker_tool(
        "trace",
        "Return a bounded driver, load, path, fanin, or fanout evidence graph.",
    )
    register_worker_tool(
        "trace_active_driver",
        "Evaluate active and feasible driver branches for signals at a wave time.",
    )
    register_worker_tool(
        "trace_value_origin",
        "Trace sampled value origin across state boundaries at a wave time.",
    )
    register_worker_tool(
        "wave_value",
        "Read typed waveform values. Request requires signals and unit-bearing "
        "times such as ['200ns']; set wave_id when multiple waves are attached.",
    )
    register_worker_tool(
        "wave_changes",
        "Read bounded waveform transitions. Request requires signals, start, and end.",
    )
    register_worker_tool(
        "wave_compute",
        "Run a typed bounded waveform operation such as sample, statistics, "
        "compare, period, pulse, xz, or first_divergence.",
    )
    register_worker_tool(
        "source_context",
        "Read bounded source context anchored to an exact ObjectRef.",
    )
    register_worker_tool(
        "assertion_structure",
        "Return bounded structural evidence for an NPI concurrent assertion.",
    )
    register_worker_tool(
        "mapping",
        "Resolve, validate, or explain deterministic design-to-wave mappings.",
    )

    @mcp.tool()
    def artifact(
        action: str,
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Manage bounded evidence artifacts and asynchronous jobs."""
        return dispatcher.dispatch(
            "artifact",
            {"action": action, "request": request or {}},
        )

    if settings.dev_tools:

        @mcp.tool()
        def admin_doctor() -> dict[str, Any]:
            """Check runtime, MCP, and Verdi Python NPI prerequisites."""
            return dispatcher.dispatch("admin_doctor", {})

        @mcp.tool()
        def admin_metrics(
            action: str = "snapshot",
            request: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Read or reset low-overhead supervisor and worker metrics."""
            return dispatcher.dispatch(
                "admin_metrics",
                {"action": action, "request": request or {}},
            )

        @mcp.tool()
        def admin_trace(
            action: str,
            request: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Configure or read correlated request traces."""
            return dispatcher.dispatch(
                "admin_trace",
                {"action": action, "request": request or {}},
            )

        @mcp.tool()
        def admin_logs(
            action: str = "list",
            request: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Read bounded structured worker and native logs."""
            return dispatcher.dispatch(
                "admin_logs",
                {"action": action, "request": request or {}},
            )

        @mcp.tool()
        def admin_benchmark(request: dict[str, Any]) -> dict[str, Any]:
            """Run a bounded worker performance benchmark."""
            return dispatcher.dispatch(
                "admin_benchmark",
                {"request": request},
            )

        @mcp.tool()
        def admin_selftest(
            request: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Run predefined transport and backend self-tests."""
            return dispatcher.dispatch(
                "admin_selftest",
                {"request": request or {}},
            )

    return mcp


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
