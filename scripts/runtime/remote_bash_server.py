#!/usr/bin/env python3
"""
Remote Bash Execution Server

Provides HTTP API for executing bash commands remotely.
This service runs inside the container and allows remote control via bash.

Port: 9090 (default)

Features:
- Synchronous execution: POST /exec
- Asynchronous execution: POST /exec_async (returns task_id)
- Task management: GET /task/{task_id}, POST /task/{task_id}/stop
"""

from flask import Flask, request, jsonify
import atexit
import ipaddress
import json
import subprocess
import os
import signal
import shutil
import socket
import sys
import uuid
import threading
import time
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Optional
from enum import Enum

app = Flask(__name__)

# Ensure DISPLAY is set for X11 applications
if 'DISPLAY' not in os.environ:
    os.environ['DISPLAY'] = ':0'

# Set working directory (default /workspace, fallback to current dir for local mode)
WORKSPACE_ROOT = os.environ.get('REMOTE_BASH_WORKDIR', '/workspace')
if not os.path.isdir(WORKSPACE_ROOT):
    WORKSPACE_ROOT = os.getcwd()
WORKSPACE_ROOT = os.path.abspath(WORKSPACE_ROOT)
os.chdir(WORKSPACE_ROOT)

SANDBOX_ENABLED = os.environ.get('REMOTE_BASH_SANDBOX', '').strip().lower() == 'true'
SANDBOX_DISABLE_NETWORK = (
    os.environ.get('REMOTE_BASH_SANDBOX_DISABLE_NETWORK', '').strip().lower()
    == 'true'
)
SANDBOX_ACTION_BIN = os.path.abspath(
    os.environ.get('REMOTE_BASH_SANDBOX_ACTION_BIN', '/opt/mcbots-action-bin')
)
SANDBOX_RUNTIME_CONFIG = os.path.abspath(
    os.environ.get('REMOTE_BASH_SANDBOX_RUNTIME_CONFIG', '/run/mcbots/runtime.json')
)

# One private tmp directory per Remote Bash process/trial, reused by actions.
_SANDBOX_TMP = tempfile.TemporaryDirectory(prefix='mcbots-action-tmp-') if SANDBOX_ENABLED else None

def sandbox_tmp_dir():
    global _SANDBOX_TMP
    if _SANDBOX_TMP is None:
        _SANDBOX_TMP = tempfile.TemporaryDirectory(prefix='mcbots-action-tmp-')
    return _SANDBOX_TMP.name

_BWRAP_SUPERVISOR = r"""
import subprocess
import sys

process = subprocess.Popen(sys.argv[1:])
raise SystemExit(process.wait())
"""

_AGENTBRIDGE_RELAY_HOST_PATH = f'/tmp/mcbots-agentbridge-relay-{os.getpid()}.sock'
_AGENTBRIDGE_RELAY_SANDBOX_PATH = '/run/mcbots/agentbridge-relay.sock'
_AGENTBRIDGE_RELAY_MAX_REQUEST_BYTES = 64 * 1024
_AGENTBRIDGE_RELAY_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_AGENTBRIDGE_EXACT_PATHS = {
    '/api/health',
    '/api/state',
    '/api/look',
    '/api/right_click_block',
    '/api/window_click',
    '/api/close_gui',
}
_agentbridge_relay_lock = threading.Lock()
_agentbridge_relay_socket: Optional[socket.socket] = None
_agentbridge_relay_stop = threading.Event()
_input_lease_lock = threading.RLock()
_input_owners: dict[str, str] = {}
_live_input_tasks: set[str] = set()
_input_cleanup_pending: set[str] = set()
_agentbridge_base_url: Optional[str] = None


def _bridge_request(base_url, endpoint, params=None, timeout=8.0):
    query = urllib.parse.urlencode(params or {})
    url = f'{base_url}{endpoint}' + (f'?{query}' if query else '')
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body = response.read(_AGENTBRIDGE_RELAY_MAX_RESPONSE_BYTES + 1)
    if len(body) > _AGENTBRIDGE_RELAY_MAX_RESPONSE_BYTES:
        raise ValueError('AgentBridge response exceeds size limit')
    decoded = json.loads(body.decode('utf-8'))
    if not isinstance(decoded, dict):
        raise ValueError('AgentBridge response must be a JSON object')
    return decoded


