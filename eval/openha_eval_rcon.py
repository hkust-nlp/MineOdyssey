from __future__ import annotations

import random
import re
import socket
import struct
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple


LogFn = Callable[[str], None]


@dataclass
class RconExecResult:
    returncode: int
    stdout: str
    stderr: str


class RconClient:
    AUTH_PACKET_TYPE = 3
    COMMAND_PACKET_TYPE = 2
    AUTH_RESPONSE_PACKET_TYPE = 2

    def __init__(self, host: str, port: int, password: str, log_fn: Optional[LogFn] = None):
        self.host = host
        self.port = port
        self.password = password
        self.log_fn = log_fn

    def _log(self, message: str) -> None:
        if self.log_fn is not None:
            self.log_fn(message)

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("RCON connection closed unexpectedly")
            buf += chunk
        return buf

    @classmethod
    def _send_packet(cls, sock: socket.socket, request_id: int, packet_type: int, payload: str) -> None:
        payload_bytes = payload.encode("utf-8") + b"\x00\x00"
        length = len(payload_bytes) + 8
        packet = struct.pack("<iii", length, request_id, packet_type) + payload_bytes
        sock.sendall(packet)

    @classmethod
    def _read_packet(cls, sock: socket.socket) -> Tuple[int, int, str]:
        (length,) = struct.unpack("<i", cls._recv_exact(sock, 4))
        if length < 10:
            raise RuntimeError(f"Invalid RCON packet length: {length}")
        body = cls._recv_exact(sock, length)
        request_id, packet_type = struct.unpack("<ii", body[:8])
        payload = body[8:-2].decode("utf-8", errors="replace")
        return request_id, packet_type, payload

    def _authenticate(self, sock: socket.socket, timeout_sec: float) -> None:
        auth_id = random.randint(1, 2_000_000_000)
        self._send_packet(
            sock=sock,
            request_id=auth_id,
            packet_type=self.AUTH_PACKET_TYPE,
            payload=self.password,
        )

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            sock.settimeout(remaining)
            req_id, packet_type, _payload = self._read_packet(sock)
            if packet_type != self.AUTH_RESPONSE_PACKET_TYPE:
                continue
            if req_id == -1:
                raise RuntimeError("RCON auth failed (password mismatch)")
            if req_id == auth_id:
                return
        raise RuntimeError("RCON auth timed out")

    def _execute(self, command: str, timeout_sec: float) -> str:
        with socket.create_connection((self.host, self.port), timeout=timeout_sec) as sock:
            sock.settimeout(timeout_sec)
            self._authenticate(sock=sock, timeout_sec=timeout_sec)

            cmd_id = random.randint(1, 2_000_000_000)
            self._send_packet(
                sock=sock,
                request_id=cmd_id,
                packet_type=self.COMMAND_PACKET_TYPE,
                payload=command,
            )

            deadline = time.time() + timeout_sec
            got_any = False
            payloads: List[str] = []

            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break

                if got_any:
                    sock.settimeout(min(0.2, remaining))
                else:
                    sock.settimeout(remaining)

                try:
                    req_id, _packet_type, payload = self._read_packet(sock)
                except socket.timeout:
                    if got_any:
                        break
                    raise RuntimeError("RCON command timed out")

                if req_id == -1:
                    raise RuntimeError("RCON command rejected")
                if req_id != cmd_id:
                    continue

                got_any = True
                payloads.append(payload)

            if not got_any:
                raise RuntimeError("No RCON response packet received")

            merged = "\n".join([p for p in payloads if p.strip()]).strip()
            return merged

    def run_raw(self, command: str, timeout_sec: float = 10.0) -> RconExecResult:
        try:
            out = self._execute(command=command, timeout_sec=timeout_sec)
            return RconExecResult(returncode=0, stdout=out, stderr="")
        except Exception as e:  # noqa: BLE001
            return RconExecResult(returncode=1, stdout="", stderr=str(e))

    def run(self, command: str, timeout_sec: float = 10.0) -> str:
        proc = self.run_raw(command=command, timeout_sec=timeout_sec)
        if proc.returncode != 0:
            raise RuntimeError(
                f"RCON command failed (rc={proc.returncode}): {command}\n"
                f"stdout={proc.stdout.strip()}\n"
                f"stderr={proc.stderr.strip()}"
            )
        return proc.stdout.strip()

    def wait_ready(self, timeout_sec: float = 120.0, interval_sec: float = 1.0) -> None:
        deadline = time.time() + timeout_sec
        next_log_at = time.time()
        last_err = ""
        while time.time() < deadline:
            try:
                self.run("list", timeout_sec=10.0)
                return
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                now = time.time()
                if now >= next_log_at:
                    remaining = max(0.0, deadline - now)
                    self._log(
                        "waiting RCON ready... "
                        f"remaining={remaining:.0f}s last_error={last_err}"
                    )
                    next_log_at = now + 5.0
                time.sleep(interval_sec)
        raise RuntimeError(f"RCON not ready within {timeout_sec}s. last_error={last_err}")

    def list_players(self) -> List[str]:
        out = self.run("list", timeout_sec=10.0)
        match = re.search(r"online:?\s*(.*)$", out, flags=re.IGNORECASE)
        if not match:
            return []
        tail = match.group(1).strip()
        if tail in {"", "<none>", "none"}:
            return []
        return [p.strip() for p in tail.split(",") if p.strip()]

    def wait_player_online(self, player_name: str, timeout_sec: float, interval_sec: float = 1.0) -> None:
        deadline = time.time() + timeout_sec
        next_log_at = time.time()
        while time.time() < deadline:
            online = self.list_players()
            if player_name in online:
                return
            now = time.time()
            if now >= next_log_at:
                remaining = max(0.0, deadline - now)
                self._log(
                    f"waiting player online... player={player_name} "
                    f"online={online or ['<none>']} remaining={remaining:.0f}s"
                )
                next_log_at = now + 5.0
            time.sleep(interval_sec)
        raise RuntimeError(f"Player not online within {timeout_sec}s: {player_name}")

    def ensure_score_objective(self, objective: str, criterion: str) -> None:
        self.run(f"scoreboard objectives add {objective} {criterion}", timeout_sec=10.0)
        listed = self.run("scoreboard objectives list", timeout_sec=10.0)
        if objective in listed:
            return
        raise RuntimeError(
            f"Failed to ensure scoreboard objective {objective}. "
            f"objectives_list={listed!r}"
        )

    def get_score(self, player_name: str, objective: str) -> int:
        text = self.run(f"scoreboard players get {player_name} {objective}", timeout_sec=10.0).strip()
        lowered = text.lower()
        if "has no score recorded" in lowered:
            return 0
        if "no entity was found" in lowered:
            return 0
        if "none is set" in lowered:
            return 0

        match = re.search(r"\bhas\s+(-?\d+)\b", text)
        if match:
            return int(match.group(1))
        raise RuntimeError("Unable to parse scoreboard value from response: " f"{text!r}")
