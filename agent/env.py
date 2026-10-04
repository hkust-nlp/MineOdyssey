"""环境端：独立维护state + 后台执行动作"""
import json
import os
import subprocess
import tempfile
import threading
import time
import re
import base64
from pathlib import Path
from typing import Any, Dict, Optional, Literal, List
from queue import Empty, Queue
from collections import deque
from dataclasses import dataclass

try:
    import requests
except ImportError:
    requests = None


@dataclass
class Action:
    """动作指令"""
    type: Literal["exec", "skip", "stop_execute"]
    content: Optional[str] = None
    action_id: Optional[str] = None
    target_action_id: Optional[str] = None


@dataclass
class StateItem:
    """统一的状态项"""
    type: Literal["screenshot", "command_result", "chat_message"]
    timestamp: float

    # 截图数据（type=screenshot时有效）
    screenshot: Optional[bytes] = None

    # 命令结果（type=command_result时有效）
    command: Optional[str] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    exit_code: Optional[int] = None
    command_status: Optional[str] = None
    action_id: Optional[str] = None
    task_id: Optional[str] = None

    # 聊天消息（type=chat_message时有效）
    chat_content: Optional[str] = None
    chat_type: Optional[str] = None  # "player" 或 "system"


class Environment:
    """
    环境端：管理容器，提供截图，执行动作
    
    两个核心线程和一个可选线程：
    1. 截图线程：一直截图（与动作无关）
    2. 动作线程：后台执行动作（支持中断）
    3. 聊天线程：按需监控客户端聊天日志
    """
    
    def __init__(
        self,
        container_name: str,
        screenshot_interval: float = 0.5,
        screenshot_http_timeout: float = 12.0,
        screenshot_cmd_timeout: float = 8.0,
        screenshot_retries: int = 1,
        screenshot_retry_backoff: float = 0.25,
        exec_timeout: float = 60.0,
        state_queue_maxlen: int = 1000,
        chat_check_interval: float = 0.2,
        # Remote Bash API配置（本地和远程统一使用）
        remote_bash_host: str = "localhost",
        remote_bash_port: int = 9090,
        display: str = ":0",
        log_file_path: str = "/app/game/logs/latest.log",
        player_name: str = "Bot1",
        chat_monitor_enabled: bool = True,
        periodic_screenshot_enabled: bool = True,
    ):
        """
        Args:
            container_name: 容器名称
            screenshot_interval: 截图间隔（秒）
            screenshot_http_timeout: 截图 HTTP 请求超时（秒）
            screenshot_cmd_timeout: Remote Bash /exec 内部命令超时（秒）
            screenshot_retries: 单次截图失败后的重试次数（不含首轮）
            screenshot_retry_backoff: 截图重试退避时间（秒）
            exec_timeout: 命令执行超时（秒）
            state_queue_maxlen: 状态队列最大长度
            chat_check_interval: 聊天日志检查间隔（秒，默认0.2秒接近实时）
            remote_bash_host: Remote Bash API 主机地址（本地用localhost，远程用实际IP）
            remote_bash_port: Remote Bash API 端口
            display: 截图使用的 DISPLAY
            log_file_path: 客户端日志路径（Remote Bash 可见路径）
            player_name: 当前 bot 名称（用于全景脚本定位 workspace）
            chat_monitor_enabled: 是否启动聊天日志监控线程
            periodic_screenshot_enabled: 是否启动固定频率截图线程
        """
        self.container_name = container_name
        self.player_name = player_name
        self.screenshot_interval = max(float(screenshot_interval), 0.05)
        self.screenshot_http_timeout = max(float(screenshot_http_timeout), 1.0)
        self.screenshot_cmd_timeout = max(float(screenshot_cmd_timeout), 1.0)
        if self.screenshot_http_timeout <= self.screenshot_cmd_timeout:
            self.screenshot_http_timeout = self.screenshot_cmd_timeout + 1.0
        self.screenshot_retries = max(int(screenshot_retries), 0)
        self.screenshot_retry_backoff = max(float(screenshot_retry_backoff), 0.0)
        self.exec_timeout = exec_timeout
        self.chat_check_interval = chat_check_interval
        self.chat_monitor_enabled = bool(chat_monitor_enabled)
        self.periodic_screenshot_enabled = bool(periodic_screenshot_enabled)
        self.running = False
        self.display = display

        # Remote Bash API配置
        self.remote_bash_host = remote_bash_host
        self.remote_bash_port = remote_bash_port
        self.remote_bash_url = f"http://{remote_bash_host}:{remote_bash_port}"

        # 检查依赖
        if requests is None:
            raise ImportError("Environment requires 'requests' library. Install with: pip install requests")
        self.http = requests.Session()

        # 维护统一的state队列
        self.state_queue = deque(maxlen=state_queue_maxlen)
        self.state_lock = threading.Lock()
        self.state_base_index = 0

        # 动作队列
        self.action_queue = Queue()

        # 当前执行的任务
        self.current_task_id: Optional[str] = None
        self.current_action_id: Optional[str] = None
        self.task_lock = threading.Lock()

        # 聊天日志监控状态
        self.log_line_offset = 0  # 已读取的行数
        self.log_file_path = log_file_path
        # 首次读取时把 offset 对齐到当前文件末尾，避免回放 agent 启动前的历史日志
        self._chat_offset_initialized = False

    def start(self):
        """启动截图、动作及可选的聊天监控线程。"""
        self.running = True

        # 线程1: 一直截图
        screenshot_thread = threading.Thread(
            target=self._screenshot_loop,
            daemon=True
        )

        # 线程2: 执行动作
        action_thread = threading.Thread(
            target=self._action_loop,
            daemon=True
        )

        if self.periodic_screenshot_enabled:
            screenshot_thread.start()
        else:
            print("✓ Periodic screenshot loop disabled; using on-demand capture")
        action_thread.start()
        if self.chat_monitor_enabled:
            chat_thread = threading.Thread(
                target=self._chat_monitor_loop,
                daemon=True
            )
            chat_thread.start()
        else:
            print("✓ Chat log monitor disabled")

        print(f"✓ Environment started for {self.container_name}")

    def stop(self):
        """停止环境"""
        self.running = False
        self._stop_current_task()
        try:
            self.http.close()
        except Exception:
            pass

    def enable_auto_respawn(
        self,
        agentbridge_host: str,
        agentbridge_port: int,
        screen_width: int,
        screen_height: int,
        poll_interval_sec: float = 2.0,
        cooldown_sec: float = 5.0,
    ):
        """启动后台线程：检测玩家死亡（health<=0）并自动点击屏幕中心右键以触发重生。"""
        t = threading.Thread(
            target=self._auto_respawn_loop,
            args=(
                agentbridge_host,
                agentbridge_port,
                int(screen_width),
                int(screen_height),
                float(poll_interval_sec),
                float(cooldown_sec),
            ),
            daemon=True,
        )
        t.start()
        print(
            f"✓ Auto-respawn enabled (center={screen_width//2},{screen_height//2}; "
            f"poll={poll_interval_sec}s, cooldown={cooldown_sec}s)"
        )

    def _auto_respawn_loop(
        self,
        agentbridge_host: str,
        agentbridge_port: int,
        screen_width: int,
        screen_height: int,
        poll_interval_sec: float,
        cooldown_sec: float,
    ):
        state_url = f"http://{agentbridge_host}:{agentbridge_port}/api/state"
        cx = screen_width // 2
        cy = screen_height // 2
        xdotool_cmd = f"DISPLAY={self.display} xdotool mousemove {cx} {cy} click 1"
        last_trigger = 0.0
        while self.running:
            try:
                resp = self.http.get(state_url, timeout=3.0)
                if resp.ok:
                    payload = resp.json() or {}
                    data = payload.get("data") or {}
                    health = data.get("health")
                    if isinstance(health, (int, float)) and health <= 0:
                        now = time.monotonic()
                        if now - last_trigger >= cooldown_sec:
                            print(f"[auto-respawn] health={health}, clicking ({cx},{cy}) to respawn")
                            click_ok = False
                            try:
                                self.http.post(
                                    f"{self.remote_bash_url}/exec",
                                    json={"command": xdotool_cmd, "timeout": 5.0},
                                    timeout=8.0,
                                )
                                click_ok = True
                            except Exception as e:
                                print(f"[auto-respawn] click failed: {e}")
                            notice = (
                                f"[auto-respawn] Player was dead (health={health}); "
                                f"clicked screen center ({cx},{cy}) to trigger respawn."
                                if click_ok
                                else f"[auto-respawn] Player was dead (health={health}); "
                                f"attempted respawn click but it failed; you may need to respawn manually."
                            )
                            self._append_state(StateItem(
                                type="chat_message",
                                timestamp=time.time(),
                                chat_content=notice,
                                chat_type="system",
                            ))
                            last_trigger = now
            except Exception:
                pass
            time.sleep(poll_interval_sec)

    # ===== 截图线程 =====

    def _screenshot_loop(self):
        """独立线程：一直截图（与动作无关）"""
        next_tick = time.monotonic()
        while self.running:
            now = time.monotonic()
            sleep_for = next_tick - now
            if sleep_for > 0:
                time.sleep(min(sleep_for, 0.2))
                continue
            try:
                screenshot = self._capture_screenshot()

                # 添加到state队列
                state = StateItem(
                    type="screenshot",
                    timestamp=time.time(),
                    screenshot=screenshot
                )

                self._append_state(state)
            except Exception as e:
                print(f"⚠️  Screenshot error: {e}")
            finally:
                next_tick += self.screenshot_interval
                now = time.monotonic()
                if next_tick < now:
                    next_tick = now

    def _capture_screenshot(self) -> bytes:
        """获取截图（JPEG 75质量）- 通过 Remote Bash API"""
        cmd = f"DISPLAY={self.display} xwd -root -silent | convert xwd:- -quality 75 jpg:- | base64 | tr -d '\\n'"
        attempts = self.screenshot_retries + 1
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                response = self.http.post(
                    f"{self.remote_bash_url}/exec",
                    json={"command": cmd, "timeout": self.screenshot_cmd_timeout},
                    timeout=self.screenshot_http_timeout,
                )
                if response.status_code >= 400:
                    detail = response.text.strip()
                    try:
                        err_payload = response.json()
                    except Exception:
                        err_payload = {}
                    if isinstance(err_payload, dict):
                        err_main = str(err_payload.get("error") or "").strip()
                        err_code = str(err_payload.get("error_code") or "").strip()
                        cmd_preview = str(err_payload.get("command_preview") or "").strip()
                        stdout_tail = str(err_payload.get("stdout_tail") or "").strip()
                        stderr_tail = str(err_payload.get("stderr_tail") or "").strip()
                        parts = []
                        if err_main:
                            parts.append(err_main)
                        if err_code:
                            parts.append(f"code={err_code}")
                        if cmd_preview:
                            parts.append(f"cmd={cmd_preview}")
                        if stdout_tail:
                            parts.append(f"stdout_tail={stdout_tail}")
                        if stderr_tail:
                            parts.append(f"stderr_tail={stderr_tail}")
                        if parts:
                            detail = " | ".join(parts)
                    if len(detail) > 800:
                        detail = detail[:800] + "...(truncated)"
                    raise RuntimeError(
                        f"Remote /exec HTTP {response.status_code}: {detail or '<empty error body>'}"
                    )
                result = response.json()
                if not result.get("success", True):
                    error_msg = result.get("error") or "Unknown remote exec error"
                    raise RuntimeError(f"Remote /exec failed: {error_msg}")
                exit_code = result.get("exit_code", -1)
                if exit_code != 0:
                    stderr = result.get("stderr") or result.get("error") or "Unknown error"
                    raise RuntimeError(f"Screenshot command failed (exit={exit_code}): {stderr}")

                screenshot_b64 = (result.get("stdout") or "").strip()
                if not screenshot_b64:
                    raise RuntimeError("Screenshot command returned empty stdout")
                return base64.b64decode(screenshot_b64)
            except Exception as e:
                last_error = e
                if attempt < attempts and self.screenshot_retry_backoff > 0:
                    time.sleep(self.screenshot_retry_backoff * attempt)

        raise Exception(f"Screenshot failed after {attempts} attempts: {last_error}")

    def fetch_runtime_state(self) -> Optional[Dict[str, Any]]:
        """Read mcapi state locally when available, with Remote Bash fallback."""
        project_root = Path(
            os.getenv("MCBOTS_PROJECT_ROOT")
            or Path(__file__).resolve().parents[1]
        )
        workspace_root = Path(
            os.getenv("MCBOTS_WORKSPACE_ROOT") or project_root
        )
        runtime_config = os.getenv("MCBOTS_RUNTIME_CONFIG", "").strip()
        mcapi = project_root / "scripts" / "runtime" / "mcapi"
        if mcapi.is_file() and runtime_config and Path(runtime_config).is_file():
            try:
                completed = subprocess.run(
                    [str(mcapi), "state"],
                    cwd=workspace_root if workspace_root.is_dir() else project_root,
                    env=os.environ.copy(),
                    capture_output=True,
                    text=True,
                    timeout=4.0,
                    check=False,
                )
                if completed.returncode == 0:
                    payload = json.loads(completed.stdout or "")
                    data = payload.get("data")
                    if isinstance(data, dict):
                        return data
            except Exception:
                pass

        cmd = "mcapi state"
        try:
            response = self.http.post(
                f"{self.remote_bash_url}/exec",
                json={"command": cmd, "timeout": 3},
                timeout=4.0,
            )
            if response.status_code >= 400:
                return None
            result = response.json()
            if result.get("exit_code", -1) != 0:
                return None
            payload = json.loads(result.get("stdout") or "")
            return payload.get("data") or None
        except Exception:
            return None

    def capture_panorama(
        self,
        lr_yaw: float = 60.0,
        ud_delta: float = 55.0,
        sleep_sec: float = 0.15,
        timeout: float = 15.0,
    ) -> Optional[bytes]:
        """Capture a six-view mosaic locally, with legacy Remote Bash fallback."""
        project_root = Path(
            os.getenv("MCBOTS_PROJECT_ROOT")
            or Path(__file__).resolve().parents[1]
        )
        runtime_config = os.getenv("MCBOTS_RUNTIME_CONFIG", "").strip()
        local_script = (
            project_root
            / "scripts"
            / "analysis"
            / "capture-practical-panorama-inside.sh"
        )
        if (
            local_script.is_file()
            and runtime_config
            and Path(runtime_config).is_file()
        ):
            try:
                with tempfile.TemporaryDirectory(prefix="mcbots-panorama.") as temporary:
                    local_out = Path(temporary) / "panorama.jpg"
                    completed = subprocess.run(
                        [
                            str(local_script),
                            self.player_name,
                            "--lr-yaw",
                            str(lr_yaw),
                            "--ud-delta",
                            str(ud_delta),
                            "--sleep-sec",
                            str(sleep_sec),
                            "--out",
                            str(local_out),
                        ],
                        cwd=project_root,
                        env=os.environ.copy(),
                        capture_output=True,
                        text=True,
                        timeout=max(1.0, float(timeout)),
                        check=False,
                    )
                    if completed.returncode == 0 and local_out.is_file():
                        payload = local_out.read_bytes()
                        if payload:
                            return payload
            except Exception:
                pass

        out_path = "/tmp/mcbots-panorama.jpg"
        script = "/workspace/mcbots/scripts/analysis/capture-practical-panorama-inside.sh"
        cmd = (
            f"{script} {self.player_name} "
            f"--lr-yaw {lr_yaw} --ud-delta {ud_delta} --sleep-sec {sleep_sec} "
            f"--out {out_path} >/dev/null 2>&1 && "
            f"base64 {out_path} | tr -d '\\n'"
        )
        try:
            response = self.http.post(
                f"{self.remote_bash_url}/exec",
                json={"command": cmd, "timeout": max(1, int(timeout) - 1)},
                timeout=timeout,
            )
            if response.status_code >= 400:
                return None
            result = response.json()
            if result.get("exit_code", -1) != 0:
                return None
            b64 = (result.get("stdout") or "").strip()
            if not b64:
                return None
            return base64.b64decode(b64)
        except Exception:
            return None

    def _append_state(self, state: StateItem) -> None:
        with self.state_lock:
            maxlen = self.state_queue.maxlen
            if maxlen is not None and len(self.state_queue) >= maxlen:
                self.state_base_index += 1
            self.state_queue.append(state)

    def capture_screenshot_now(self) -> Optional[StateItem]:
        """立即主动抓取一帧截图并入队，脱离固定截图频率。"""
        try:
            screenshot = self._capture_screenshot()
            state = StateItem(
                type="screenshot",
                timestamp=time.time(),
                screenshot=screenshot,
            )
            self._append_state(state)
            return state
        except Exception as e:
            print(f"⚠️  Immediate screenshot capture failed: {e}")
            return None

    def get_new_states(self, after_index: int) -> List[StateItem]:
        """获取指定索引之后的所有新状态"""
        with self.state_lock:
            queue_list = list(self.state_queue)
            queue_start = self.state_base_index
            queue_end = queue_start + len(queue_list)
            if after_index < 0:
                return queue_list
            elif after_index < queue_start:
                return queue_list
            elif after_index >= queue_end:
                return []
            else:
                return queue_list[after_index - queue_start:]

    # ===== 动作线程 =====

    def _action_loop(self):
        """独立线程：执行动作"""
        while self.running:
            action = None
            try:
                action = self.action_queue.get(timeout=1.0)

                if action.type == "exec":
                    self._execute_command(action.content, action_id=action.action_id)
                elif action.type == "stop_execute":
                    self._stop_current_task(expected_action_id=action.target_action_id)
                elif action.type == "skip":
                    pass  # 什么也不做

            except Empty:
                continue
            except Exception as error:
                print(f"  ⚠️ Action loop error: {error}")
                if action is not None and action.type == "exec":
                    self._append_failed_command_result(
                        action.content or "",
                        f"Action loop error: {error}",
                        action_id=action.action_id,
                    )

    def _append_failed_command_result(
        self,
        command: str,
        error: str,
        *,
        action_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> None:
        """Emit one terminal state so synchronous callers cannot wait forever."""
        self._append_state(
            StateItem(
                type="command_result",
                timestamp=time.time(),
                command=command,
                stdout=None,
                stderr=error,
                exit_code=-1,
                command_status="failed",
                action_id=action_id,
                task_id=task_id,
            )
        )

    def _execute_command(self, command: str, *, action_id: Optional[str] = None):
        """执行命令 - 通过 Remote Bash API（异步）"""
        if not command:
            print("  ⚠️  Empty command, skipping")
            self._append_failed_command_result(
                command or "",
                "Empty command was not executed.",
                action_id=action_id,
            )
            return

        # 先停止之前的任务
        self._stop_current_task()

        # Bash 中以 # 开头的行会被当作注释；若整段命令都是注释，直接给出明确提示。
        if self._is_all_comment_lines(command):
            tip = (
                "All lines in this exec content were treated as bash comments, so nothing was executed. "
                "This runtime executes exec content via bash. "
                "If you want to run Baritone commands, use: mcapi chat \"<baritone_command>\" "
                "(for example: mcapi chat \"#mine dirt\"). "
                "Otherwise, the game will not receive or execute Baritone commands."
            )
            state = StateItem(
                type="command_result",
                timestamp=time.time(),
                command=command,
                stdout=None,
                stderr=tip,
                exit_code=2,
                command_status="failed",
                action_id=action_id,
            )
            self._append_state(state)
            print("  ⚠️  Command skipped: all lines are bash comments")
            return

        print(f"  ⚡ Executing: {command[:60]}...")
        try:
            # 调用异步API（命令在容器的当前目录执行，默认为/workspace）
            response = requests.post(
                f"{self.remote_bash_url}/exec_async",
                json={"command": command},
                timeout=5
            )
            response.raise_for_status()
            result = response.json()

            if not result.get("success"):
                error = str(result.get("error") or "Remote Bash rejected the command.")
                print(f"  ⚠️  Failed to start command: {error}")
                self._append_failed_command_result(command, error, action_id=action_id)
                return

            task_id = result.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                error = "Remote Bash accepted the command without returning a task_id."
                print(f"  ⚠️  Failed to start command: {error}")
                self._append_failed_command_result(command, error, action_id=action_id)
                return
            with self.task_lock:
                self.current_task_id = task_id
                self.current_action_id = action_id

            print(f"  ✓ Task started: {task_id}")

            # 启动后台线程监控任务
            monitor_thread = threading.Thread(
                target=self._monitor_task,
                args=(task_id, command, action_id),
                daemon=True
            )
            monitor_thread.start()

        except Exception as e:
            print(f"  ⚠️  Command execution error: {e}")
            self._append_failed_command_result(
                command,
                f"Execution error: {e}",
                action_id=action_id,
            )

    @staticmethod
    def _is_all_comment_lines(command: str) -> bool:
        """判断命令是否由空行与 # 注释行组成。"""
        lines = command.splitlines()
        non_empty = [line for line in lines if line.strip()]
        if not non_empty:
            return False
        return all(line.lstrip().startswith("#") for line in non_empty)

    def _monitor_task(
        self,
        task_id: str,
        command: str,
        action_id: Optional[str] = None,
    ):
        """后台监控任务状态"""
        poll_interval = 0.5  # 每0.5秒轮询一次
        start_time = time.time()

        while self.running:
            # Keep the timeout outside the GET try/except. Otherwise a broken
            # Remote Bash endpoint can retry forever without emitting a result.
            if time.time() - start_time > self.exec_timeout:
                print(f"  ⚠️  Task timeout ({self.exec_timeout}s), stopping...")
                self._stop_task(task_id)
                self._append_state(
                    StateItem(
                        type="command_result",
                        timestamp=time.time(),
                        command=command,
                        stderr=(
                            f"Command timed out after {self.exec_timeout:g} seconds."
                        ),
                        exit_code=124,
                        command_status="timed_out",
                        action_id=action_id,
                        task_id=task_id,
                    )
                )
                break
            try:
                # 查询任务状态
                response = requests.get(
                    f"{self.remote_bash_url}/task/{task_id}",
                    timeout=2
                )
                response.raise_for_status()
                result = response.json()

                status = result.get("status")

                # 如果任务完成（completed/failed/stopped）
                if status in ["completed", "failed", "stopped"]:
                    stdout_str = result.get("stdout", "")
                    stderr_str = result.get("stderr", "")
                    exit_code = result.get("exit_code", 0)

                    # 清除当前任务ID
                    with self.task_lock:
                        if self.current_task_id == task_id:
                            self.current_task_id = None
                            self.current_action_id = None

                    # 始终上报命令完成事件（即使无输出）
                    state = StateItem(
                        type="command_result",
                        timestamp=time.time(),
                        command=command,
                        stdout=stdout_str if stdout_str else None,
                        stderr=stderr_str if stderr_str else None,
                        exit_code=exit_code,
                        command_status=status,
                        action_id=action_id,
                        task_id=task_id,
                    )

                    self._append_state(state)

                    if stdout_str or stderr_str:
                        print(f"  ✓ Task {status} (exit code: {exit_code})")
                    else:
                        print(f"  ✓ Task {status} (no output)")

                    break  # 任务结束，退出监控

                # 继续轮询
                time.sleep(poll_interval)

            except Exception as e:
                print(f"  ⚠️  Task monitor error: {e}")
                time.sleep(poll_interval)

    def _stop_current_task(self, expected_action_id: Optional[str] = None):
        """停止当前任务"""
        with self.task_lock:
            if not self.current_task_id:
                return
            if (
                expected_action_id is not None
                and getattr(self, "current_action_id", None) != expected_action_id
            ):
                return

            task_id = self.current_task_id

        self._stop_task(task_id)

    def _stop_task(self, task_id: str) -> None:
        """Stop one exact Remote Bash task without clearing a newer task id."""
        with self.task_lock:
            if self.current_task_id == task_id:
                self.current_task_id = None
                self.current_action_id = None

        print(f"  🛑 Stopping task: {task_id}")
        try:
            response = requests.post(
                f"{self.remote_bash_url}/task/{task_id}/stop",
                timeout=5
            )
            response.raise_for_status()
            result = response.json()

            if result.get("success"):
                print(f"  ✓ Task stopped")
            else:
                print(f"  ⚠️  Failed to stop task: {result.get('error')}")

        except Exception as e:
            print(f"  ⚠️  Error stopping task: {e}")

    # ===== 聊天监控线程 =====

    def _chat_monitor_loop(self):
        """独立线程：监控聊天日志"""
        while self.running:
            try:
                new_messages = self._read_new_chat_messages()

                for msg_type, content in new_messages:
                    state = StateItem(
                        type="chat_message",
                        timestamp=time.time(),
                        chat_content=content,
                        chat_type=msg_type
                    )

                    self._append_state(state)

                    print(f"💬 Chat [{msg_type}]: {content}")

                time.sleep(self.chat_check_interval)

            except Exception as e:
                print(f"⚠️  Chat monitor error: {e}")
                time.sleep(1)

    def _read_new_chat_messages(self):
        """从日志文件读取新的聊天消息（从上次读取位置继续） - 通过 Remote Bash API

        Returns:
            List[Tuple[str, str]]: [(msg_type, content), ...]
            msg_type: "player" 或 "system"
        """
        try:
            # 1. 获取日志总行数
            wc_cmd = f"wc -l {self.log_file_path}"
            response = requests.post(
                f"{self.remote_bash_url}/exec",
                json={"command": wc_cmd},
                timeout=2
            )
            response.raise_for_status()
            wc_result = response.json()

            if wc_result.get("exit_code") != 0:
                return []

            total_lines = int(wc_result.get("stdout", "0").strip().split()[0])

            # 2a. 首次读取：对齐到文件末尾，跳过 agent 启动前的历史日志
            if not self._chat_offset_initialized:
                self.log_line_offset = total_lines
                self._chat_offset_initialized = True
                return []

            # 2b. 文件被截断（client 重启导致 latest.log 轮转）：从头读新文件
            if total_lines < self.log_line_offset:
                self.log_line_offset = 0

            # 2c. 如果没有新行，直接返回
            if total_lines <= self.log_line_offset:
                return []

            # 3. 读取新增的行（从 offset+1 到 total_lines）
            start_line = self.log_line_offset + 1
            sed_cmd = f"sed -n '{start_line},{total_lines}p' {self.log_file_path}"
            response = requests.post(
                f"{self.remote_bash_url}/exec",
                json={"command": sed_cmd},
                timeout=2
            )
            response.raise_for_status()
            sed_result = response.json()

            if sed_result.get("exit_code") != 0:
                return []

            # 4. 更新offset
            self.log_line_offset = total_lines

            # 5. 解析聊天消息
            lines = sed_result.get("stdout", "").strip().split('\n')
            messages = []

            for line in lines:
                if not line or '[CHAT]' not in line:
                    continue

                # 提取聊天内容
                chat_match = re.search(r'\[CHAT\]\s+(.*)', line)
                if not chat_match:
                    continue

                chat_content = chat_match.group(1).strip()

                # 判断消息类型
                if chat_content.startswith('<') and '>' in chat_content:
                    # 玩家消息: <玩家名> 消息内容
                    msg_type = "player"
                else:
                    # 系统消息
                    msg_type = "system"

                messages.append((msg_type, chat_content))

            return messages

        except Exception as e:
            print(f"⚠️  Error reading chat log: {e}")
            return []

    def send_action(self, action: Action):
        """Agent发送动作"""
        self.action_queue.put(action)