def _leased_bridge_request(base_url, endpoint, params, timeout, task_id):
    if not endpoint.startswith('/api/input/'):
        return _bridge_request(base_url, endpoint, params, timeout)
    parts = endpoint.split('/')
    if len(parts) != 5 or parts[4] not in ('true', 'false'):
        raise ValueError('Invalid input endpoint')
    key, pressed = parts[3], parts[4] == 'true'
    with _input_lease_lock:
        if not task_id or task_id not in _live_input_tasks:
            raise ValueError('Input requires a live action lease')
        if _input_cleanup_pending:
            raise RuntimeError('Previous action input cleanup is incomplete')
        owner = _input_owners.get(key)
        if pressed and owner is not None and owner != task_id:
            raise RuntimeError('Input is still owned by another action')
        if not pressed and owner != task_id:
            return {'success': True, 'message': 'Stale release ignored'}
        if pressed:
            # Record BEFORE sending: a timed-out request may still reach the client.
            _input_owners[key] = task_id
        result = _bridge_request(base_url, endpoint, params, timeout)
        if not pressed and result.get('success') is True:
            _input_owners.pop(key, None)
        return result


def _retire_input_lease(task_id):
    """Outside the sandbox: revoke first, then release only this action's keys."""
    with _input_lease_lock:
        _live_input_tasks.discard(task_id)
        keys = [key for key, owner in _input_owners.items() if owner == task_id]
        if not keys:
            _input_cleanup_pending.discard(task_id)
            return
        _input_cleanup_pending.add(task_id)
        for key in keys:
            result = _bridge_request(_agentbridge_base_url, f'/api/input/{key}/false')
            if result.get('success') is not True:
                raise RuntimeError(f'Input cleanup failed for {key}')
            _input_owners.pop(key, None)
        _input_cleanup_pending.discard(task_id)


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == 'localhost':
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_allowed_agentbridge_endpoint(endpoint: str) -> bool:
    """Allow only the local AgentBridge operations exposed by ``mcapi``."""
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.query:
        return False
    path = parsed.path
    return path in _AGENTBRIDGE_EXACT_PATHS or path.startswith('/api/input/')


