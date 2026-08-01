from stb.artifacts import ArtifactManager
from stb.config import Settings
from stb.dispatcher import ToolDispatcher
from stb.errors import StbError
from stb.supervisor import Supervisor


def test_cross_context_compare_and_streamed_first_divergence(tmp_path) -> None:
    settings = Settings(
        backend="fake",
        allowed_roots=str(tmp_path),
        artifact_root=tmp_path / "artifacts",
    )
    supervisor = Supervisor(settings)
    artifacts = ArtifactManager(settings.artifact_root)
    dispatcher = ToolDispatcher(settings, supervisor, artifacts)
    try:
        supervisor.open_context(
            "left",
            backend="fake",
            wave_specs=[{"wave_id": "run", "path": "normal.fsdb"}],
        )
        supervisor.open_context(
            "right",
            backend="fake",
            wave_specs=[{"wave_id": "run", "path": "divergent.fsdb"}],
        )
        sides = {
            "left": {"context_id": "left", "wave_id": "run", "signal": "top.req"},
            "right": {"context_id": "right", "wave_id": "run", "signal": "top.req"},
        }
        compared = dispatcher.dispatch(
            "wave_compute",
            {
                "context_id": "left",
                "request": {
                    "operation": "compare",
                    "context_mode": "cross",
                    **sides,
                    "times": ["10ns", "15ns", "20ns"],
                },
            },
        )
        assert [row["equal"] for row in compared["data"]["rows"]] == [
            True,
            False,
            True,
        ]

        request = {
            "operation": "first_divergence",
            "context_mode": "cross",
            **sides,
            "start": "0ns",
            "end": "20ns",
            "max_transitions": 2,
        }
        first = dispatcher.dispatch(
            "wave_compute", {"context_id": "left", "request": request}
        )
        assert first["status"] == "partial"
        second = dispatcher.dispatch(
            "wave_compute",
            {
                "context_id": "left",
                "request": {**request, "cursor": first["data"]["next_cursor"]},
            },
        )
        assert second["status"] == "complete"
        assert second["data"]["divergence"]["time"] == "12000000fs"
    finally:
        supervisor.close_all()
        artifacts.close()


def test_cross_context_cursor_is_invalid_after_wave_reload(tmp_path) -> None:
    settings = Settings(
        backend="fake",
        allowed_roots=str(tmp_path),
        artifact_root=tmp_path / "artifacts",
    )
    supervisor = Supervisor(settings)
    try:
        supervisor.open_context(
            "left",
            wave_specs=[{"wave_id": "wave", "path": "left.fsdb"}],
        )
        supervisor.open_context(
            "right",
            wave_specs=[{"wave_id": "wave", "path": "right-divergent.fsdb"}],
        )
        request = {
            "operation": "first_divergence",
            "context_mode": "cross",
            "left": {
                "context_id": "left",
                "wave_id": "wave",
                "signal": "top.req",
            },
            "right": {
                "context_id": "right",
                "wave_id": "wave",
                "signal": "top.req",
            },
            "start": "0fs",
            "end": "20ns",
            "max_transitions": 1,
        }
        first = supervisor.cross_wave_compute(request)
        cursor = first["data"]["next_cursor"]
        assert cursor
        supervisor.call(
            "right",
            "wave_manage",
            {"action": "reload", "wave_id": "wave"},
        )
        try:
            supervisor.cross_wave_compute({**request, "cursor": cursor})
        except StbError as exc:
            assert exc.code == "cursor_mismatch"
        else:
            raise AssertionError("expected cross-context cursor mismatch")
    finally:
        supervisor.close_all()
