import json
import time

from stb.artifacts import ArtifactManager
from stb.config import Settings
from stb.dispatcher import ToolDispatcher
from stb.supervisor import Supervisor


def test_metrics_history_and_logs(tmp_path) -> None:
    settings = Settings(
        backend="fake",
        dev_tools=True,
        allowed_roots=str(tmp_path),
        artifact_root=tmp_path / "artifacts",
    )
    supervisor = Supervisor(settings)
    try:
        supervisor.open_context("rtl")
        supervisor.call("rtl", "object_resolve", {"name": "top"})
        metrics = supervisor.metrics_snapshot()
        row = metrics["operations"]["object_resolve"]
        assert row["count"] == 1
        assert row["complete"] == 1
        assert row["response_bytes"] > 0
        assert row["input_bytes"] > 0
        assert row["mean_queue_ms"] >= 0
        history = supervisor.request_history(1)[0]
        assert history["method"] == "object_resolve"
        assert history["status"] == "complete"
        assert history["request_id"]
        logs = supervisor.read_logs("rtl", contains="object_resolve")
        assert logs["context_id"] == "rtl"
        event = json.loads(logs["lines"][-1])
        assert event["schema_version"] == "stb.worker-log.v1"
        assert event["method"] == "object_resolve"
        assert event["request_id"] == history["request_id"]
        assert event["response_bytes"] > 0
    finally:
        supervisor.close_all()


def test_admin_benchmark_reports_required_dimensions(tmp_path) -> None:
    settings = Settings(
        backend="fake",
        dev_tools=True,
        allowed_roots=str(tmp_path),
        artifact_root=tmp_path / "artifacts",
    )
    supervisor = Supervisor(settings)
    artifacts = ArtifactManager(settings.artifact_root)
    dispatcher = ToolDispatcher(settings, supervisor, artifacts)
    try:
        dispatcher.dispatch(
            "context_manage",
            {"action": "open", "context_id": "rtl", "backend": "fake"},
        )
        result = dispatcher.dispatch(
            "admin_benchmark",
            {
                "request": {
                    "context_id": "rtl",
                    "method": "object_resolve",
                    "args": {"name": "top"},
                    "iterations": 2,
                }
            },
        )
        assert result["status"] == "complete"
        assert result["p95_ms"] >= result["median_ms"]
        assert result["rss_bytes"] > 0
        assert result["worker_rss_bytes"] > 0
        assert result["metrics"]["input_bytes"] > 0
        assert result["metrics"]["response_bytes"] > 0
        assert result["metrics"]["last_receipt_metrics"]["transport_ms"] >= 0
    finally:
        supervisor.close_all()
        artifacts.close()


def test_artifact_job(tmp_path) -> None:
    manager = ArtifactManager(tmp_path)
    try:
        submitted = manager.submit(lambda: {"answer": 42})
        for _ in range(100):
            status = manager.status(submitted["job_id"])
            if status["status"] == "completed":
                break
            time.sleep(0.01)
        assert status["status"] == "completed"
        assert status["artifact"]["bytes"] > 0
        reopened = ArtifactManager(tmp_path)
        try:
            persisted = reopened.status(submitted["job_id"])
            assert persisted["status"] == "completed"
            assert persisted["artifact"]["sha256"] == status["artifact"]["sha256"]
        finally:
            reopened.close()
        assert manager.delete(submitted["artifact_id"])["deleted"]
    finally:
        manager.close()


def test_artifact_cancel_after_terminal_status_reports_not_cancelled(tmp_path) -> None:
    manager = ArtifactManager(tmp_path)
    try:
        submitted = manager.submit(lambda: {"answer": 42})
        for _ in range(100):
            status = manager.status(submitted["job_id"])
            if status["status"] == "completed":
                break
            time.sleep(0.01)
        assert status["status"] == "completed"
        cancelled = manager.cancel(submitted["job_id"])
        assert cancelled == {
            "job_id": submitted["job_id"],
            "cancelled": False,
            "status": "completed",
        }
    finally:
        manager.close()


def test_artifact_quota_failure_is_persisted(tmp_path) -> None:
    manager = ArtifactManager(
        tmp_path,
        max_artifact_bytes=16,
        max_total_bytes=32,
        shutdown_grace_sec=1,
    )
    try:
        submitted = manager.submit(lambda: {"payload": "x" * 100})
        for _ in range(100):
            status = manager.status(submitted["job_id"])
            if status["status"] == "failed":
                break
            time.sleep(0.01)
        assert status["status"] == "failed"
        assert "quota" in status["error"]
        assert not (tmp_path / f"{submitted['artifact_id']}.json").exists()
    finally:
        manager.close()


def test_running_job_becomes_interrupted_after_restart(tmp_path) -> None:
    job_id = "job-0123456789abcdef"
    artifact_id = "artifact-0123456789abcdef"
    (tmp_path / f"{job_id}.job.json").write_text(
        (
            '{"schema_version":"stb.job.v1","job_id":"'
            + job_id
            + '","artifact_id":"'
            + artifact_id
            + '","status":"running","created_at":1,"updated_at":1,'
            + '"metadata":{},"artifact":null,"error":null}'
        ),
        encoding="utf-8",
    )
    manager = ArtifactManager(tmp_path)
    try:
        assert manager.status(job_id)["status"] == "interrupted"
    finally:
        manager.close()


def test_running_artifact_job_cancels_cooperatively(tmp_path) -> None:
    manager = ArtifactManager(tmp_path, shutdown_grace_sec=1)
    try:
        def producer(cancel_event):
            while not cancel_event.is_set():
                time.sleep(0.001)
            return {"late": True}

        submitted = manager.submit(producer)
        for _ in range(100):
            if manager.status(submitted["job_id"])["status"] == "running":
                break
            time.sleep(0.001)
        cancelled = manager.cancel(submitted["job_id"])
        assert cancelled["cancelled"]
        for _ in range(100):
            status = manager.status(submitted["job_id"])
            if status["status"] == "cancelled":
                break
            time.sleep(0.001)
        assert status["status"] == "cancelled"
    finally:
        manager.close()
