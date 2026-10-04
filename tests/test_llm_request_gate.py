import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from agent.agent import Agent
from agent.llm_request_gate import (
    LLMRequestGateConfig,
    LLMRequestGateLease,
    SharedLLMRequestGate,
)


class _FakeTime:
    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class SharedLLMRequestGateTest(unittest.TestCase):
    def _config(self, directory: Path, **overrides) -> LLMRequestGateConfig:
        values = {
            "directory": directory,
            "max_inflight": 2,
            "starts_per_minute": 60.0,
            "initial_burst": 2,
            "slot_poll_interval_sec": 0.01,
        }
        values.update(overrides)
        return LLMRequestGateConfig(**values)

    def test_initial_burst_then_refills_at_configured_rate(self) -> None:
        fake = _FakeTime()
        with tempfile.TemporaryDirectory() as tmp:
            gate = SharedLLMRequestGate(
                self._config(Path(tmp)),
                clock=fake.clock,
                sleeper=fake.sleep,
            )
            self.assertEqual(gate.wait_for_start(), 0.0)
            self.assertEqual(gate.wait_for_start(), 0.0)
            self.assertAlmostEqual(gate.wait_for_start(), 1.0)
        self.assertEqual(fake.sleeps, [1.0])

    def test_rate_limit_cooldown_drains_burst_before_gradual_refill(self) -> None:
        fake = _FakeTime()
        with tempfile.TemporaryDirectory() as tmp:
            gate = SharedLLMRequestGate(
                self._config(Path(tmp)),
                clock=fake.clock,
                sleeper=fake.sleep,
            )
            gate.defer_after_rate_limit(5.0)
            self.assertAlmostEqual(gate.wait_for_start(), 6.0)
        self.assertEqual(fake.sleeps, [5.0, 1.0])

    def test_inflight_slot_is_released_after_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(
                Path(tmp),
                max_inflight=1,
                initial_burst=1,
                starts_per_minute=60_000.0,
            )
            first = SharedLLMRequestGate(config)
            second = SharedLLMRequestGate(config)
            first.wait_for_start = lambda: 0.0
            second.wait_for_start = lambda: 0.0
            attempted = threading.Event()
            acquired = threading.Event()

            def acquire_second() -> None:
                attempted.set()
                with second.request():
                    acquired.set()

            with first.request():
                thread = threading.Thread(target=acquire_second)
                thread.start()
                self.assertTrue(attempted.wait(1.0))
                time.sleep(0.05)
                self.assertFalse(acquired.is_set())
            thread.join(timeout=1.0)
            self.assertTrue(acquired.is_set())


class _RateLimitError(Exception):
    status_code = 429
    response = None


class _ImageParseError(Exception):
    status_code = 400
    body = {"error": {"code": "1210", "message": "图片输入格式/解析错误"}}


class _FakeGate:
    def __init__(self) -> None:
        self.delays: list[float] = []

    @contextmanager
    def request(self):
        yield LLMRequestGateLease(0, 0.0, 0.0)

    def defer_after_rate_limit(self, delay: float) -> None:
        self.delays.append(delay)


class AgentRateLimitRetryTest(unittest.TestCase):
    def _agent(self, *, retries: int) -> Agent:
        agent = Agent.__new__(Agent)
        agent.llm_429_max_retries = retries
        agent.llm_429_backoff_base_sec = 5.0
        agent.llm_429_backoff_max_sec = 60.0
        agent.llm_429_backoff_jitter = 0.0
        agent.llm_request_gate = _FakeGate()
        return agent

    def test_429_retries_reenter_gate_without_escaping_logical_request(self) -> None:
        agent = self._agent(retries=10)
        calls = 0

        def request():
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise _RateLimitError()
            return "ok"

        self.assertEqual(agent._run_provider_request(request, attempt_id=7), "ok")
        self.assertEqual(calls, 3)
        self.assertEqual(agent.llm_request_gate.delays, [5.0, 10.0])

    def test_non_429_error_is_not_retried(self) -> None:
        agent = self._agent(retries=10)
        calls = 0

        def request():
            nonlocal calls
            calls += 1
            raise RuntimeError("broken")

        with self.assertRaisesRegex(RuntimeError, "broken"):
            agent._run_provider_request(request, attempt_id=8)
        self.assertEqual(calls, 1)
        self.assertEqual(agent.llm_request_gate.delays, [])

    def test_glm_image_parse_error_retries_with_bounded_backoff(self) -> None:
        agent = self._agent(retries=0)
        calls = 0

        def request():
            nonlocal calls
            calls += 1
            if calls <= 3:
                raise _ImageParseError()
            return "ok"

        with mock.patch("agent.agent.time.sleep") as sleep:
            self.assertEqual(agent._run_provider_request(request, attempt_id=9), "ok")
        self.assertEqual(calls, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [10.0, 20.0, 30.0])

    def test_other_http_400_is_not_retried(self) -> None:
        agent = self._agent(retries=0)
        error = RuntimeError("other bad request")
        error.status_code = 400
        calls = 0

        def request():
            nonlocal calls
            calls += 1
            raise error

        with self.assertRaisesRegex(RuntimeError, "other bad request"):
            agent._run_provider_request(request, attempt_id=10)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
