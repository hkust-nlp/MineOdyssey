"""
Minecraft Agent Bridge API Client

This module provides a Python interface to control Minecraft through the Agent Bridge mod.
All methods communicate with the mod via HTTP/JSON API.

Example:
    api = MinecraftAPI()
    if api.is_connected():
        api.move_forward(True)
        time.sleep(2)
        api.move_forward(False)
"""

import json
import os
from pathlib import Path
import requests
import time
from typing import Dict, Any, Optional, Tuple
from enum import Enum


DEFAULT_RUNTIME_CONFIG_PATH = Path("/workspace/.mcbots_runtime.json")
DEFAULT_PRESS_DURATION_SEC = 0.05


def _runtime_config_path() -> Path:
    override = os.environ.get("MCBOTS_RUNTIME_CONFIG", "").strip()
    if override:
        return Path(override)

    workspace_root = os.environ.get("MCBOTS_WORKSPACE_ROOT", "").strip()
    if workspace_root:
        return Path(workspace_root) / ".mcbots_runtime.json"

    cwd_candidate = Path.cwd() / ".mcbots_runtime.json"
    if cwd_candidate.is_file():
        return cwd_candidate

    for candidate in (
        DEFAULT_RUNTIME_CONFIG_PATH,
        Path("/workspace/mcbots/.mcbots_runtime.json"),
    ):
        if candidate.is_file():
            return candidate

    return DEFAULT_RUNTIME_CONFIG_PATH


def _load_runtime_config() -> Dict[str, Any]:
    """Load endpoint config from the per-agent workspace file."""
    runtime_config_path = _runtime_config_path()
    if not runtime_config_path.is_file():
        raise RuntimeError(
            f"Missing runtime config file: {runtime_config_path}. "
            "This file must be generated in the agent workspace before using MinecraftAPI/RemoteBashAPI."
        )
    try:
        with runtime_config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise RuntimeError(f"Failed to read runtime config: {runtime_config_path}: {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid runtime config format in {runtime_config_path}: root must be an object")
    return data


def _read_endpoint(data: Dict[str, Any], section: str) -> Tuple[str, int]:
    runtime_config_path = _runtime_config_path()
    raw = data.get(section)
    if not isinstance(raw, dict):
        raise RuntimeError(f"Invalid runtime config: missing object '{section}' in {runtime_config_path}")
    host = raw.get("host")
    port = raw.get("port")
    if not isinstance(host, str) or not host.strip():
        raise RuntimeError(f"Invalid runtime config: '{section}.host' must be a non-empty string")
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise RuntimeError(f"Invalid runtime config: '{section}.port' must be an integer in [1, 65535]")
    return host.strip(), port


class InputType(Enum):
    """Available input types for player control"""
    MOVE_FORWARD = "move_forward"
    MOVE_BACK = "move_back"
    MOVE_LEFT = "move_left"
    MOVE_RIGHT = "move_right"
    JUMP = "jump"
    SNEAK = "sneak"
    SPRINT = "sprint"
    CLICK_LEFT = "click_left"
    CLICK_RIGHT = "click_right"


