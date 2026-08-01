import pytest

from stb.config import Settings
from stb.errors import StbError
from stb.supervisor import Supervisor


def test_worker_lifecycle_and_query(tmp_path) -> None:
    settings = Settings(
        backend="fake",
        allowed_roots=str(tmp_path),
        artifact_root=tmp_path / "artifacts",
    )
    supervisor = Supervisor(settings)
    try:
        opened = supervisor.open_context("rtl")
        assert opened["state"] == "active"

        result = supervisor.call(
            "rtl", "object_resolve", {"name": "top.u_core.req"}
        )
        assert result["status"] == "complete"
        assert result["data"]["name"] == "req"

        reloaded = supervisor.reload_context("rtl")
        assert reloaded["worker_generation"] == 2
        assert supervisor.close_context("rtl")["state"] == "closed"
    finally:
        supervisor.close_all()


def test_context_reload_updates_worker_generation_and_invalidates_old_refs(tmp_path) -> None:
    settings = Settings(
        backend="fake",
        allowed_roots=str(tmp_path),
        artifact_root=tmp_path / "artifacts",
    )
    supervisor = Supervisor(settings)
    try:
        supervisor.open_context("rtl")
        resolved = supervisor.call("rtl", "object_resolve", {"name": "top.u_core.req"})
        old_ref = resolved["data"]["ref"]
        assert old_ref["worker_generation"] == 1

        reloaded = supervisor.reload_context("rtl")
        assert reloaded["worker_generation"] == 2
        resolved_after_reload = supervisor.call(
            "rtl", "object_resolve", {"name": "top.u_core.req"}
        )
        assert resolved_after_reload["receipt"]["worker_generation"] == 2
        assert resolved_after_reload["data"]["ref"]["worker_generation"] == 2

        stale = supervisor.call(
            "rtl",
            "object_get",
            {"references": [old_ref], "properties": ["width"]},
        )
        assert stale["status"] == "partial"
        assert stale["data"][0]["error_code"] == "stale_object_id"
    finally:
        supervisor.close_all()


def test_active_context_limit(tmp_path) -> None:
    settings = Settings(
        backend="fake",
        max_active_contexts=1,
        allowed_roots=str(tmp_path),
        artifact_root=tmp_path / "artifacts",
    )
    supervisor = Supervisor(settings)
    try:
        supervisor.open_context("a")
        try:
            supervisor.open_context("b")
        except Exception as exc:
            assert getattr(exc, "code", None) == "active_context_limit_reached"
        else:
            raise AssertionError("expected active context limit error")
    finally:
        supervisor.close_all()


def test_verdi_backend_requires_configured_home(tmp_path) -> None:
    supervisor = Supervisor(
        Settings(
            backend="verdi",
            verdi_home=None,
            allowed_roots=str(tmp_path),
            artifact_root=tmp_path / "artifacts",
        )
    )
    with pytest.raises(StbError) as error:
        supervisor.open_context("missing-verdi")
    assert error.value.code == "invalid_request"
    assert "STB_VERDI_HOME or VERDI_HOME" in error.value.message


def test_traversal_cursor_replay_mismatch_and_reload_invalidation(tmp_path) -> None:
    supervisor = Supervisor(
        Settings(
            backend="fake",
            allowed_roots=str(tmp_path),
            artifact_root=tmp_path / "artifacts",
        )
    )
    try:
        supervisor.open_context("rtl")
        root = supervisor.call(
            "rtl", "object_resolve", {"name": "top"}
        )["data"]["ref"]
        request = {
            "roots": [root],
            "relation": "children",
            "depth": 2,
            "max_nodes": 1,
        }
        first = supervisor.call("rtl", "object_traverse", request)
        cursor = first["data"]["next_cursor"]
        assert first["data"]["truncated"]

        replay = supervisor.call(
            "rtl", "object_traverse", {**request, "cursor": cursor}
        )
        replay_again = supervisor.call(
            "rtl", "object_traverse", {**request, "cursor": cursor}
        )
        assert replay["data"] == replay_again["data"]

        mismatch = supervisor.call(
            "rtl",
            "object_traverse",
            {**request, "depth": 1, "cursor": cursor},
        )
        assert mismatch["status"] == "failed"
        assert mismatch["error"]["code"] == "cursor_mismatch"

        supervisor.reload_context("rtl")
        stale = supervisor.call(
            "rtl", "object_traverse", {**request, "cursor": cursor}
        )
        assert stale["status"] == "failed"
        assert stale["error"]["code"] == "cursor_expired"
    finally:
        supervisor.close_all()
