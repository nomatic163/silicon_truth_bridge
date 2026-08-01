from stb.config import Settings
from stb.server import build_server
from stb.supervisor import Supervisor
from stb.tool_inventory import CORE_TOOLS, DEV_TOOLS


def _names(server) -> set[str]:
    return set(server._tool_manager._tools)


def _request_schema(server, tool_name: str) -> dict:
    parameters = server._tool_manager._tools[tool_name].parameters
    request_ref = parameters["properties"]["request"]["$ref"]
    request_name = request_ref.rsplit("/", 1)[-1]
    return parameters["$defs"][request_name]


def test_core_tool_inventory(tmp_path) -> None:
    settings = Settings(
        backend="fake",
        dev_tools=False,
        allowed_roots=str(tmp_path),
        artifact_root=tmp_path / "artifacts",
    )
    supervisor = Supervisor(settings)
    try:
        server = build_server(settings, supervisor)
        assert _names(server) == set(CORE_TOOLS)
    finally:
        supervisor.close_all()


def test_worker_tools_publish_typed_request_schemas(tmp_path) -> None:
    settings = Settings(
        backend="fake",
        dev_tools=False,
        allowed_roots=str(tmp_path),
        artifact_root=tmp_path / "artifacts",
    )
    supervisor = Supervisor(settings)
    try:
        server = build_server(settings, supervisor)

        resolve = _request_schema(server, "object_resolve")
        assert {"model", "wave_id", "name"} <= set(resolve["properties"])
        assert resolve["properties"]["model"]["enum"] == [
            "netlist",
            "language",
            "waveform",
        ]

        wave_value = _request_schema(server, "wave_value")
        assert {"signals", "times"} <= set(wave_value["properties"])
        assert set(wave_value["required"]) == {"signals", "times"}

        assertion = _request_schema(server, "assertion_structure")
        assert set(assertion["required"]) == {"reference"}
        assert {
            "reference",
            "max_source_lines",
            "max_chars",
            "include_preprocessor",
            "allow_current_changed_source",
        } == set(assertion["properties"])

        context = server._tool_manager._tools["context_manage"].parameters
        assert context["properties"]["action"]["enum"] == [
            "open",
            "reload",
            "close",
            "list",
            "status",
            "release_objects",
        ]

        wave = server._tool_manager._tools["wave_manage"].parameters
        assert wave["properties"]["action"]["enum"] == [
            "attach",
            "reload",
            "detach",
            "list",
            "status",
        ]
    finally:
        supervisor.close_all()


def test_development_tool_inventory(tmp_path) -> None:
    settings = Settings(
        backend="fake",
        dev_tools=True,
        allowed_roots=str(tmp_path),
        artifact_root=tmp_path / "artifacts",
    )
    supervisor = Supervisor(settings)
    try:
        server = build_server(settings, supervisor)
        assert _names(server) == set(CORE_TOOLS) | set(DEV_TOOLS)
    finally:
        supervisor.close_all()
