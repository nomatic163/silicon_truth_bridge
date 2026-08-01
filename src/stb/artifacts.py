from __future__ import annotations

import hashlib
import inspect
import json
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from stb.errors import StbError


_SAFE_ID = re.compile(r"^(?:artifact|job)-[a-f0-9]{16}$")


@dataclass
class Job:
    job_id: str
    artifact_id: str
    status: str
    created_at: float
    updated_at: float
    metadata: dict[str, Any]
    future: Future[dict[str, Any]] | None = None
    artifact: dict[str, Any] | None = None
    error: str | None = None
    cancel_event: threading.Event | None = None


class ArtifactManager:
    def __init__(
        self,
        root: Path,
        max_artifact_bytes: int = 1 << 30,
        max_total_bytes: int = 20 << 30,
        shutdown_grace_sec: float = 5.0,
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_artifact_bytes = max_artifact_bytes
        self.max_total_bytes = max_total_bytes
        self.shutdown_grace_sec = shutdown_grace_sec
        self.executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="stb-artifact"
        )
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._load_jobs()

    def _job_path(self, job_id: str) -> Path:
        self._validate_id(job_id, "job")
        return self.root / f"{job_id}.job.json"

    def _artifact_path(self, artifact_id: str) -> Path:
        self._validate_id(artifact_id, "artifact")
        return self.root / f"{artifact_id}.json"

    def _validate_id(self, value: str, kind: str) -> None:
        if not _SAFE_ID.fullmatch(value) or not value.startswith(f"{kind}-"):
            raise StbError("invalid_request", f"invalid {kind} id")

    def _atomic_json(self, path: Path, value: Any) -> None:
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
            temp.write_bytes(payload)
            temp.replace(path)
        except (OSError, TypeError, ValueError) as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise StbError("artifact_write_failed", str(exc)) from exc

    def _persist_job(self, job: Job) -> None:
        self._atomic_json(
            self._job_path(job.job_id),
            {
                "schema_version": "stb.job.v1",
                "job_id": job.job_id,
                "artifact_id": job.artifact_id,
                "status": job.status,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "metadata": job.metadata,
                "artifact": job.artifact,
                "error": job.error,
            },
        )

    def _load_jobs(self) -> None:
        for path in sorted(self.root.glob("job-*.job.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                status = raw["status"]
                if status in {"queued", "running"}:
                    status = "interrupted"
                    raw["status"] = status
                    raw["updated_at"] = time.time()
                    self._atomic_json(path, raw)
                job = Job(
                    job_id=raw["job_id"],
                    artifact_id=raw["artifact_id"],
                    status=status,
                    created_at=float(raw["created_at"]),
                    updated_at=float(raw["updated_at"]),
                    metadata=raw.get("metadata") or {},
                    artifact=raw.get("artifact"),
                    error=raw.get("error"),
                )
            except (OSError, ValueError, KeyError, TypeError):
                continue
            self.jobs[job.job_id] = job

    def _artifact_total_bytes(self) -> int:
        return sum(
            path.stat().st_size
            for path in self.root.glob("artifact-*.json")
            if not path.name.endswith(".manifest.json")
        )

    def _write(self, job_id: str, producer: Callable[..., Any]) -> dict[str, Any]:
        with self._lock:
            job = self.jobs[job_id]
            job.status = "running"
            job.updated_at = time.time()
            self._persist_job(job)
        try:
            cancel_event = job.cancel_event or threading.Event()
            if len(inspect.signature(producer).parameters) > 0:
                data = producer(cancel_event)
            else:
                data = producer()
            if cancel_event.is_set():
                raise StbError("job_cancelled", "artifact job was cancelled")
            payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            if len(payload) > self.max_artifact_bytes:
                raise StbError(
                    "artifact_write_failed",
                    "artifact exceeds per-artifact quota",
                    {"bytes": len(payload), "limit": self.max_artifact_bytes},
                )
            with self._lock:
                total = self._artifact_total_bytes()
                if total + len(payload) > self.max_total_bytes:
                    raise StbError(
                        "artifact_write_failed",
                        "artifact root exceeds total quota",
                        {
                            "current_bytes": total,
                            "requested_bytes": len(payload),
                            "limit": self.max_total_bytes,
                        },
                    )
                path = self._artifact_path(job.artifact_id)
                temp = self.root / f".{job.artifact_id}.{uuid.uuid4().hex}.tmp"
                temp.write_bytes(payload)
                temp.replace(path)
                digest = hashlib.sha256(payload).hexdigest()
                sidecar = self.root / f"{job.artifact_id}.manifest.json"
                manifest = {
                    "schema_version": "stb.artifact.v1",
                    "artifact_id": job.artifact_id,
                    "job_id": job.job_id,
                    "created_at": time.time(),
                    "sha256": digest,
                    "bytes": len(payload),
                    "metadata": job.metadata,
                }
                self._atomic_json(sidecar, manifest)
                artifact = {
                    "artifact_id": job.artifact_id,
                    "path": str(path),
                    "bytes": len(payload),
                    "sha256": digest,
                    "manifest_path": str(sidecar),
                }
                job.status = "completed"
                job.updated_at = time.time()
                job.artifact = artifact
                self._persist_job(job)
                return artifact
        except Exception as exc:
            with self._lock:
                job = self.jobs[job_id]
                job.status = (
                    "cancelled"
                    if isinstance(exc, StbError) and exc.code == "job_cancelled"
                    else "failed"
                )
                job.updated_at = time.time()
                job.error = str(exc)
                self._persist_job(job)
            raise

    def submit(
        self,
        producer: Callable[..., Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact_id = f"artifact-{uuid.uuid4().hex[:16]}"
        job_id = f"job-{uuid.uuid4().hex[:16]}"
        now = time.time()
        job = Job(
            job_id=job_id,
            artifact_id=artifact_id,
            status="queued",
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
            cancel_event=threading.Event(),
        )
        with self._lock:
            self.jobs[job_id] = job
            self._persist_job(job)
            job.future = self.executor.submit(self._write, job_id, producer)
        return {"job_id": job_id, "artifact_id": artifact_id, "status": "queued"}

    def status(self, job_id: str) -> dict[str, Any]:
        self._validate_id(job_id, "job")
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise StbError("job_not_found", f"job not found: {job_id}")
            result = {
                "job_id": job.job_id,
                "artifact_id": job.artifact_id,
                "status": job.status,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "metadata": job.metadata,
            }
            if job.artifact is not None:
                result["artifact"] = job.artifact
            if job.error is not None:
                result["error"] = job.error
            return result

    def cancel(self, job_id: str) -> dict[str, Any]:
        self._validate_id(job_id, "job")
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise StbError("job_not_found", f"job not found: {job_id}")
            if job.status in {"completed", "failed", "cancelled", "interrupted"}:
                return {"job_id": job_id, "cancelled": False, "status": job.status}
            if job.cancel_event is not None:
                job.cancel_event.set()
            cancelled = bool(job.future and job.future.cancel())
            if cancelled or job.status in {"queued", "running"}:
                job.status = "cancelled"
                job.updated_at = time.time()
                self._persist_job(job)
            return {
                "job_id": job_id,
                "cancelled": cancelled or job.status == "cancelled",
                "status": job.status,
            }

    def list(self) -> list[dict[str, Any]]:
        return [self.status(job_id) for job_id in sorted(self.jobs)]

    def delete(self, artifact_id: str) -> dict[str, Any]:
        path = self._artifact_path(artifact_id)
        if not path.exists():
            raise StbError("artifact_not_found", f"artifact not found: {artifact_id}")
        path.unlink()
        sidecar = self.root / f"{artifact_id}.manifest.json"
        sidecar.unlink(missing_ok=True)
        return {"artifact_id": artifact_id, "deleted": True}

    def close(self) -> None:
        deadline = time.monotonic() + self.shutdown_grace_sec
        while time.monotonic() < deadline:
            running = [
                job
                for job in self.jobs.values()
                if job.future is not None and not job.future.done()
            ]
            if not running:
                break
            time.sleep(0.01)
        self.executor.shutdown(wait=False, cancel_futures=True)