def _read_relay_message(connection: socket.socket) -> dict:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = connection.recv(min(65536, _AGENTBRIDGE_RELAY_MAX_REQUEST_BYTES - size + 1))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > _AGENTBRIDGE_RELAY_MAX_REQUEST_BYTES:
            raise ValueError('relay request exceeds size limit')
        if b'\n' in chunk:
            break
    raw = b''.join(chunks).split(b'\n', 1)[0]
    payload = json.loads(raw.decode('utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('relay request must be a JSON object')
    return payload


def _send_relay_message(connection: socket.socket, payload: dict) -> None:
    connection.sendall(
        json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        + b'\n'
    )


def _handle_agentbridge_relay_connection(
    connection: socket.socket,
    agentbridge_base_url: str,
) -> None:
    try:
        with connection:
            request_payload = _read_relay_message(connection)
            endpoint = request_payload.get('endpoint')
            params = request_payload.get('params')
            timeout = request_payload.get('timeout', 8.0)
            if not isinstance(endpoint, str) or not _is_allowed_agentbridge_endpoint(endpoint):
                raise ValueError('AgentBridge relay endpoint is not allowed')
            if params is not None and not isinstance(params, dict):
                raise ValueError('AgentBridge relay params must be an object or null')
            try:
                timeout_value = min(30.0, max(0.1, float(timeout)))
            except (TypeError, ValueError) as exc:
                raise ValueError('AgentBridge relay timeout is invalid') from exc
            decoded = _leased_bridge_request(agentbridge_base_url, endpoint, params,
                    timeout_value, request_payload.get('action_lease'))
            _send_relay_message(connection, {'ok': True, 'body': decoded})
    except Exception as exc:
        try:
            _send_relay_message(
                connection,
                {'ok': False, 'error': f'{type(exc).__name__}: {exc}'},
            )
        except OSError:
            pass


def _serve_agentbridge_relay(
    listener: socket.socket,
    agentbridge_base_url: str,
) -> None:
    listener.settimeout(0.5)
    while not _agentbridge_relay_stop.is_set():
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(
            target=_handle_agentbridge_relay_connection,
            args=(connection, agentbridge_base_url),
            daemon=True,
        ).start()


def _ensure_agentbridge_relay() -> str:
    """Start a Unix-socket-only relay from the netless sandbox to AgentBridge."""
    global _agentbridge_relay_socket, _agentbridge_base_url
    with _agentbridge_relay_lock:
        if _agentbridge_relay_socket is not None:
            return _AGENTBRIDGE_RELAY_SANDBOX_PATH
        with open(SANDBOX_RUNTIME_CONFIG, encoding='utf-8') as handle:
            runtime_config = json.load(handle)
        endpoint = runtime_config.get('agentbridge')
        if not isinstance(endpoint, dict):
            raise RuntimeError('Missing agentbridge endpoint in sandbox runtime config')
        host = endpoint.get('host')
        port = endpoint.get('port')
        if not isinstance(host, str) or not _is_loopback_host(host):
            raise RuntimeError('AgentBridge relay requires a loopback-only host')
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise RuntimeError('AgentBridge relay port is invalid')

        relay_path = _AGENTBRIDGE_RELAY_HOST_PATH
        try:
            os.unlink(relay_path)
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(relay_path)
        os.chmod(relay_path, 0o600)
        listener.listen(16)
        _agentbridge_relay_socket = listener
        _agentbridge_base_url = f'http://{host}:{port}'
        threading.Thread(
            target=_serve_agentbridge_relay,
            args=(listener, f'http://{host}:{port}'),
            daemon=True,
        ).start()
        return _AGENTBRIDGE_RELAY_SANDBOX_PATH


def _close_agentbridge_relay() -> None:
    global _agentbridge_relay_socket
    _agentbridge_relay_stop.set()
    listener = _agentbridge_relay_socket
    _agentbridge_relay_socket = None
    if listener is not None:
        listener.close()
    relay_path = _AGENTBRIDGE_RELAY_HOST_PATH
    try:
        os.unlink(relay_path)
    except FileNotFoundError:
        pass


atexit.register(_close_agentbridge_relay)


def build_execution(
    command: str,
    working_dir: str,
    *,
    die_with_parent: bool = True,
) -> tuple[list[str], Optional[str], dict]:
    """Build a direct Bash or Bubblewrap-isolated command invocation."""
    env = os.environ.copy()
    if not SANDBOX_ENABLED:
        return ['bash', '-c', command], working_dir, env

    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise RuntimeError('REMOTE_BASH_SANDBOX=true but bwrap is not installed')
    if not os.path.isdir(SANDBOX_ACTION_BIN):
        raise RuntimeError(f'Missing sandbox action bin: {SANDBOX_ACTION_BIN}')
    if not os.path.isfile(SANDBOX_RUNTIME_CONFIG):
        raise RuntimeError(f'Missing sandbox runtime config: {SANDBOX_RUNTIME_CONFIG}')

    relative_working_dir = os.path.relpath(working_dir, WORKSPACE_ROOT)
    sandbox_working_dir = (
        '/workspace'
        if relative_working_dir == '.'
        else f"/workspace/{relative_working_dir}"
    )
    display = os.environ.get('DISPLAY', ':0')
    argv = [bwrap]
    if die_with_parent:
        argv.append('--die-with-parent')
    if SANDBOX_DISABLE_NETWORK:
        relay_socket = _ensure_agentbridge_relay()
        argv.append('--unshare-net')
    argv.extend([
        '--new-session',
        '--cap-drop', 'ALL',
        '--unshare-pid',
        '--unshare-ipc',
        '--unshare-uts',
        '--proc', '/proc',
        '--dev', '/dev',
        '--ro-bind', '/usr', '/usr',
        '--symlink', 'usr/bin', '/bin',
        '--symlink', 'usr/lib', '/lib',
        '--symlink', 'usr/lib64', '/lib64',
        '--symlink', 'usr/sbin', '/sbin',
        '--ro-bind', '/etc', '/etc',
        '--dir', '/opt',
        '--ro-bind', '/opt/mcbots-venv', '/opt/mcbots-venv',
        '--ro-bind', SANDBOX_ACTION_BIN, '/opt/mcbots-action-bin',
        '--dir', '/run',
        '--dir', '/run/mcbots',
        '--ro-bind', SANDBOX_RUNTIME_CONFIG, '/run/mcbots/runtime.json',
        '--bind', sandbox_tmp_dir(), '/tmp',
        '--ro-bind', '/tmp/.X11-unix', '/tmp/.X11-unix',
        '--bind', WORKSPACE_ROOT, '/workspace',
        '--chdir', sandbox_working_dir,
        '--clearenv',
        '--setenv', 'HOME', '/workspace',
        '--setenv', 'PATH', '/opt/mcbots-action-bin:/opt/mcbots-venv/bin:/usr/local/bin:/usr/bin:/bin',
        '--setenv', 'PYTHONPATH', '/opt/mcbots-action-bin',
        '--setenv', 'MCBOTS_RUNTIME_CONFIG', '/run/mcbots/runtime.json',
        '--setenv', 'MCBOTS_WORKSPACE_ROOT', '/workspace',
        '--setenv', 'DISPLAY', display,
        '--setenv', 'LANG', os.environ.get('LANG', 'C.UTF-8'),
    ])
    if SANDBOX_DISABLE_NETWORK:
        argv.extend([
            '--ro-bind', _AGENTBRIDGE_RELAY_HOST_PATH, relay_socket,
            '--setenv', 'MCBOTS_AGENTBRIDGE_RELAY_SOCKET', relay_socket,
        ])
    argv.extend(['/bin/bash', '-c', command])
    return argv, None, env


def build_async_execution(
    command: str,
    working_dir: str,
    action_lease: Optional[str] = None,
) -> tuple[list[str], Optional[str], dict]:
    """Build an async invocation whose full sandbox dies with its supervisor.

    Bubblewrap may create a process/session that no longer belongs to the
    initially launched process group.  A dedicated supervisor remains its
    parent for the complete action lifetime, allowing ``--die-with-parent`` to
    clean up the namespace when stop_task terminates the supervisor.
    """
    argv, process_cwd, process_env = build_execution(
        command,
        working_dir,
        die_with_parent=SANDBOX_ENABLED,
    )
    if SANDBOX_ENABLED:
        if action_lease:
            argv[-3:-3] = ['--setenv', 'MCBOTS_ACTION_LEASE', action_lease]
        argv = [sys.executable, '-c', _BWRAP_SUPERVISOR, *argv]
    if action_lease:
        process_env['MCBOTS_ACTION_LEASE'] = action_lease
    return argv, process_cwd, process_env


def is_path_within_workspace(path: str) -> bool:
    abs_path = os.path.abspath(path)
    return abs_path == WORKSPACE_ROOT or abs_path.startswith(f"{WORKSPACE_ROOT}{os.sep}")


def resolve_working_dir(raw_working_dir: Optional[str]) -> tuple[Optional[str], str]:
    """
    Resolve and validate working directory.

    - Empty -> WORKSPACE_ROOT
    - Relative path -> resolved under WORKSPACE_ROOT
    - Absolute path -> must still be inside WORKSPACE_ROOT
    """
    if raw_working_dir is None or not str(raw_working_dir).strip():
        return WORKSPACE_ROOT, ""

    candidate = str(raw_working_dir).strip()
    if SANDBOX_ENABLED and (candidate == '/workspace' or candidate.startswith('/workspace/')):
        candidate = os.path.join(WORKSPACE_ROOT, candidate.removeprefix('/workspace').lstrip('/'))
    if os.path.isabs(candidate):
        resolved = os.path.abspath(candidate)
    else:
        resolved = os.path.abspath(os.path.join(WORKSPACE_ROOT, candidate))

    if not os.path.isdir(resolved):
        return None, f"Invalid working_dir: '{candidate}' does not exist or is not a directory"
    if not is_path_within_workspace(resolved):
        return None, (
            f"Permission denied: working_dir '{candidate}' resolves to '{resolved}', "
            f"which is outside workspace root '{WORKSPACE_ROOT}'"
        )
    return resolved, ""


def check_cd_command(command: str, current_dir: str = WORKSPACE_ROOT) -> tuple[bool, str]:
    """
    检查命令中的 cd 是否会离开 WORKSPACE_ROOT

    Returns:
        (is_safe, error_message): 如果安全返回 (True, "")，否则返回 (False, "错误信息")
    """
    # Bubblewrap, not a shell regex, enforces the filesystem boundary.
    if SANDBOX_ENABLED:
        return True, ""
    import re
    import os.path

    # 简单检测：查找 cd 命令（支持 cd xxx、cd "xxx"、cd 'xxx'）
    cd_pattern = r'\bcd\s+([^\s;&|]+)'
    matches = re.findall(cd_pattern, command)

    if not matches:
        return True, ""  # 没有 cd 命令，安全

    # 检查每个 cd 目标
    for target in matches:
        # 去除引号
        target = target.strip('"\'')

        # 计算绝对路径
        if target.startswith('/'):
            # 绝对路径
            abs_path = os.path.abspath(target)
        else:
            # 相对路径，基于当前目录
            abs_path = os.path.abspath(os.path.join(current_dir, target))

        # 规范化路径（解析 ..、. 等）
        abs_path = os.path.normpath(abs_path)

        # 检查是否在 WORKSPACE_ROOT 内
        if not (abs_path == WORKSPACE_ROOT or abs_path.startswith(f"{WORKSPACE_ROOT}/")):
            return False, (
                f"Permission denied: Cannot cd to '{target}' (resolves to '{abs_path}'). "
                f"Agent must stay in '{WORKSPACE_ROOT}'."
            )

    return True, ""


class TaskStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class Task:
    task_id: str
    command: str
    process: subprocess.Popen
    status: TaskStatus
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    start_time: float = field(default_factory=time.time)


# Task storage
tasks: Dict[str, Task] = {}
tasks_lock = threading.Lock()
DEFAULT_EXEC_TIMEOUT_SEC = 30.0
MAX_EXEC_TIMEOUT_SEC = 120.0


def normalize_timeout(raw_timeout, default: float = DEFAULT_EXEC_TIMEOUT_SEC) -> float:
    """Parse and clamp timeout to keep /exec predictable."""
    try:
        timeout = float(raw_timeout)
    except Exception:
        return float(default)
    if timeout <= 0:
        return float(default)
    if timeout > MAX_EXEC_TIMEOUT_SEC:
        return MAX_EXEC_TIMEOUT_SEC
    return timeout


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    with tasks_lock:
        running_tasks = sum(1 for t in tasks.values() if t.status == TaskStatus.RUNNING)

    return jsonify({
        "success": True,
        "status": "healthy",
        "service": "remote-bash-server",
        "display": os.environ.get('DISPLAY', 'not set'),
        "working_dir": os.getcwd(),
        "active_tasks": running_tasks,
        "total_tasks": len(tasks)
    })


def monitor_task(task: Task):
    """Background thread to monitor task completion"""
    try:
        stdout, stderr = task.process.communicate()
        _retire_input_lease(task.task_id)

        with tasks_lock:
            task.stdout = stdout.decode() if stdout else ""
            task.stderr = stderr.decode() if stderr else ""
            if task.status != TaskStatus.STOPPED:
                task.exit_code = task.process.returncode
                task.status = TaskStatus.COMPLETED if task.exit_code == 0 else TaskStatus.FAILED

    except Exception as e:
        with tasks_lock:
            task.stderr = f"Monitor error: {str(e)}"
            task.exit_code = -1
            task.status = TaskStatus.FAILED


def terminate_process_group(process: subprocess.Popen, grace_sec: float = 2.0) -> None:
    """Terminate the complete process group, escalating to SIGKILL if needed."""
    if process.poll() is not None:
        return
    try:
        process_group = os.getpgid(process.pid)
    except ProcessLookupError:
        return

    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_sec)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=5)


