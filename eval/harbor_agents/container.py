"""Container-local lifecycle bridge; all model decisions remain in agent.main."""
from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import http.client
import importlib.metadata
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import sys
import threading
import time
import tomllib
from types import SimpleNamespace

from agent.navigation_prompt import BASELINE_COMMIT, build_navigation_system_prompt
from scripts.analysis.finalize_agent_messages import finalize_agent_messages

ROOT = Path(__file__).resolve().parents[2]
SOCKET = "/run/navigation/api.sock"
PID_FILE = Path("/run/navigation-agent.pid")


def installation_details():
    packages = ("openai", "pillow", "requests", "httpx")
    versions = {name: importlib.metadata.version(name) for name in packages}
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    expected = {p["name"]: p["version"] for p in lock["package"] if p["name"] in packages}
    if versions != expected:
        raise RuntimeError("Container Agent dependencies differ from uv.lock; rebuild the image")
    return {
        "dependencies": versions,
        "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted((ROOT / "agent").rglob("*.py"))},
        "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        "runtime_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "namespaces": {name: os.readlink("/proc/self/ns/" + name) for name in ("pid", "mnt", "net")},
        "python": sys.executable,
    }

AGENT_ENV = {
    "observe_interval_sec": "MCBOTS_OBSERVE_INTERVAL",
    "default_periodic_observe": "MCBOTS_DEFAULT_OBSERVE_ENABLED",
    "allow_model_observe_toggle": "MCBOTS_ALLOW_MODEL_OBSERVE_TOGGLE",
    "bash_timeout_sec": "MCBOTS_EXEC_TIMEOUT",
    "llm_timeout_sec": "MCBOTS_LLM_TIMEOUT_SEC",
    "llm_max_retries": "MCBOTS_LLM_MAX_RETRIES",
    "max_consecutive_llm_failures": "MCBOTS_MAX_CONSECUTIVE_LLM_FAILURES",
    "six_view_enabled": "MCBOTS_ENABLE_PANORAMA",
    "max_images_in_context": "MCBOTS_MAX_IMAGES_IN_CONTEXT",
    "auto_summarize_turn_threshold": "MCBOTS_AUTO_SUMMARIZE_TURN_THRESHOLD",
    "auto_summarize_token_threshold": "MCBOTS_AUTO_SUMMARIZE_TOKEN_THRESHOLD",
    "max_conversation_rounds": "MCBOTS_MAX_CONVERSATION_ROUNDS",
}


def child_environment(session, config, logs, port):
    """Use the effective world setting, not another set of agent defaults."""
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
               "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"}
    values = {key: value for key, value in os.environ.items() if key in allowed}
    # Optional transport/provider tuning has the same opt-in environment surface
    # as agent.main. Effective benchmark settings below remain authoritative.
    prefixes = ("MCBOTS_LLM_GATE_", "MCBOTS_LLM_429_", "MCBOTS_FRAME_", "MCBOTS_SCREENSHOT_")
    optional = {"MCBOTS_LLM_WATCHDOG_INTERVAL_SEC", "MCBOTS_SAMPLING_CONFIG_PATH"}
    values.update({key: value for key, value in os.environ.items()
                   if key.startswith(prefixes) or key in optional})
    for key, variable in AGENT_ENV.items():
        value = session["agent"][key]
        values[variable] = str(value).lower() if isinstance(value, bool) else str(value)
    parameters = dict(config.get("model_params") or {})
    api_protocol = parameters.pop("api_protocol", config.get("api_protocol", "chat_completions"))
    action_protocol = parameters.pop("action_protocol", config.get("action_protocol", "tool_calls"))
    parameters.pop("model_id", None)
    values.update({
        "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "MCBOTS_API_KEY": config["api_key"], "MCBOTS_BASE_URL": config["base_url"],
        "MCBOTS_MODEL": config["modelname"], "MCBOTS_API_PROTOCOL": api_protocol,
        "MCBOTS_ACTION_PROTOCOL": action_protocol,
        "MCBOTS_MODEL_PARAMS_JSON": json.dumps(parameters),
        "MCBOTS_INITIAL_USER_INPUT": session["prompt"],
        "MCBOTS_SYSTEM_PROMPT_PROFILE": "navigation", "MCBOTS_EVAL_MODE": "true",
        "MCBOTS_REMOTE_BASH_HOST": "127.0.0.1", "MCBOTS_REMOTE_BASH_PORT": str(port),
        "MCBOTS_DISPLAY": session["display"], "MCBOTS_PLAYER": "client",
        "MCBOTS_RECORD_DIR": str(logs / "agent-record"),
        "MCBOTS_NAV_CLAIM_REQUEST_DIR": str(logs / "claims/requests"),
        "MCBOTS_NAV_CLAIM_RESPONSE_DIR": str(logs / "claims/responses"),
        "MCBOTS_NAV_EVENT_PATH": str(logs / "evaluator-events.jsonl"),
        "MCBOTS_NAV_AGENT_STATUS_PATH": str(logs / "agent-status.json"),
        "MCBOTS_MAX_LLM_REQUEST_SUCCESSES": str(session["limits"]["max_assistant_steps"]),
        "MCBOTS_NAVIGATION_HINTS_ENABLED": str(session["eval_setting"]["navigation_hints_enabled"]).lower(),
        "MCBOTS_RECORD_VIDEO": "false",
        # No main-container Minecraft config: state fetch uses the transported mcapi.
        "MCBOTS_RUNTIME_CONFIG": str(logs / "unused-world-runtime.json"),
        "MCBOTS_WORKSPACE_ROOT": "/workspace",
        "MCBOTS_LOG_FILE_PATH": "/workspace/game/logs/latest.log",
    })
    if session["agent"]["six_view_enabled"] or session["record_video"]:
        raise ValueError("This transport supports the pinned single-view, no-video setting")
    return values


