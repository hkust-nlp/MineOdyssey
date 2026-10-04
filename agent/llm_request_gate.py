"""Cross-process admission control for shared LLM provider credentials."""

from __future__ import annotations

import fcntl
import json
import os
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, TextIO


@dataclass(frozen=True)
class LLMRequestGateConfig:
    directory: Path
    max_inflight: int
    starts_per_minute: float
    initial_burst: int
    slot_poll_interval_sec: float = 0.05

    def __post_init__(self) -> None:
        if self.max_inflight <= 0:
            raise ValueError("max_inflight must be positive")
        if self.starts_per_minute <= 0:
            raise ValueError("starts_per_minute must be positive")
        if self.initial_burst <= 0:
            raise ValueError("initial_burst must be positive")
        if self.initial_burst > self.max_inflight:
            raise ValueError("initial_burst must not exceed max_inflight")
        if self.slot_poll_interval_sec <= 0:
            raise ValueError("slot_poll_interval_sec must be positive")


@dataclass(frozen=True)
class LLMRequestGateLease:
    slot_index: int
    rate_wait_sec: float
    inflight_wait_sec: float


class SharedLLMRequestGate:
    """Coordinate request starts and in-flight calls through shared file locks.

    Every container must point at the same bind-mounted directory. Token-bucket
    state controls request starts while one ``flock`` file per slot limits calls
    that have been sent but have not returned yet. Kernel locks are released
    automatically when a worker exits, including abrupt container termination.
    """

    def __init__(
        self,
        config: LLMRequestGateConfig,
        *,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._clock = clock
        self._sleep = sleeper
        self.config.directory.mkdir(parents=True, exist_ok=True)
        self._slots_dir = self.config.directory / "inflight-slots"
        self._slots_dir.mkdir(exist_ok=True)
        self._queue_dir = self.config.directory / "start-queue"
        self._queue_dir.mkdir(exist_ok=True)
        self._state_path = self.config.directory / "rate-state.lock"

    def _open_locked_state(self) -> TextIO:
        descriptor = os.open(self._state_path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        fcntl.flock(handle, fcntl.LOCK_EX)
        return handle

    def _load_state(self, handle: TextIO, now: float) -> dict[str, Any]:
        handle.seek(0)
        raw = handle.read().strip()
        if not raw:
            return {
                "tokens": float(self.config.initial_burst),
                "updated_at": now,
                "cooldown_until": 0.0,
                "next_ticket": 0,
                "serving_ticket": 0,
            }
        try:
            state = json.loads(raw)
            tokens = float(state["tokens"])
            updated_at = float(state["updated_at"])
            cooldown_until = float(state.get("cooldown_until", 0.0))
            next_ticket = int(state.get("next_ticket", 0))
            serving_ticket = int(state.get("serving_ticket", 0))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"invalid shared LLM request gate state: {self._state_path}"
            ) from error
        if next_ticket < 0 or serving_ticket < 0 or serving_ticket > next_ticket:
            raise RuntimeError(f"invalid LLM request gate ticket state: {self._state_path}")
        elapsed = max(0.0, now - updated_at)
        refill_per_second = self.config.starts_per_minute / 60.0
        return {
            "tokens": min(
                float(self.config.initial_burst),
                tokens + elapsed * refill_per_second,
            ),
            # ``updated_at`` may intentionally be in the future while a shared
            # 429 cooldown is active. Preserve it so tokens cannot refill during
            # that quiet period and form another burst at its boundary.
            "updated_at": max(now, updated_at),
            "cooldown_until": cooldown_until,
            "next_ticket": next_ticket,
            "serving_ticket": serving_ticket,
        }

    @staticmethod
    def _save_state(handle: TextIO, state: dict[str, Any]) -> None:
        handle.seek(0)
        handle.truncate()
        json.dump(state, handle, separators=(",", ":"), sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())

    @staticmethod
    def _unlock_close(handle: TextIO) -> None:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()

    def _ticket_path(self, ticket: int) -> Path:
        return self._queue_dir / f"ticket-{ticket:020d}.lock"

    def _allocate_ticket(self) -> tuple[int, TextIO]:
        now = self._clock()
        state_handle = self._open_locked_state()
        ticket_handle: TextIO | None = None
        ticket_path: Path | None = None
        try:
            state = self._load_state(state_handle, now)
            ticket = int(state["next_ticket"])
            ticket_path = self._ticket_path(ticket)
            descriptor = os.open(ticket_path, os.O_RDWR | os.O_CREAT, 0o600)
            ticket_handle = os.fdopen(descriptor, "r+", encoding="utf-8")
            fcntl.flock(ticket_handle, fcntl.LOCK_EX)
            state["next_ticket"] = ticket + 1
            self._save_state(state_handle, state)
            return ticket, ticket_handle
        except Exception:
            if ticket_handle is not None:
                self._unlock_close(ticket_handle)
            if ticket_path is not None:
                ticket_path.unlink(missing_ok=True)
            raise
        finally:
            self._unlock_close(state_handle)

    def _ticket_is_active(self, ticket: int) -> bool:
        path = self._ticket_path(ticket)
        try:
            handle = path.open("r+", encoding="utf-8")
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return True
        self._unlock_close(handle)
        path.unlink(missing_ok=True)
        return False

    def _skip_abandoned_tickets(self, state: dict[str, Any], own_ticket: int) -> None:
        serving = int(state["serving_ticket"])
        next_ticket = int(state["next_ticket"])
        while serving < next_ticket and serving != own_ticket:
            if self._ticket_is_active(serving):
                break
            serving += 1
        state["serving_ticket"] = serving

    def wait_for_start(self) -> float:
        """Consume one shared start token in FIFO order."""
        waited = 0.0
        refill_per_second = self.config.starts_per_minute / 60.0
        ticket, ticket_handle = self._allocate_ticket()
        ticket_path = self._ticket_path(ticket)
        try:
            while True:
                now = self._clock()
                handle = self._open_locked_state()
                admitted = False
                try:
                    state = self._load_state(handle, now)
                    self._skip_abandoned_tickets(state, ticket)
                    serving = int(state["serving_ticket"])
                    cooldown_wait = max(0.0, state["cooldown_until"] - now)
                    if (
                        serving == ticket
                        and cooldown_wait <= 0
                        and state["tokens"] >= 1.0
                    ):
                        state["tokens"] -= 1.0
                        state["serving_ticket"] = ticket + 1
                        admitted = True
                        delay = 0.0
                    elif serving == ticket:
                        token_wait = max(
                            0.0, (1.0 - state["tokens"]) / refill_per_second
                        )
                        delay = cooldown_wait if cooldown_wait > 0 else token_wait
                    else:
                        tickets_ahead = max(1, ticket - serving)
                        delay = min(
                            1.0,
                            max(
                                self.config.slot_poll_interval_sec,
                                tickets_ahead / refill_per_second,
                            ),
                        )
                    self._save_state(handle, state)
                finally:
                    self._unlock_close(handle)
                if admitted:
                    self._unlock_close(ticket_handle)
                    ticket_handle = None
                    ticket_path.unlink(missing_ok=True)
                    return waited
                delay = max(delay, self.config.slot_poll_interval_sec)
                self._sleep(delay)
                waited += delay
        finally:
            if ticket_handle is not None:
                self._unlock_close(ticket_handle)
                ticket_path.unlink(missing_ok=True)

    def defer_after_rate_limit(self, delay_sec: float) -> None:
        """Apply one provider-wide cooldown and drain accumulated burst tokens."""
        delay_sec = max(0.0, float(delay_sec))
        now = self._clock()
        handle = self._open_locked_state()
        try:
            state = self._load_state(handle, now)
            cooldown_until = max(state["cooldown_until"], now + delay_sec)
            state.update(
                {
                    "tokens": 0.0,
                    # Prevent refill during the cooldown. Refill resumes gradually
                    # after the provider is eligible to receive another request.
                    "updated_at": cooldown_until,
                    "cooldown_until": cooldown_until,
                }
            )
            self._save_state(handle, state)
        finally:
            self._unlock_close(handle)

    def _acquire_inflight_slot(self) -> tuple[int, TextIO, float]:
        started = self._clock()
        while True:
            indices = list(range(self.config.max_inflight))
            random.shuffle(indices)
            for index in indices:
                path = self._slots_dir / f"slot-{index:03d}.lock"
                handle = path.open("a+", encoding="utf-8")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    continue
                return index, handle, max(0.0, self._clock() - started)
            self._sleep(self.config.slot_poll_interval_sec)

    @contextmanager
    def request(self) -> Iterator[LLMRequestGateLease]:
        """Admit one provider call and hold its in-flight slot until completion."""
        rate_wait = self.wait_for_start()
        slot_index, slot_handle, inflight_wait = self._acquire_inflight_slot()
        try:
            yield LLMRequestGateLease(
                slot_index=slot_index,
                rate_wait_sec=rate_wait,
                inflight_wait_sec=inflight_wait,
            )
        finally:
            self._unlock_close(slot_handle)
