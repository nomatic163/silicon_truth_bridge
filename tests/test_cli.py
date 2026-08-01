import json
import subprocess
import sys


def test_cli_batch_keeps_one_supervisor_lifecycle(tmp_path) -> None:
    workflow = tmp_path / "workflow.jsonl"
    commands = [
        {
            "tool": "context_manage",
            "request": {
                "action": "open",
                "context_id": "rtl",
                "backend": "fake",
            },
        },
        {
            "tool": "object_resolve",
            "request": {
                "context_id": "rtl",
                "request": {"name": "top.u_core.req"},
            },
        },
        {
            "tool": "context_manage",
            "request": {"action": "close", "context_id": "rtl"},
        },
    ]
    workflow.write_text(
        "".join(json.dumps(command) + "\n" for command in commands),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "stb.cli",
            "--backend",
            "fake",
            "--allowed-roots",
            str(tmp_path),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "batch",
            str(workflow),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    rows = [json.loads(line) for line in completed.stdout.splitlines()]
    assert rows[0]["state"] == "active"
    assert rows[1]["status"] == "complete"
    assert rows[1]["data"]["name"] == "req"
    assert rows[2]["state"] == "closed"


def test_cli_call_reports_invalid_json(tmp_path) -> None:
    request = tmp_path / "request.json"
    request.write_text("not-json", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "stb.cli",
            "--backend",
            "fake",
            "call",
            "context_manage",
            "--request",
            str(request),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    result = json.loads(completed.stdout)
    assert result["error"]["code"] == "invalid_request"


def test_cli_schema_and_replay(tmp_path) -> None:
    schema = subprocess.run(
        [sys.executable, "-m", "stb.cli", "schema", "wave_changes"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert schema.returncode == 0
    schema_result = json.loads(schema.stdout)
    assert schema_result["schema"]["additionalProperties"] is False
    assert "request" in schema_result["schema"]["properties"]

    replay_file = tmp_path / "replay.json"
    replay_file.write_text(
        json.dumps(
            {
                "tool": "context_manage",
                "request": {"action": "list"},
            }
        ),
        encoding="utf-8",
    )
    output_file = tmp_path / "response.json"
    replay = subprocess.run(
        [
            sys.executable,
            "-m",
            "stb.cli",
            "--output-file",
            str(output_file),
            "replay",
            str(replay_file),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert replay.returncode == 0
    assert replay.stdout == ""
    assert json.loads(output_file.read_text(encoding="utf-8"))["contexts"] == []


def test_cli_batch_partial_result_exits_three(tmp_path) -> None:
    workflow = tmp_path / "workflow.jsonl"
    commands = [
        {
            "tool": "context_manage",
            "request": {
                "action": "open",
                "context_id": "rtl",
                "backend": "fake",
                "wave_specs": [{"wave_id": "run", "path": "fake.fsdb"}],
            },
        },
        {
            "tool": "wave_changes",
            "request": {
                "context_id": "rtl",
                "request": {
                    "wave_id": "run",
                    "signals": ["top.clk"],
                    "start": "0fs",
                    "end": "20ns",
                    "max_changes": 2,
                },
            },
        },
        {
            "tool": "context_manage",
            "request": {"action": "close", "context_id": "rtl"},
        },
    ]
    workflow.write_text(
        "".join(json.dumps(command) + "\n" for command in commands),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "stb.cli",
            "--backend",
            "fake",
            "--allowed-roots",
            str(tmp_path),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "batch",
            str(workflow),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 3, completed.stderr
    rows = [json.loads(line) for line in completed.stdout.splitlines()]
    assert rows[1]["status"] == "partial"