def terminal_reason(status, return_code):
    reason = status.get("stop_reason", "")
    if reason in {"max_llm_request_successes", "max_llm_request_failures"}:
        return "step_limit"
    if reason == "max_consecutive_llm_failures":
        return "llm_failure_limit"
    if reason.startswith("exception:") or return_code != 0:
        return "agent_crash"
    return "agent_stopped_without_completion"


class RemoteBashProxy(ThreadingHTTPServer):
    """Loopback transport for unmodified Environment's requests calls."""
    daemon_threads = True
    block_on_close = False

    def __init__(self, loop, rpc):
        self.loop, self.rpc = loop, rpc
        super().__init__(("127.0.0.1", 0), RemoteBashHandler)


class RemoteBashHandler(BaseHTTPRequestHandler):
    def handle_request(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 131072:
                raise ValueError("Invalid request size")
            data = json.loads(self.rfile.read(length)) if length else None
            payload = {"method": self.command, "path": self.path, "data": data}
            future = asyncio.run_coroutine_threadsafe(self.server.rpc("rpc", payload), self.server.loop)
            response = future.result(timeout=330)
            body = json.dumps(response["body"]).encode()
            self.send_response(response["status"])
        except Exception:
            body = b'{"success":false,"error":"Harbor action transport unavailable"}'
            self.send_response(502)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = do_POST = handle_request

    def log_message(self, *_args):
        pass


class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET)


