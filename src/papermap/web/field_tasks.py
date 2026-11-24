"""Track background taxonomy edits and optional handbooks separately for each field."""
from __future__ import annotations

import threading
from concurrent.futures import Future


class FieldTasks:
    def __init__(self, pipeline, background):
        self.pipeline = pipeline
        self.background = background
        self._lock = threading.Lock()
        self._tasks: dict[str, Future] = {}

    def _start(self, key, fn, *args) -> Future:
        with self._lock:
            current = self._tasks.get(key)
            if current and not current.done():
                return current
            task = Future()
            self._tasks[key] = task

            def work():
                try:
                    result = fn(*args)
                except Exception as exc:
                    # Keep the message for the review UI without keeping worker stacks alive.
                    task.set_exception(ValueError(str(exc)))
                else:
                    task.set_result(result)

            self.background(key, work)
            return task

    def _state(self, key):
        with self._lock:
            task = self._tasks.get(key)
        if task is None:
            return {"state": "idle"}
        if not task.done():
            return {"state": "working"}
        error = task.exception()
        if error is not None:
            return {"state": "error", "message": str(error)}
        return {"state": "done"}

    def handbook(self, slug):
        return self._start(f"handbook:{slug}", self.pipeline.generate_handbook, slug)

    def handbook_state(self, slug):
        return self._state(f"handbook:{slug}")

    def establish(self, pending_id, details, make_handbook):
        def create():
            # Retrying delivery after a disconnect must not buy a second handbook.
            row = self.pipeline.registry.pending_by_id(pending_id)
            if row is not None and row["status"] == "established":
                return row["merged_into"]
            slug = self.pipeline.establish_pending(pending_id, **details)
            if make_handbook:
                self.handbook(slug)
            return slug

        return self._start(f"establish:{pending_id}", create)

    def establishment_state(self, pending_id):
        status = self._state(f"establish:{pending_id}")
        if status["state"] in ("working", "error"):
            return status
        row = self.pipeline.registry.pending_by_id(pending_id)
        if row is not None and row["status"] == "established":
            return {"state": "done", "url": f"/field/{row['merged_into']}?created=1"}
        return {"state": "idle"}
