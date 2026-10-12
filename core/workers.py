"""Thread-safe background job management for long-running lab operations."""

from __future__ import annotations

import threading
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .config import redact


def _now() -> str:
    """Return an ISO 8601 UTC timestamp string with 'Z' suffix."""
    # Format: 2026-09-25T12:34:56.789Z
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class JobRecord:
    """A single background job's lifecycle record."""
    id: str                       # Unique job identifier (UUID hex)
    name: str                     # Human-readable job name (e.g., "chat-turn")
    status: str = "queued"        # Current status: queued, running, completed, failed, cancelled
    created_at: str = field(default_factory=_now)  # When the job was submitted
    started_at: str | None = None  # When the job started executing
    finished_at: str | None = None  # When the job finished (completed/failed/cancelled)
    result: Any = None            # The job's return value (set on completion)
    error: str | None = None      # Error message (set on failure)
    traceback: str | None = None  # Redacted traceback (set on failure)

    def as_dict(self) -> dict[str, Any]:
        """Convert the job record to a dictionary for API/Streamlit consumption."""
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
            "traceback": self.traceback,
        }


class JobManager:
    """Small executor wrapper suitable for Streamlit session state."""

    def __init__(self, max_workers: int = 4) -> None:
        # Validate that at least one worker thread is configured
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        # Create a ThreadPoolExecutor for running jobs in background threads
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="agentic-lab"
        )
        # Dictionary mapping job ID -> JobRecord for all jobs (completed and active)
        self._jobs: dict[str, JobRecord] = {}
        # Dictionary mapping job ID -> Future for tracking in-flight execution
        self._futures: dict[str, Future[Any]] = {}
        # Re-entrant lock to protect all shared state (jobs, futures)
        self._lock = threading.RLock()

    def submit(self, name: str, function: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
        """Submit a function for background execution and return its job ID."""
        from uuid import uuid4

        # Generate a unique job ID
        job_id = uuid4().hex
        # Create a new JobRecord with "queued" status
        record = JobRecord(id=job_id, name=name)
        with self._lock:
            # Store the job record
            self._jobs[job_id] = record
            # Submit the wrapped _run method (not the function directly) so we can track status
            future = self._executor.submit(self._run, job_id, function, *args, **kwargs)
            self._futures[job_id] = future
        return job_id

    def _run(
        self,
        job_id: str,
        function: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Internal wrapper that runs the function and updates the job record."""
        with self._lock:
            # Update the job record to "running" status with a start timestamp
            record = self._jobs[job_id]
            record.status = "running"
            record.started_at = _now()
        try:
            # Execute the actual function with the provided arguments
            result = function(*args, **kwargs)
            with self._lock:
                # Store the result and mark the job as completed
                self._jobs[job_id].result = result
                self._jobs[job_id].status = "completed"
            return result
        except Exception as exc:
            # On exception, mark the job as failed with redacted error info
            with self._lock:
                record = self._jobs[job_id]
                record.status = "failed"
                record.error = redact(f"{type(exc).__name__}: {exc}")
                record.traceback = redact(traceback.format_exc())
            # Re-raise the exception to propagate it to the Future (for error handling)
            raise
        finally:
            with self._lock:
                record = self._jobs[job_id]
                # If the job didn't reach completed/failed status (e.g., future was cancelled),
                # mark it as cancelled
                if record.status not in {"completed", "failed"}:
                    record.status = "cancelled"
                # Record the finish timestamp regardless of outcome
                record.finished_at = _now()

    def get(self, job_id: str) -> dict[str, Any] | None:
        """Retrieve a job record by ID as a dictionary, or None if not found."""
        with self._lock:
            record = self._jobs.get(job_id)
            return record.as_dict() if record else None

    def list(self) -> list[dict[str, Any]]:
        """List all job records (most recent first)."""
        with self._lock:
            return [
                self._jobs[key].as_dict()
                for key in sorted(
                    self._jobs,
                    key=lambda key: (self._jobs[key].created_at, key),
                    reverse=True,
                )
            ]

    def cancel(self, job_id: str) -> bool:
        """Attempt to cancel a queued or running job. Returns True if cancellation was requested."""
        with self._lock:
            future = self._futures.get(job_id)
            record = self._jobs.get(job_id)
            # If the job or its future doesn't exist, cannot cancel
            if future is None or record is None:
                return False
            # Only queued/running jobs can be cancelled
            if record.status in {"queued", "running"}:
                future.cancel()
                # If the job hadn't started yet, update its status to cancelled
                if record.status == "queued":
                    record.status = "cancelled"
                    record.finished_at = _now()
                return True
            # Job is already in a terminal state (completed/failed), can't cancel
            return False

    def shutdown(self, wait: bool = True) -> None:
        """Shut down the executor, optionally waiting for running jobs to finish."""
        # cancel_futures=True cancels any not-yet-started futures
        self._executor.shutdown(wait=wait, cancel_futures=True)


# Module-level singleton instance (lazily initialized)
_default_manager: JobManager | None = None
# Lock to protect lazy initialization of the singleton
_default_lock = threading.Lock()


def default_job_manager() -> JobManager:
    """Return the shared default JobManager, creating it on first access."""
    global _default_manager
    with _default_lock:
        if _default_manager is None:
            _default_manager = JobManager()
        return _default_manager


def submit_job(name: str, function: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    """Submit a job to the default JobManager and return its ID."""
    return default_job_manager().submit(name, function, *args, **kwargs)


def get_job(job_id: str) -> dict[str, Any] | None:
    """Look up a job by ID from the default JobManager."""
    return default_job_manager().get(job_id)


def list_jobs() -> list[dict[str, Any]]:
    """List all jobs from the default JobManager."""
    return default_job_manager().list()


def cancel_job(job_id: str) -> bool:
    """Cancel a job by ID from the default JobManager."""
    return default_job_manager().cancel(job_id)


__all__ = [
    "JobManager",
    "JobRecord",
    "cancel_job",
    "default_job_manager",
    "get_job",
    "list_jobs",
    "submit_job",
]