def socket_request(action, payload):
    connection = Connection("localhost", timeout=660 if action == "session" else 330)
    try:
        connection.request("POST", "/" + action, json.dumps(payload),
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise RuntimeError(f"World {action} returned HTTP {response.status}")
        return json.loads(body)
    finally:
        connection.close()


class ContainerRunner:
    def __init__(self, config, logs):
        self.config = config
        self.model_name = config["modelname"]
        self.logs_dir = Path(logs)
        self.transport_redactions = (config["api_key"], config["base_url"])
        self.transport_failure = None
        self.completion = {}

    @staticmethod
    def name():
        return "navigation-original"

    async def agent_interpreter(self):
        executable = sys.executable
        packages = ("openai", "pillow", "requests", "httpx")
        process = await asyncio.create_subprocess_exec(
            executable, "-c",
            "import importlib.metadata as m,json; print(json.dumps({p:m.version(p) for p in " + repr(packages) + "}))",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await process.communicate()
        if process.returncode:
            raise RuntimeError("Original Agent dependencies missing; run uv sync --frozen")
        versions = json.loads(stdout)
        lock = tomllib.loads((ROOT / "uv.lock").read_text())
        expected = {p["name"]:p["version"] for p in lock["package"] if p["name"] in packages}
        if versions != expected:
            raise RuntimeError("Original Agent dependencies differ from uv.lock; run uv sync --frozen")
        return executable, versions

    def transport_event(self, **event):
        # No commands or request bodies: keep diagnostics bounded and credential-free.
        for key in ("stdout", "stderr", "error"):
            if key in event:
                value = str(event[key])[:4096]
                for secret in getattr(self, "transport_redactions", ()):
                    if secret:
                        value = value.replace(secret, "[redacted]")
                event[key] = value
        with (self.logs_dir / "transport-events.jsonl").open("a") as output:
            output.write(json.dumps({"time_unix": time.time(), **event}) + "\n")

    async def pump_feedback(self):
        reply = await self.rpc("events", {"offset": self.event_offset})
        if reply["events"]:
            with (self.logs_dir / "evaluator-events.jsonl").open("a") as output:
                for event in reply["events"]:
                    output.write(json.dumps(event) + "\n")
        self.event_offset = reply["offset"]
        for request in sorted((self.logs_dir / "claims/requests").glob("*.request.json")):
            target = self.logs_dir / "claims/responses" / request.name.replace(".request.json", ".response.json")
            if target.exists():
                continue
            claim = json.loads(request.read_text())
            response = await self.rpc("claim-done", {"claim_id": claim["claim_id"]})
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(response))
            temporary.replace(target)

    def update_usage(self, context):
        journal = self.logs_dir / "agent-record/messages.jsonl"
        if not journal.exists():
            return
        with journal.open() as source:
            source.seek(self.journal_offset)
            while True:
                position = source.tell()
                line = source.readline()
                if not line.endswith("\n"):
                    self.journal_offset = position
                    break
                msg = json.loads(line)["message"]
                if msg.get("role") != "assistant":
                    continue
                usage = msg.get("usage") or {}
                context.n_input_tokens += usage.get("prompt_tokens", 0) or 0
                context.n_output_tokens += usage.get("completion_tokens", 0) or 0
                context.n_cache_tokens += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
                self.responses += 1
        (self.logs_dir / "progress.json").write_text(json.dumps({
            "model": self.model_name, "responses_including_summaries": self.responses,
            "n_input_tokens": context.n_input_tokens, "n_output_tokens": context.n_output_tokens,
            "elapsed_sec": round(time.monotonic() - self.started, 1),
            "completion": self.completion}, indent=2))

    async def stop_child(self, process):
        if process.returncode is not None:
            return
        process.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 10)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    def check_transport(self):
        if self.transport_failure:
            raise RuntimeError("Harbor action transport failed; trial is an infrastructure failure")

    async def rpc(self, action, payload=None):
        payload = payload or {}
        read_only = action in {"events", "result"} or (
            action == "rpc" and payload.get("method") == "GET")
        attempts = 3 if read_only else 1
        for attempt in range(1, attempts + 1):
            try:
                return await asyncio.to_thread(socket_request, action, payload)
            except Exception as error:
                self.transport_event(action=action, method=payload.get("method"),
                                     path=payload.get("path"), attempt=attempt,
                                     error_type=type(error).__name__, error=str(error))
                if attempt == attempts:
                    self.transport_failure = {"action": action, "type": type(error).__name__}
                    raise
                await asyncio.sleep(0.25 * attempt)

    def write_status(self, state, reason=None, return_code=None):
        payload = {"state": state, "reason": reason, "return_code": return_code,
                   "agent_location": "main_container", "completion": self.completion,
                   "transport_failure": self.transport_failure}
        target = self.logs_dir / "runner-status.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n")
        temporary.replace(target)

    async def run(self):
        self.event_offset = self.journal_offset = self.responses = 0
        self.started = time.monotonic()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.write_status("starting")
        context = SimpleNamespace(n_input_tokens=0, n_output_tokens=0, n_cache_tokens=0)
        process = proxy = None
        reason, return_code = "agent_crash", None
        try:
            executable, versions = await self.agent_interpreter()
            session = await self.rpc("session")
            for suffix in ("claims/requests", "claims/responses"):
                (self.logs_dir / suffix).mkdir(parents=True, exist_ok=True)
            proxy = RemoteBashProxy(asyncio.get_running_loop(), self.rpc)
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            values = child_environment(session, self.config, self.logs_dir, proxy.server_port)
            receipt = {
                "model": self.model_name, "agent": self.name(), "agent_location": "main_container",
                "setting": session["agent"], "limits": session["limits"],
                "model_parameters": self.config.get("model_params") or {},
                "action_protocol": values["MCBOTS_ACTION_PROTOCOL"],
                "api_protocol": values["MCBOTS_API_PROTOCOL"], "dependencies": versions,
                "transport": "container_local_unix_socket",
                "source_baseline_commit": BASELINE_COMMIT,
                "prompt_baseline": "historical_formal_native_tools" if values["MCBOTS_ACTION_PROTOCOL"] == "tool_calls" else "initial_submission_xml",
                "eval_setting": session["eval_setting"], "runtime_readback": session["runtime_readback"],
                "system_prompt_sha256": hashlib.sha256(build_navigation_system_prompt(
                    hints_enabled=session["eval_setting"]["navigation_hints_enabled"],
                    panorama_enabled=session["agent"]["six_view_enabled"],
                    action_protocol=values["MCBOTS_ACTION_PROTOCOL"]).encode()).hexdigest(),
                "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
                "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                    for name in ("agent/agent.py", "agent/main.py", "agent/navigation_prompt.py",
                                 "agent/env.py", "agent/navigation_completion.py")},
                "runtime_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "optional_runtime_settings": {key: value for key, value in values.items()
                    if key.startswith(("MCBOTS_LLM_GATE_", "MCBOTS_LLM_429_", "MCBOTS_FRAME_", "MCBOTS_SCREENSHOT_"))},
                "namespaces": {name: os.readlink("/proc/self/ns/" + name) for name in ("pid", "mnt", "net")},
                "python": executable,
            }
            with (self.logs_dir / "original-agent.log").open("wb") as output:
                process = await asyncio.create_subprocess_exec(
                    executable, "-m", "agent.main", cwd=ROOT, env=values,
                    stdout=output, stderr=asyncio.subprocess.STDOUT)
                receipt["agent_pid"] = process.pid
                receipt["agent_namespaces"] = {
                    name: os.readlink(f"/proc/{process.pid}/ns/{name}") for name in ("pid", "mnt", "net")}
                (self.logs_dir / "parity-receipt.json").write_text(json.dumps(receipt, indent=2))
                self.write_status("running")
                while process.returncode is None:
                    self.check_transport()
                    await self.pump_feedback()
                    self.completion = await self.rpc("result")
                    self.check_transport()
                    self.update_usage(context)
                    if self.completion.get("terminal"):
                        try:
                            await asyncio.wait_for(process.wait(), 2)
                        except asyncio.TimeoutError:
                            await self.stop_child(process)
                        break
                    await asyncio.sleep(0.5)
                await process.wait()
                return_code = process.returncode
                status_path = self.logs_dir / "agent-status.json"
                status = json.loads(status_path.read_text()) if status_path.exists() else {}
                reason = terminal_reason(status, return_code)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.transport_event(phase="supervision", error_type=type(error).__name__, error=str(error))
        finally:
            if process is not None:
                await self.stop_child(process)
            if proxy is not None:
                await asyncio.to_thread(proxy.shutdown)
                proxy.server_close()
            self.update_usage(context)
            if (self.logs_dir / "agent-record/messages.jsonl").exists():
                finalize_agent_messages(self.logs_dir / "agent-record")
            self.write_status("finished", reason, return_code)


