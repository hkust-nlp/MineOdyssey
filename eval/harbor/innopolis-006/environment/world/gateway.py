#!/usr/bin/env python3
"""Expose sandboxed actions over a Unix socket, keeping evaluator files private."""
from http.server import BaseHTTPRequestHandler
import json
import math
import os
from pathlib import Path
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import http.client
import shlex
import base64
import uuid
import re
import shutil

ROOT = Path("/workspace/mcbots")
TASK_ID = json.loads(Path(__file__).with_name("task-spec.json").read_text())["task_id"]
if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]{3}", TASK_ID):
    raise ValueError("Invalid baked task ID")
RUN = ROOT / "eval/runtime/navigation/harbor" / TASK_ID
RESULT = ROOT / "eval/results/navigation/harbor" / TASK_ID / "completion.json"
MODEL_METADATA = ROOT / "external-agent-model.json"
SOCKET = Path("/run/navigation/api.sock")
LOCK = threading.Lock()
PROCESS = None
MAX_EXEC_TIMEOUT_SEC = 120


def read_json(path):
    return json.loads(path.read_text())


def atomic_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data))
    temporary.replace(path)


def write_model_metadata(metadata):
    allowed = {"model_id", "api_protocol", "action_protocol", "max_tokens",
               "temperature", "top_p", "seed", "reasoning_effort", "thinking"}
    if not isinstance(metadata, dict) or set(metadata) - allowed:
        raise ValueError("Unexpected model metadata fields")
    if any(not isinstance(metadata.get(key), str) or not metadata[key]
           for key in ("model_id", "api_protocol", "action_protocol")):
        raise ValueError("Missing model identity/protocol")
    atomic_json(MODEL_METADATA, metadata)


def collected_run_metadata(run):
    # Review starts Minecraft without running the configured model. Preserve that
    # profile's placeholder separately; never label it as the actual external LLM.
    result = dict(run)
    result["world_profile_model_parameters"] = run.get("model_parameters", {})
    result["model_parameters"] = read_json(MODEL_METADATA) if MODEL_METADATA.exists() else {}
    result["model_parameters_source"] = (
        "trusted_external_agent_host" if MODEL_METADATA.exists() else "external_agent_unreported")
    return result