@app.route('/exec', methods=['POST'])
def exec_command():
    """
    Execute a bash command synchronously (blocks until completion)

    Body (JSON):
    {
        "command": "ls -la",           // Required: bash command to execute
        "timeout": 30,                 // Optional: timeout in seconds (default: 30)
        "working_dir": "/app"          // Optional: working directory (default: current)
    }

    Returns:
    {
        "success": true,
        "stdout": "...",
        "stderr": "...",
        "exit_code": 0
    }
    """
    data = request.json

    if not data or 'command' not in data:
        return jsonify({
            "success": False,
            "error": "Missing 'command' parameter"
        }), 400

    command = data['command']
    timeout = normalize_timeout(data.get('timeout', DEFAULT_EXEC_TIMEOUT_SEC))
    requested_working_dir = data.get('working_dir', None)
    working_dir, err = resolve_working_dir(requested_working_dir)
    if err:
        return jsonify({
            "success": False,
            "error": err
        }), 403

    # 检查 cd 命令安全性
    is_safe, error_msg = check_cd_command(command, working_dir)
    if not is_safe:
        return jsonify({
            "success": False,
            "error": error_msg
        }), 403

    action_lease = str(uuid.uuid4())
    try:
        # Sync calls use the same external input cleanup as async calls.
        argv, process_cwd, process_env = build_async_execution(command, working_dir, action_lease)
        with _input_lease_lock:
            _live_input_tasks.add(action_lease)
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=float(timeout),
            cwd=process_cwd,
            env=process_env
        )

        return jsonify({
            "success": True,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode
        })

    except subprocess.TimeoutExpired as e:
        cmd_preview = " ".join(command.strip().split())
        if len(cmd_preview) > 200:
            cmd_preview = cmd_preview[:200] + "...(truncated)"
        stdout_tail = ((e.stdout or "")[-200:]).strip()
        stderr_tail = ((e.stderr or "")[-200:]).strip()
        app.logger.warning(
            "exec timeout: timeout=%.2fs cwd=%s cmd=%s",
            float(timeout),
            working_dir,
            cmd_preview,
        )
        return jsonify({
            "success": False,
            "error": f"Command timeout after {timeout:.2f} seconds",
            "error_code": "timeout",
            "command_preview": cmd_preview,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
        }), 500
    except Exception as e:
        cmd_preview = " ".join(command.strip().split())
        if len(cmd_preview) > 200:
            cmd_preview = cmd_preview[:200] + "...(truncated)"
        app.logger.exception("exec exception: cwd=%s cmd=%s", working_dir, cmd_preview)
        return jsonify({
            "success": False,
            "error": str(e),
            "error_code": "exec_exception",
            "command_preview": cmd_preview,
        }), 500
    finally:
        try:
            _retire_input_lease(action_lease)
        except Exception:
            app.logger.exception('Input cleanup failed; further input is blocked')
            return jsonify({'success': False, 'error_code': 'input_cleanup_failed',
                            'error': 'Input cleanup failed; further input is blocked'}), 500