def read_config(path):
    try:
        if path.stat().st_mode & 0o077:
            raise ValueError("Agent configuration must have mode 0600")
        config = json.loads(path.read_text())
    finally:
        path.unlink(missing_ok=True)
    return config


def stop_runner():
    if not PID_FILE.exists():
        return
    pid = int(PID_FILE.read_text())
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
        if b"eval.harbor_agents.container" not in command:
            raise RuntimeError("Agent runner PID no longer matches")
        os.kill(pid, signal.SIGTERM)
    except (FileNotFoundError, ProcessLookupError):
        pass


async def run_config(config, logs):
    # Only explicit navigation tuning crosses the host/container boundary.
    prefixes = ("MCBOTS_LLM_GATE_", "MCBOTS_LLM_429_", "MCBOTS_FRAME_", "MCBOTS_SCREENSHOT_")
    optional = {"MCBOTS_LLM_WATCHDOG_INTERVAL_SEC", "MCBOTS_SAMPLING_CONFIG_PATH"}
    for key, value in config.pop("environment", {}).items():
        if not (key.startswith(prefixes) or key in optional) or not isinstance(value, str):
            raise ValueError("Unsupported Agent environment override")
        os.environ[key] = value
    runner = ContainerRunner(config, logs)
    task = asyncio.create_task(runner.run())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    finally:
        PID_FILE.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--logs", type=Path, default=Path("/logs/agent"))
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        print(json.dumps(installation_details()))
        return
    if args.stop:
        stop_runner()
        return
    if args.config is None:
        parser.error("--config is required")
    config = read_config(args.config)
    PID_FILE.write_text(str(os.getpid()))
    try:
        asyncio.run(run_config(config, args.logs))
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    main()
