"""Agent-side client for evaluator-owned navigation completion claims."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


class NavigationClaimError(RuntimeError):
    """Raised when the evaluator claim channel is unavailable or malformed."""


def _atomic_write_request(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


class NavigationClaimClient:
    """Write claim requests and read evaluator responses from separate roots."""

    def __init__(
        self,
        *,
        request_dir: Path,
        response_dir: Path,
        event_path: Path | None = None,
        timeout_sec: float = 8.0,
        poll_interval_sec: float = 0.05,
    ):
        self.request_dir = Path(request_dir)
        self.response_dir = Path(response_dir)
        self.event_path = Path(event_path) if event_path is not None else None
        self.event_offset = 0
        self.timeout_sec = max(float(timeout_sec), 0.1)
        self.poll_interval_sec = max(float(poll_interval_sec), 0.01)

    def claim(self) -> dict[str, Any]:
        claim_id = uuid.uuid4().hex
        request_path = self.request_dir / f"{claim_id}.request.json"
        response_path = self.response_dir / f"{claim_id}.response.json"
        _atomic_write_request(
            request_path,
            {
                "schema_version": 1,
                "action": "claim_done",
                "claim_id": claim_id,
                "issued_at_unix_sec": time.time(),
            },
        )
        deadline = time.monotonic() + self.timeout_sec
        while time.monotonic() < deadline:
            try:
                payload = json.loads(response_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                time.sleep(self.poll_interval_sec)
                continue
            except (OSError, json.JSONDecodeError) as exc:
                raise NavigationClaimError(
                    f"invalid evaluator response for claim {claim_id}: {exc}"
                ) from exc
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                raise NavigationClaimError(
                    f"unsupported evaluator response for claim {claim_id}"
                )
            if payload.get("claim_id") != claim_id:
                raise NavigationClaimError(
                    f"evaluator response claim ID mismatch for {claim_id}"
                )
            return payload
        raise NavigationClaimError(
            f"evaluator did not answer claim_done within {self.timeout_sec:.1f}s"
        )

    def read_events(self) -> list[str]:
        if self.event_path is None or not self.event_path.is_file():
            return []
        messages: list[str] = []
        with self.event_path.open("r", encoding="utf-8") as handle:
            handle.seek(self.event_offset)
            for line in handle:
                payload = json.loads(line)
                messages.append(str(payload["message"]))
            self.event_offset = handle.tell()
        return messages