@app.route('/exec_async', methods=['POST'])
def exec_async():
    """
    Execute a bash command asynchronously (returns immediately with task_id)

    Body (JSON):
    {
        "command": "sleep 10 && echo done",   // Required: bash command
        "working_dir": "/workspace"            // Optional: working directory
    }

    Returns:
    {
        "success": true,
        "task_id": "uuid-string",
        "status": "running"
    }
    """
    data = request.json

    if not data or 'command' not in data:
        return jsonify({
            "success": False,
            "error": "Missing 'command' parameter"
        }), 400

    command = data['command']
    requested_working_dir = data.get('working_dir', None)
    working_dir, err = resolve_working_dir(requested_working_dir)
    if err:
        return jsonify({
            "success": False,
            "error": err
        }), 403
    task_id = str(uuid.uuid4())

    # 检查 cd 命令安全性
    is_safe, error_msg = check_cd_command(command, working_dir)
    if not is_safe:
        return jsonify({
            "success": False,
            "error": error_msg
        }), 403

    try:
        # Start process in background
        argv, process_cwd, process_env = build_async_execution(command, working_dir, task_id)
        with _input_lease_lock:
            _live_input_tasks.add(task_id)
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=process_cwd,
            env=process_env,
            start_new_session=True,
        )

        # Create task
        task = Task(
            task_id=task_id,
            command=command,
            process=process,
            status=TaskStatus.RUNNING
        )

        with tasks_lock:
            tasks[task_id] = task

        # Start monitoring thread
        monitor_thread = threading.Thread(target=monitor_task, args=(task,), daemon=True)
        monitor_thread.start()

        return jsonify({
            "success": True,
            "task_id": task_id,
            "status": TaskStatus.RUNNING
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/task/<task_id>', methods=['GET'])
def get_task(task_id):
    """
    Get task status and output

    Returns:
    {
        "success": true,
        "task_id": "...",
        "status": "running|completed|failed|stopped",
        "command": "...",
        "exit_code": 0,  // null if still running
        "stdout": "...",
        "stderr": "...",
        "elapsed_time": 12.5
    }
    """
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        return jsonify({
            "success": False,
            "error": f"Task {task_id} not found"
        }), 404

    elapsed = time.time() - task.start_time

    return jsonify({
        "success": True,
        "task_id": task.task_id,
        "status": task.status,
        "command": task.command,
        "exit_code": task.exit_code,
        "stdout": task.stdout,
        "stderr": task.stderr,
        "elapsed_time": round(elapsed, 2)
    })


@app.route('/task/<task_id>/stop', methods=['POST'])
def stop_task(task_id):
    """
    Stop a running task

    Returns:
    {
        "success": true,
        "message": "Task stopped"
    }
    """
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        return jsonify({
            "success": False,
            "error": f"Task {task_id} not found"
        }), 404

    if task.status != TaskStatus.RUNNING:
        with _input_lease_lock:
            cleanup_needed = task_id in _input_cleanup_pending
        if cleanup_needed:
            try:
                _retire_input_lease(task_id)
                return jsonify({'success': True, 'task_id': task_id, 'message': 'Input cleanup retried'})
            except Exception as error:
                return jsonify({'success': False, 'error': str(error)}), 500
        return jsonify({
            "success": False,
            "error": f"Task is not running (status: {task.status})"
        }), 400

    try:
        with tasks_lock:
            task.status = TaskStatus.STOPPED
            task.exit_code = -signal.SIGTERM

        terminate_process_group(task.process)
        _retire_input_lease(task_id)

        return jsonify({
            "success": True,
            "message": "Task stopped",
            "task_id": task_id
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Failed to stop task: {str(e)}"
        }), 500


if __name__ == '__main__':
    import sys

    port = 9090
    if len(sys.argv) > 1:
        port = int(sys.argv[1])

    print(f"Starting Remote Bash Execution Server on port {port}")
    print(f"DISPLAY: {os.environ.get('DISPLAY', 'not set')}")
    print(f"Working directory: {os.getcwd()}")
    print("\nEndpoints:")
    print("  GET  /health")
    print("  POST /exec              - Synchronous execution")
    print("  POST /exec_async        - Asynchronous execution (returns task_id)")
    print("  GET  /task/<task_id>    - Get task status")
    print("  POST /task/<task_id>/stop - Stop task")
    print()
    print("⚠️  WARNING: This service allows arbitrary command execution.")
    print("   Only use in trusted environments.")
    print()

    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
