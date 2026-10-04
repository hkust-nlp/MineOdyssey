"""Harbor launcher for the unchanged original Agent inside the main container.

Only lifecycle, credential delivery and trusted result collection run on the host.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import tempfile
import time

from harbor.agents.nop import NopAgent
from eval.harbor_agents.container import child_environment, terminal_reason
from eval.harbor_agents.transport import disable_exec_tty

ROOT = Path(__file__).resolve().parents[2]
CONTAINER_PYTHON = "/opt/mcbots-venv/bin/python"
CONTAINER_ROOT = "/opt/navigation-agent"
CONFIG_PATH = "/run/navigation-agent/config.json"
RUNNER = CONTAINER_PYTHON + " -m eval.harbor_agents.container"


def selected_environment():
    prefixes = ("MCBOTS_LLM_GATE_", "MCBOTS_LLM_429_", "MCBOTS_FRAME_", "MCBOTS_SCREENSHOT_")
    optional = {"MCBOTS_LLM_WATCHDOG_INTERVAL_SEC", "MCBOTS_SAMPLING_CONFIG_PATH"}
    return {key: value for key, value in os.environ.items() if key.startswith(prefixes) or key in optional}


class OriginalNavigationAgent(NopAgent):
    @staticmethod
    def name():
        return "navigation-original"

    def version(self):
        return "2.0.0"

    def load_model_config(self):
        config = json.loads(Path(os.environ["MCBOTS_HARBOR_API_MODELS_FILE"]).read_text())[
            os.environ["MCBOTS_HARBOR_MODEL_KEY"]]
        if self.model_name and config["modelname"] != self.model_name:
            raise ValueError("Harbor model differs from model config")
        return config

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

    async def rpc(self, action, payload=None):
        payload = payload or {}
        command = "nav rpc " + shlex.quote(action) + " " + shlex.quote(json.dumps(payload))
        # Reads can be repeated. Never replay an action/claim after an uncertain
        # transport failure: it may already have reached Minecraft.
        read_only = action in {"events", "result"} or (
            action == "rpc" and payload.get("method") == "GET")
        attempts = 3 if read_only else 1
        for attempt in range(1, attempts + 1):
            try:
                result = await self.environment.exec(command, timeout_sec=660 if action == "session" else 330)
                if result.return_code != 0:
                    self.transport_event(action=action, method=payload.get("method"), path=payload.get("path"),
                                         attempt=attempt, return_code=result.return_code,
                                         stdout=result.stdout, stderr=result.stderr)
                    raise RuntimeError(f"Harbor {action} transport failed (exit {result.return_code})")
                return json.loads(result.stdout)
            except Exception as error:
                self.transport_event(action=action, attempt=attempt,
                                     error_type=type(error).__name__, error=str(error))
                if attempt == attempts:
                    # A failed lifecycle transport is an infrastructure outcome,
                    # never a scored navigation failure.
                    self.transport_failure = {"action": action, "type": type(error).__name__}
                    raise
                await asyncio.sleep(0.25 * attempt)

    async def host_terminal(self, reason):
        result = await self.environment.service_exec(
            "python3 /opt/harbor-nav/gateway.py --agent-terminal " + shlex.quote(reason),
            service="world", timeout_sec=30)
        if result.return_code:
            raise RuntimeError("Trusted termination channel failed")

    async def record_model_metadata(self, config, values):
        # Only reproducibility settings go into world; provider credentials and
        # endpoint are delivered separately to the main Agent container.
        allowed = {"max_tokens", "temperature", "top_p", "seed", "reasoning_effort", "thinking"}
        parameters = json.loads(values["MCBOTS_MODEL_PARAMS_JSON"])
        metadata = {key: value for key, value in parameters.items() if key in allowed}
        metadata.update(model_id=config["modelname"],
                        api_protocol=values["MCBOTS_API_PROTOCOL"],
                        action_protocol=values["MCBOTS_ACTION_PROTOCOL"])
        result = await self.environment.service_exec(
            "python3 /opt/harbor-nav/gateway.py --model-metadata " + shlex.quote(json.dumps(metadata)),
            service="world", timeout_sec=30)
        if result.return_code:
            raise RuntimeError("Trusted model metadata channel failed")

    async def verify_container_source(self):
        result = await self.environment.exec(RUNNER + " --check", cwd=CONTAINER_ROOT, timeout_sec=30)
        if result.return_code:
            raise RuntimeError("Container Agent installation invalid; rebuild the exported task")
        receipt = json.loads(result.stdout)
        expected_paths = {str(path.relative_to(ROOT)) for path in (ROOT / "agent").rglob("*.py")}
        if set(receipt["source_sha256"]) != expected_paths:
            raise RuntimeError("Container Agent source inventory differs from the launcher")
        for name, digest in receipt["source_sha256"].items():
            if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
                raise RuntimeError("Container Agent source differs from the launcher: " + name)
        if receipt["uv_lock_sha256"] != hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest():
            raise RuntimeError("Container Agent dependency lock differs from the launcher")
        if receipt["runtime_sha256"] != hashlib.sha256((ROOT / "eval/harbor_agents/container.py").read_bytes()).hexdigest():
            raise RuntimeError("Container Agent bridge differs from the launcher")
        # Real container namespace IDs are recorded for validation, not assumed.
        (self.logs_dir / "container-installation.json").write_text(json.dumps(receipt, indent=2))

    async def deliver_config(self, config):
        prepared = await self.environment.exec(
            "install -d -m 700 /run/navigation-agent", timeout_sec=30)
        if prepared.return_code:
            raise RuntimeError("Cannot prepare private Agent configuration directory")
        with tempfile.TemporaryDirectory(prefix="navigation-agent-config-") as directory:
            path = Path(directory) / "config.json"
            with open(path, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
                json.dump({**config, "environment": selected_environment()}, output)
            await self.environment.upload_file(path, CONFIG_PATH)
        protected = await self.environment.exec("chmod 600 " + CONFIG_PATH, timeout_sec=30)
        if protected.return_code:
            raise RuntimeError("Cannot protect Agent configuration")

    async def collect_agent_state(self):
        if not self.environment.capabilities.mounted:
            await self.environment.download_dir(str(self.environment_logs_dir), self.logs_dir)
        path = self.logs_dir / "runner-status.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def update_context(self, context):
        path = self.logs_dir / "progress.json"
        if path.exists():
            progress = json.loads(path.read_text())
            for key in ("n_input_tokens", "n_output_tokens", "n_cache_tokens"):
                setattr(context, key, progress.get(key, 0))
        receipt = self.logs_dir / "parity-receipt.json"
        if receipt.exists():
            data = json.loads(receipt.read_text())
            context.metadata.update({key: data[key] for key in ("setting", "limits", "agent_location")})

    def runner_command(self):
        return RUNNER

    async def run(self, instruction, environment, context):
        self.environment = environment
        self.transport_failure = None
        self.completion = {}
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        disable_exec_tty(environment)
        config = self.load_model_config()
        self.transport_redactions = (config["api_key"], config["base_url"])
        context.n_input_tokens = context.n_output_tokens = context.n_cache_tokens = 0
        context.metadata = {"model": config["modelname"], "agent": self.name(),
                            "agent_location": "main_container"}
        # Validate image contents before starting Minecraft or delivering secrets.
        await self.verify_container_source()
        session = await self.rpc("session")
        parameters = dict(config.get("model_params") or {})
        values = {
            "MCBOTS_MODEL_PARAMS_JSON": json.dumps(parameters),
            "MCBOTS_API_PROTOCOL": parameters.get("api_protocol", config.get("api_protocol", "chat_completions")),
            "MCBOTS_ACTION_PROTOCOL": parameters.get("action_protocol", config.get("action_protocol", "tool_calls")),
        }
        try:
            await self.record_model_metadata(config, values)
            await self.deliver_config(config)
            command = (self.runner_command() + " --config " + CONFIG_PATH + " --logs "
                       + shlex.quote(str(self.environment_logs_dir))
                       + " > " + shlex.quote(str(self.environment_logs_dir / "runner.log")) + " 2>&1")
            result = await environment.exec(command, cwd=CONTAINER_ROOT, timeout_sec=22800)
            state = await self.collect_agent_state()
            self.completion = await self.rpc("result")
            if not self.completion.get("terminal"):
                reason = state.get("reason") if result.return_code == 0 else "agent_crash"
                if reason not in {"step_limit", "llm_failure_limit", "agent_crash", "agent_stopped_without_completion"}:
                    reason = "agent_crash"
                await self.host_terminal(reason)
                self.completion = await self.rpc("result")
        except asyncio.CancelledError:
            await self.host_terminal("agent_crash")
            raise
        except Exception as error:
            self.transport_event(phase="supervision", error_type=type(error).__name__, error=str(error))
            await self.host_terminal("agent_crash")
            self.completion = await self.rpc("result")
        finally:
            # This terminates a container process, never a host Agent process.
            await environment.exec(RUNNER + " --stop", cwd=CONTAINER_ROOT, timeout_sec=30)
            await environment.exec("rm -f " + CONFIG_PATH, timeout_sec=30)
            await self.collect_agent_state()
            self.update_context(context)
            context.metadata["completion"] = self.completion

    def check_transport(self):
        if self.transport_failure:
            raise RuntimeError("Harbor lifecycle transport failed; trial is an infrastructure failure")
