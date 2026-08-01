from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from stb.artifacts import ArtifactManager
from stb.config import Settings
from stb.dispatcher import ToolDispatcher
from stb.supervisor import Supervisor
from stb.schemas import tool_json_schema
from stb.tool_inventory import CORE_TOOLS, DEV_TOOLS


def _read_json(path: str) -> dict[str, Any]:
    if path == "-":
        value = json.load(sys.stdin)
    else:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    return value


def _write_json(
    value: dict[str, Any], pretty: bool = False, stream: TextIO | None = None
) -> None:
    stream = stream or sys.stdout
    json.dump(
        value,
        stream,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
        sort_keys=pretty,
    )
    stream.write("\n")
    stream.flush()


def _exit_code(result: dict[str, Any]) -> int:
    status = result.get("status")
    if status == "failed":
        return 2
    if status == "partial":
        return 3
    return 0


def _build_runtime(settings: Settings) -> tuple[
    Supervisor, ArtifactManager, ToolDispatcher
]:
    supervisor = Supervisor(settings)
    artifacts = ArtifactManager(
        settings.artifact_root,
        settings.max_artifact_bytes,
        settings.max_artifact_total_bytes,
        settings.artifact_shutdown_grace_sec,
    )
    return supervisor, artifacts, ToolDispatcher(settings, supervisor, artifacts)


def _close_runtime(supervisor: Supervisor, artifacts: ArtifactManager) -> None:
    supervisor.close_all()
    artifacts.close()


def _normalize_command(value: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    tool = value.get("tool")
    if not isinstance(tool, str):
        raise ValueError("command requires string field 'tool'")
    request = value.get("request")
    if request is None:
        request = {key: item for key, item in value.items() if key != "tool"}
    if not isinstance(request, dict):
        raise ValueError("command request must be a JSON object")
    return tool, request


def _run_stream(
    stream: TextIO,
    dispatcher: ToolDispatcher,
    pretty: bool,
    interactive: bool,
    output: TextIO | None = None,
) -> int:
    result_code = 0
    while True:
        if interactive:
            sys.stderr.write("stb> ")
            sys.stderr.flush()
        raw = stream.readline()
        if not raw:
            break
        text = raw.strip()
        if not text:
            continue
        if interactive and text in {"quit", "exit"}:
            break
        try:
            value = json.loads(text)
            if not isinstance(value, dict):
                raise ValueError("command must be a JSON object")
            tool, request = _normalize_command(value)
            result = dispatcher.dispatch(tool, request)
        except (json.JSONDecodeError, ValueError) as exc:
            result = {
                "status": "failed",
                "error": {
                    "code": "invalid_request",
                    "message": str(exc),
                    "recoverable": True,
                },
            }
        _write_json(result, pretty, output)
        result_code = max(result_code, _exit_code(result))
    return result_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stb")
    parser.add_argument("--backend", default=None)
    parser.add_argument("--dev-tools", action="store_true")
    parser.add_argument("--allowed-roots")
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--receipt-only", action="store_true")
    parser.add_argument("--output-file", type=Path)
    parser.add_argument("--save-request", type=Path)
    parser.add_argument("--save-response", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    call = subparsers.add_parser("call")
    call.add_argument("tool", choices=CORE_TOOLS + DEV_TOOLS)
    call.add_argument("--request", default="-")

    batch = subparsers.add_parser("batch")
    batch.add_argument("workflow")

    subparsers.add_parser("shell")
    schema = subparsers.add_parser("schema")
    schema.add_argument("tool", choices=CORE_TOOLS + DEV_TOOLS)
    replay = subparsers.add_parser("replay")
    replay.add_argument("request")
    return parser


def _present(result: dict[str, Any], receipt_only: bool) -> dict[str, Any]:
    if not receipt_only:
        return result
    return {
        "status": result.get("status"),
        "error": result.get("error"),
        "receipt": result.get("receipt"),
    }


def main() -> None:
    args = _parser().parse_args()
    overrides: dict[str, Any] = {}
    if args.backend is not None:
        overrides["backend"] = args.backend
    if args.dev_tools:
        overrides["dev_tools"] = True
    if args.allowed_roots is not None:
        overrides["allowed_roots"] = args.allowed_roots
    if args.artifact_root is not None:
        overrides["artifact_root"] = args.artifact_root
    settings = Settings(**overrides)
    supervisor, artifacts, dispatcher = _build_runtime(settings)
    output_stream: TextIO = sys.stdout
    opened_output: TextIO | None = None
    if args.output_file is not None:
        args.output_file.parent.mkdir(parents=True, exist_ok=True)
        opened_output = args.output_file.open("w", encoding="utf-8")
        output_stream = opened_output
    try:
        if args.command == "call":
            request: dict[str, Any] = {}
            try:
                request = _read_json(args.request)
                result = dispatcher.dispatch(args.tool, request)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                result = {
                    "status": "failed",
                    "error": {
                        "code": "invalid_request",
                        "message": str(exc),
                        "recoverable": True,
                    },
                }
            if args.save_request is not None:
                args.save_request.parent.mkdir(parents=True, exist_ok=True)
                args.save_request.write_text(
                    json.dumps({"tool": args.tool, "request": request}, indent=2) + "\n",
                    encoding="utf-8",
                )
            if args.save_response is not None:
                args.save_response.parent.mkdir(parents=True, exist_ok=True)
                args.save_response.write_text(
                    json.dumps(result, indent=2) + "\n", encoding="utf-8"
                )
            _write_json(
                _present(result, args.receipt_only),
                args.pretty,
                output_stream,
            )
            code = _exit_code(result)
        elif args.command == "batch":
            with (
                sys.stdin
                if args.workflow == "-"
                else Path(args.workflow).open(encoding="utf-8")
            ) as stream:
                code = _run_stream(
                    stream, dispatcher, args.pretty, False, output_stream
                )
        elif args.command == "shell":
            code = _run_stream(
                sys.stdin, dispatcher, args.pretty, True, output_stream
            )
        elif args.command == "schema":
            result = {
                "status": "complete",
                "tool": args.tool,
                "schema": tool_json_schema(args.tool),
            }
            _write_json(result, True, output_stream)
            code = 0
        else:
            try:
                replay_value = _read_json(args.request)
                tool, request = _normalize_command(replay_value)
                result = dispatcher.dispatch(tool, request)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                result = {
                    "status": "failed",
                    "error": {
                        "code": "invalid_request",
                        "message": str(exc),
                        "recoverable": True,
                    },
                }
            _write_json(
                _present(result, args.receipt_only),
                args.pretty,
                output_stream,
            )
            code = _exit_code(result)
    finally:
        _close_runtime(supervisor, artifacts)
        if opened_output is not None:
            opened_output.close()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