def start():
    global PROCESS
    with LOCK:
        if PROCESS is None:
            log = ROOT / "harbor-startup.log"
            with log.open("wb") as output:
                PROCESS = subprocess.Popen([
                    "uv", "run", "--frozen", "python", "scripts/eval/run-navigation-benchmark.py",
                    "--mode", "review", "--task", TASK_ID, "--run-id", "harbor",
                    "--allow-unverified-runtime",
                ], cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 600
        positions = RESULT.parent / "positions.jsonl"
        while time.monotonic() < deadline:
            if RESULT.exists():
                result = read_json(RESULT)
                if result.get("infrastructure_error"):
                    log = RUN / "logs/action-cli-readback.log"
                    detail = log.read_text()[-4000:] if log.exists() else result["terminal_reason"]
                    raise RuntimeError("Runtime startup check failed: " + detail)
                return result
            if PROCESS.poll() is not None:
                raise RuntimeError("Minecraft startup failed; inspect world:/workspace/mcbots/harbor-startup.log")
            if positions.exists() and positions.stat().st_size:
                first = json.loads(positions.read_text().splitlines()[0])
                if "state_error" not in first:
                    return {"ready": True, "task_id": TASK_ID}
                raise RuntimeError("Initial state sampling failed")
            time.sleep(0.5)
        raise TimeoutError("Minecraft startup exceeded 600 seconds")


def remote(command, timeout=30):
    run = read_json(RUN / "control/run.json")
    request = urllib.request.Request(
        f"http://127.0.0.1:{run['ports']['remote_bash']}/exec",
        data=json.dumps({"command": command, "timeout": timeout}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout + 10) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read(16384).decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
        except ValueError:
            detail = {"error": body}
        return {"exit_code": 1, "http_status": error.code, "error": detail}



def claim(claim_id=None):
    claim_id = claim_id or uuid.uuid4().hex
    if not isinstance(claim_id, str) or not re.fullmatch(r"[a-f0-9]{32}", claim_id):
        raise ValueError("Invalid claim ID")
    request = RUN / "control/claim-requests" / (claim_id + ".request.json")
    atomic_json(request, {"schema_version": 1, "claim_id": claim_id,
                         "action": "claim_done", "issued_at_unix_sec": time.time()})
    response = RUN / "control/claim-responses" / (claim_id + ".response.json")
    for _ in range(100):
        if response.exists():
            return read_json(response)
        if RESULT.exists():
            return read_json(RESULT)
        time.sleep(0.1)
    raise TimeoutError("Completion monitor did not answer the claim")


def finish():
    if RESULT.exists():
        return read_json(RESULT)
    if PROCESS is None:
        return {"task_id": TASK_ID, "terminal": True, "success": False,
                "infrastructure_error": False, "terminal_reason": "harbor_agent_never_started"}
    control = RUN / "control"
    if not control.exists():
        raise RuntimeError("Runtime did not finish materialization")
    atomic_json(control / "terminal-request.json", {"reason": "harbor_agent_finished"})
    for _ in range(300):
        if RESULT.exists():
            return read_json(RESULT)
        time.sleep(0.1)
    raise TimeoutError("Completion monitor did not finalize")


def remote_rpc(payload):
    method = payload.get("method")
    path = payload.get("path")
    data = payload.get("data")
    allowed = (method == "POST" and path in {"/exec", "/exec_async"}) or (
        isinstance(path, str) and (
            (method == "GET" and re.fullmatch(r"/task/[a-f0-9-]{36}", path)) or
            (method == "POST" and re.fullmatch(r"/task/[a-f0-9-]{36}/stop", path))))
    if not allowed:
        raise ValueError("Remote Bash route not allowed")
    if data is not None and not isinstance(data, dict):
        raise ValueError("RPC data must be an object")
    if path in {"/exec", "/exec_async"}:
        if not data or not isinstance(data.get("command"), str) or not 0 < len(data["command"]) <= 65536:
            raise ValueError("Invalid command")
        timeout = float(data.get("timeout", 300))
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("Invalid timeout")
    else:
        timeout = 30
    run = read_json(RUN / "control/run.json")
    request = urllib.request.Request(
        f"http://127.0.0.1:{run['ports']['remote_bash']}{path}", method=method,
        data=None if data is None else json.dumps(data).encode(),
        headers={"Content-Type": "application/json"})
    try:
        response = urllib.request.urlopen(request, timeout=timeout + 10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return {"status": response.code, "body": json.load(response)}


def session():
    start()
    run = read_json(RUN / "control/run.json")
    config = read_json(RUN / "client/.mcbots_runtime.json")
    return {"task_id": run["task_id"], "prompt": run["task"]["prompt"],
            "agent": run["agent"], "limits": run["limits"],
            "display": config["x11"]["display"],
            "record_video": run["record_video"], "eval_setting": run["eval_setting"],
            "runtime_readback": read_json(RESULT.parent / "runtime-readback.json")}


def events(payload):
    offset = payload.get("offset", 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("Invalid event offset")
    path = RUN / "control/evaluator-events.jsonl"
    if not path.exists():
        return {"offset": 0, "events": []}
    lines = path.read_text().splitlines()
    if offset > len(lines):
        raise ValueError("Invalid event offset")
    # Forward exactly the public messages consumed by NavigationClaimClient.
    return {"offset": len(lines), "events": [
        {"message": json.loads(line)["message"]} for line in lines[offset:]]}


def dispatch(action, payload):
    if action == "session":
        return session()
    if action == "events":
        return events(payload)
    if action == "rpc":
        return remote_rpc(payload)
    if action == "ping":
        return {"ok": True}
    if action == "finish":
        return finish()
    if action == "result":
        return read_json(RESULT) if RESULT.exists() else {"terminal": False}
    if action not in {"start", "state", "exec", "screenshot", "claim-done", "read-image"}:
        raise ValueError("Unknown action")
    ready = start()
    if action == "start":
        return ready
    if ready.get("terminal"):
        raise RuntimeError("Task has ended; use nav result")
    if action == "state":
        return remote("mcapi state")
    if action == "exec":
        command = payload.get("command")
        timeout = float(payload.get("timeout", 30))
        if not isinstance(command, str) or not command or len(command) > 65536:
            raise ValueError("command must contain 1–65536 characters")
        if not math.isfinite(timeout) or not 0 < timeout <= MAX_EXEC_TIMEOUT_SEC:
            raise ValueError("timeout must be greater than 0 and at most 120 seconds")
        return remote(command, timeout)
    if action == "read-image":
        path = payload.get("path")
        if not isinstance(path, str) or len(path) > 4096:
            raise ValueError("Expected an image path in /workspace or /tmp")
        # Resolve and read inside the same bubblewrap namespace as actions.
        # Never resolve an agent-controlled path against the world filesystem.
        script = r"""
import base64, os, stat, sys
from pathlib import Path
p = Path(sys.argv[1]).resolve()
if not any(p.is_relative_to(root) for root in (Path('/workspace'), Path('/tmp'))):
    raise ValueError('Image must be in /workspace or /tmp')
fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
with os.fdopen(fd, 'rb') as f:
    if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
        raise ValueError('Expected a regular file')
    body = f.read(8 * 1024 * 1024 + 1)
if len(body) > 8 * 1024 * 1024:
    raise ValueError('Image exceeds 8 MiB')
if not (body.startswith(b'\x89PNG\r\n\x1a\n') or body.startswith(b'\xff\xd8\xff')):
    raise ValueError('Expected PNG or JPEG')
print(base64.b64encode(body).decode())
"""
        result = remote("python3 -c " + shlex.quote(script) + " " + shlex.quote(path))
        if result.get("exit_code") != 0:
            raise ValueError("Image read failed: " + json.dumps(result)[:16384])
        return base64.b64decode(result["stdout"].strip(), validate=True)
    if action == "claim-done":
        with LOCK:
            return claim(payload.get("claim_id"))
    config = read_json(RUN / "client/.mcbots_runtime.json")
    frame = subprocess.run(["xwd", "-display", config["x11"]["display"], "-root", "-silent"],
                           check=True, capture_output=True, timeout=15)
    return subprocess.run(["convert", "xwd:-", "png:-"], input=frame.stdout,
                          check=True, capture_output=True, timeout=15).stdout


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 131072:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            result = dispatch(self.path.lstrip("/"), payload)
            body = result if isinstance(result, bytes) else json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", ("image/jpeg" if result.startswith(b"\xff\xd8\xff") else "image/png") if isinstance(result, bytes) else "application/json")
        except Exception as error:
            body = json.dumps({"error": str(error)}).encode()
            self.send_response(400)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def shutdown(_signum, _frame):
    if PROCESS is not None and PROCESS.poll() is None:
        os.killpg(PROCESS.pid, signal.SIGTERM)
    raise SystemExit(0)


if __name__ == "__main__":
    if "--model-metadata" in sys.argv:
        # Host-only service_exec path; deliberately absent from dispatch().
        write_model_metadata(json.loads(sys.argv[sys.argv.index("--model-metadata") + 1]))
        raise SystemExit(0)
    if "--agent-terminal" in sys.argv:
        reason = sys.argv[sys.argv.index("--agent-terminal") + 1]
        if reason not in {"step_limit", "llm_failure_limit", "agent_crash", "agent_stopped_without_completion"}:
            raise ValueError("Invalid agent termination reason")
        # Host-only service_exec command; never expose this on the agent socket.
        if not RESULT.exists():
            atomic_json(RUN / "control/terminal-request.json", {"reason": reason})
            for _ in range(100):
                if RESULT.exists():
                    break
                time.sleep(0.1)
            else:
                raise TimeoutError("Monitor did not record host termination")
        raise SystemExit(0)
    if "--collect" in sys.argv:
        connection = http.client.HTTPConnection("localhost", timeout=45)
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.sock.settimeout(45)
        connection.sock.connect(str(SOCKET))
        connection.request("POST", "/finish", "{}", {"Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError("Completion collection failed")
        completion = json.loads(response.read())
        target = Path("/trusted/completion.json")
        target.parent.mkdir(exist_ok=True)
        atomic_json(target, completion)
        if RESULT.parent.exists():
            shutil.copytree(RESULT.parent, target.parent / "navigation", dirs_exist_ok=True)
        for name in ("run.json", "evaluator-events.jsonl", "terminal-request.json"):
            source = RUN / "control" / name
            if source.is_file():
                shutil.copy2(source, target.parent / name)
        for run_path in (target.parent / "run.json", target.parent / "navigation/run.json"):
            if run_path.exists():
                atomic_json(run_path, collected_run_metadata(read_json(run_path)))
        (target.parent / "evaluator-events.jsonl").touch(exist_ok=True)
        connection.close()
        raise SystemExit(0)
    if "--healthcheck" in sys.argv:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(str(SOCKET))
        raise SystemExit(0)
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    SOCKET.unlink(missing_ok=True)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    with Server(str(SOCKET), Handler) as server:
        SOCKET.chmod(0o666)
        server.serve_forever()