class MinecraftAPI:
    """
    Python client for the Minecraft Agent Bridge API.

    This class controls a Minecraft player through AgentBridge's direct client
    input APIs exposed over HTTP.

    Attributes:
        base_url (str): The base URL for API requests
        timeout (float): Default timeout for HTTP requests in seconds
    """

    def __init__(self, timeout: float = 5.0):
        """
        Initialize the Minecraft API client.

        Args:
            timeout: Default timeout for HTTP requests in seconds

        Notes:
            Endpoint host/port are loaded from runtime config in the workspace.
        """
        cfg = _load_runtime_config()
        host, port = _read_endpoint(cfg, "agentbridge")
        self.base_url = f"http://{host}:{port}"
        self.timeout = timeout

    def _request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Make an HTTP GET request to the API.

        Args:
            endpoint: API endpoint path (e.g., "/api/health")
            params: Optional query parameters

        Returns:
            Parsed JSON response as a dictionary

        Raises:
            requests.RequestException: If the request fails
        """
        url = f"{self.base_url}{endpoint}"
        response = requests.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    # ==================== Health & Status ====================

    def health_check(self) -> Dict[str, Any]:
        """
        Check if the API is available and get mod status.

        Returns:
            Dict with keys including success, status, mod_version, and backend

        Example:
            >>> api.health_check()
            {'success': True, 'status': 'healthy', 'mod_version': '1.0.0'}
        """
        return self._request("/api/health")

    def is_connected(self) -> bool:
        """
        Check if connection to the API is working.

        Returns:
            True if connected and healthy, False otherwise
        """
        try:
            result = self.health_check()
            return (
                result.get("success", False)
                and result.get("status") == "healthy"
            )
        except Exception:
            return False

    def get_state(self) -> Dict[str, Any]:
        """
        Get the current game state including player position, rotation, health, etc.

        Returns:
            Dict with 'data' containing:
                - position: {x, y, z}
                - rotation: {yaw, pitch}
                - health: float
                - max_health: float
                - food: int
                - gamemode: str
                - motion: {x, y, z}
                - on_ground: bool

        Example:
            >>> state = api.get_state()
            >>> print(f"Position: {state['data']['position']}")
            Position: {'x': 100, 'y': 64, 'z': 200}
        """
        return self._request("/api/state")

    def get_position(self) -> Optional[Tuple[int, int, int]]:
        """
        Get player position as a tuple.

        Returns:
            (x, y, z) tuple or None if failed
        """
        try:
            state = self.get_state()
            pos = state["data"]["position"]
            return (pos["x"], pos["y"], pos["z"])
        except Exception:
            return None

    def get_rotation(self) -> Optional[Tuple[float, float]]:
        """
        Get player rotation as a tuple.

        Returns:
            (yaw, pitch) tuple or None if failed
        """
        try:
            state = self.get_state()
            rot = state["data"]["rotation"]
            return (rot["yaw"], rot["pitch"])
        except Exception:
            return None

    # ==================== Input Control ====================

    def _normalize_input_type(self, input_type: InputType | str) -> InputType | None:
        """Normalize string/enum input type into InputType enum."""
        if isinstance(input_type, InputType):
            return input_type
        if isinstance(input_type, str):
            try:
                return InputType[input_type.upper()]
            except KeyError:
                return None
        return None

    def _set_input_state(self, input_type: InputType | str, state: bool) -> Dict[str, Any]:
        """Low-level hold/release input state used internally by `press`."""
        normalized = self._normalize_input_type(input_type)
        if normalized is None:
            return {
                "success": False,
                "error": f"Invalid input type: {input_type}. Valid types: {', '.join([t.name for t in InputType])}"
            }

        endpoint = f"/api/input/{normalized.value}/{str(state).lower()}"
        return self._request(endpoint)

    def press(self, input_type: InputType | str, duration: float = DEFAULT_PRESS_DURATION_SEC) -> Dict[str, Any]:
        """
        Tap an input by holding it for `duration` seconds then releasing.

        Args:
            input_type: Input enum name/value
            duration: Hold duration in seconds (default 0.05)
        """
        if duration < 0:
            return {"success": False, "error": f"Invalid duration: {duration}. Must be >= 0."}

        normalized = self._normalize_input_type(input_type)
        if normalized is None:
            return {
                "success": False,
                "error": f"Invalid input type: {input_type}. Valid types: {', '.join([t.name for t in InputType])}"
            }

        press_resp = self._set_input_state(normalized, True)
        if not press_resp.get("success", False):
            return {
                "success": False,
                "message": "Input tap failed during press",
                "input": normalized.name,
                "duration": duration,
                "press": press_resp,
            }
        time.sleep(duration)
        release_resp = self._set_input_state(normalized, False)
        success = bool(release_resp.get("success", False))
        return {
            "success": success,
            "message": "Input tapped" if success else "Input tap failed during release",
            "input": normalized.name,
            "duration": duration,
            "press": press_resp,
            "release": release_resp,
        }

    def move_forward(self, duration: float = DEFAULT_PRESS_DURATION_SEC) -> Dict[str, Any]:
        """Tap forward key (W) for duration."""
        return self.press(InputType.MOVE_FORWARD, duration)

    def move_back(self, duration: float = DEFAULT_PRESS_DURATION_SEC) -> Dict[str, Any]:
        """Tap back key (S) for duration."""
        return self.press(InputType.MOVE_BACK, duration)

    def move_left(self, duration: float = DEFAULT_PRESS_DURATION_SEC) -> Dict[str, Any]:
        """Tap left key (A) for duration."""
        return self.press(InputType.MOVE_LEFT, duration)

    def move_right(self, duration: float = DEFAULT_PRESS_DURATION_SEC) -> Dict[str, Any]:
        """Tap right key (D) for duration."""
        return self.press(InputType.MOVE_RIGHT, duration)

    def jump(self, duration: float = DEFAULT_PRESS_DURATION_SEC) -> Dict[str, Any]:
        """Tap jump key (Space) for duration."""
        return self.press(InputType.JUMP, duration)

    def sneak(self, duration: float = DEFAULT_PRESS_DURATION_SEC) -> Dict[str, Any]:
        """Tap sneak key (Shift) for duration."""
        return self.press(InputType.SNEAK, duration)

    def sprint(self, duration: float = DEFAULT_PRESS_DURATION_SEC) -> Dict[str, Any]:
        """Tap sprint key (Ctrl) for duration."""
        return self.press(InputType.SPRINT, duration)

    def click_left(self, duration: float = DEFAULT_PRESS_DURATION_SEC) -> Dict[str, Any]:
        """Tap left click for duration."""
        return self.press(InputType.CLICK_LEFT, duration)

    def click_right(self, duration: float = DEFAULT_PRESS_DURATION_SEC) -> Dict[str, Any]:
        """Tap right click for duration."""
        return self.press(InputType.CLICK_RIGHT, duration)

    # ==================== Look Control ====================

    def _resolve_look_target(self, yaw: float, pitch: float, mode: str) -> Dict[str, Any]:
        normalized_mode = (mode or "relative").lower()
        if normalized_mode not in {"relative", "absolute"}:
            return {
                "success": False,
                "error": f"Invalid look mode: {mode}. Valid modes: relative, absolute"
            }

        if normalized_mode == "absolute":
            return {
                "success": True,
                "mode": "absolute",
                "target": {"yaw": float(yaw), "pitch": float(pitch)},
            }

        state = self.get_state()
        if not state.get("success", False):
            return {
                "success": False,
                "error": f"Failed to get state for relative look: {state}"
            }

        data = state.get("data") or {}
        rotation = data.get("rotation") or {}
        if "yaw" not in rotation or "pitch" not in rotation:
            return {
                "success": False,
                "error": f"Missing rotation in state response: {state}"
            }

        base_yaw = float(rotation["yaw"])
        base_pitch = float(rotation["pitch"])
        target_yaw = base_yaw + float(yaw)
        unclamped_target_pitch = base_pitch + float(pitch)
        target_pitch = max(-90.0, min(90.0, unclamped_target_pitch))

        return {
            "success": True,
            "mode": "relative",
            "base_rotation": {"yaw": base_yaw, "pitch": base_pitch},
            "delta": {"yaw": float(yaw), "pitch": float(pitch)},
            "target": {"yaw": target_yaw, "pitch": target_pitch},
            "pitch_clamped": target_pitch != unclamped_target_pitch,
        }

    def set_look(self, yaw: float = 0.0, pitch: float = 0.0, mode: str = "relative") -> Dict[str, Any]:
        """
        Set player look direction in relative or absolute mode.

        This method always uses interaction-mode aiming internally (not exposed as a public parameter).

        Args:
            yaw:
                - mode="relative": delta yaw (positive = turn left, negative = turn right)
                - mode="absolute": absolute yaw angle
            pitch:
                - mode="relative": delta pitch (positive = look down, negative = look up)
                - mode="absolute": absolute pitch angle (-90 to 90)
            mode: "relative" (default) or "absolute"

        Returns:
            Response dict with success status

        Example:
            >>> api.set_look(30, -10)  # Relative: turn left 30°, look up 10°
            >>> api.set_look(180, 0, mode="absolute")  # Absolute facing north
        """
        resolved = self._resolve_look_target(yaw, pitch, mode)
        if not resolved.get("success", False):
            return resolved

        target = resolved["target"]
        params = {"yaw": target["yaw"], "pitch": target["pitch"], "interact": "true"}
        result = self._request("/api/look", params)
        result["look_mode"] = resolved["mode"]
        result["requested"] = {"yaw": float(yaw), "pitch": float(pitch)}
        result["resolved"] = {k: v for k, v in resolved.items() if k not in {"success"}}
        return result

    # ==================== Block Interaction ====================

    def right_click_block(self, x: int, y: int, z: int) -> Dict[str, Any]:
        """
        Right-click a block at the specified position.

        This can open containers (chests, furnaces), press buttons, etc.

        Args:
            x, y, z: Block coordinates (floats will be converted to int)

        Returns:
            Response dict with success status
        """
        params = {"x": int(x), "y": int(y), "z": int(z)}
        return self._request("/api/right_click_block", params)

    def right_click(self, duration: float = DEFAULT_PRESS_DURATION_SEC) -> Dict[str, Any]:
        """
        Tap right-click input for `duration` seconds.

        Returns:
            Response dict with success status
        """
        return self.press(InputType.CLICK_RIGHT, duration)

    # ==================== GUI/Container Operations ====================

    def window_click(
        self,
        window: int,
        slot: int,
        button: int,
        click_type: str = "PICKUP"
    ) -> Dict[str, Any]:
        """
        Click a slot in a container window.

        Args:
            window: Window ID (0 = player inventory)
            slot: Slot ID to click
            button: Button to use (0 = left, 1 = right)
            click_type: Type of click (PICKUP, QUICK_MOVE, SWAP, CLONE, THROW, QUICK_CRAFT, PICKUP_ALL)

        Returns:
            Response dict with success status

        Examples:
            >>> # Left-click slot 9 in player inventory
            >>> api.window_click(0, 9, 0, "PICKUP")

            >>> # Shift-click to quick move
            >>> api.window_click(0, 10, 0, "QUICK_MOVE")

            >>> # Right-click for half stack
            >>> api.window_click(0, 9, 1, "PICKUP")
        """
        params = {
            "window": window,
            "slot": slot,
            "button": button,
            "type": click_type
        }
        return self._request("/api/window_click", params)

    def close_gui(self) -> Dict[str, Any]:
        """
        Close the currently open interactive GUI and return to first-person view.

        Typical GUI examples:
        - Player inventory screen
        - Chest/furnace/crafting table/trading screens

        Behavior:
        - If a GUI is open: closes that GUI.
        - If no GUI is open: usually a no-op.
        - This does not exit the game or stop the client process.

        Returns:
            Response dict with success status

        Example:
            >>> api.close_gui()
        """
        return self._request("/api/close_gui")

    # ==================== Helper Methods ====================

    def stop_all_movement(self) -> None:
        """Stop all movement inputs (forward, back, left, right, jump, sneak, sprint)."""
        for input_type in [
            InputType.MOVE_FORWARD,
            InputType.MOVE_BACK,
            InputType.MOVE_LEFT,
            InputType.MOVE_RIGHT,
                InputType.JUMP,
                InputType.SNEAK,
                InputType.SPRINT
        ]:
            try:
                self._set_input_state(input_type, False)
            except Exception:
                pass  # Continue even if one fails

    def walk_forward(self, duration: float) -> None:
        """
        Walk forward for a specified duration.

        Args:
            duration: Time to walk in seconds

        Example:
            >>> api.walk_forward(2.0)  # Walk forward for 2 seconds
        """
        import time
        self.move_forward(True)
        time.sleep(duration)
        self.move_forward(False)

    def look_at(self, target_x: int, target_y: int, target_z: int) -> None:
        """
        Look at a specific block position.

        Args:
            target_x, target_y, target_z: Target block coordinates

        Note:
            This is a simplified version. For accurate aiming, you should
            calculate the proper yaw/pitch based on player position.
        """
        import math

        # Get current position
        pos = self.get_position()
        if not pos:
            return

        px, py, pz = pos

        # Calculate direction vector
        dx = target_x - px
        dy = target_y - (py + 1.62)  # Eye height
        dz = target_z - pz

        # Calculate yaw and pitch
        horizontal_distance = math.sqrt(dx * dx + dz * dz)
        yaw = math.degrees(math.atan2(dz, dx)) - 90
        pitch = -math.degrees(math.atan2(dy, horizontal_distance))

        # Normalize yaw to 0-360
        yaw = yaw % 360

        self.set_look(yaw, pitch, mode="absolute")


class RemoteBashAPI:
    """
    Python client for the Remote Bash Execution Server.

    This class provides a simple interface to execute bash commands remotely.
    Use this for any container operations: xdotool, file operations, etc.

    Attributes:
        base_url (str): The base URL for API requests
        timeout (float): Default timeout for HTTP requests in seconds
    """

    def __init__(self, timeout: float = 30.0):
        """
        Initialize the Remote Bash API client.

        Args:
            timeout: Default timeout for HTTP requests in seconds

        Notes:
            Endpoint host/port are loaded from runtime config in the workspace.
        """
        cfg = _load_runtime_config()
        host, port = _read_endpoint(cfg, "remote_bash")
        self.base_url = f"http://{host}:{port}"
        self.timeout = timeout

    def _request(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Make an HTTP request to the server."""
        url = f"{self.base_url}{endpoint}"
        if data is None:
            response = requests.get(url, timeout=self.timeout)
        else:
            response = requests.post(url, json=data, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def health_check(self) -> Dict[str, Any]:
        """Check if the server is available."""
        return self._request("/health")

    def is_connected(self) -> bool:
        """Check if connection to the server is working."""
        try:
            result = self.health_check()
            return result.get("success", False)
        except Exception:
            return False

    def exec(self, command: str, timeout: Optional[int] = None, working_dir: Optional[str] = None) -> Dict[str, Any]:
        """
        Execute a bash command remotely.

        Args:
            command: The bash command to execute
            timeout: Optional timeout in seconds (overrides default)
            working_dir: Optional working directory

        Returns:
            Dict with success, stdout, stderr, exit_code

        Example:
            >>> bash = RemoteBashAPI()
            >>> result = bash.exec("xdotool key w")
            >>> print(result['stdout'])
        """
        data = {"command": command}
        if timeout is not None:
            data["timeout"] = timeout
        if working_dir is not None:
            data["working_dir"] = working_dir

        return self._request("/exec", data)


if __name__ == "__main__":
    # Quick test
    api = MinecraftAPI()

    print("Testing connection...")
    health = api.health_check()
    print(f"Health check: {health}")

    if api.is_connected():
        print("\n✅ Connected to Minecraft!")

        state = api.get_state()
        print(f"\nCurrent position: {state['data']['position']}")
        print(f"Current rotation: {state['data']['rotation']}")
        print(f"Health: {state['data']['health']}/{state['data']['max_health']}")
    else:
        print("\n❌ Not connected to Minecraft")
