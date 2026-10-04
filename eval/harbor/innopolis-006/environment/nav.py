#!/usr/bin/env python3
"""Agent CLI: the only bridge to the isolated Minecraft/evaluator container."""
import argparse
import base64
import http.client
import json
import math
from pathlib import Path
import socket
import sys

SOCKET = "/run/navigation/api.sock"
MAX_EXEC_TIMEOUT_SEC = 120


def exec_timeout(value):
    timeout = float(value)
    if not math.isfinite(timeout) or not 0 < timeout <= MAX_EXEC_TIMEOUT_SEC:
        raise argparse.ArgumentTypeError("timeout must be greater than 0 and at most 120 seconds")
    return timeout


class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET)


def request(action, payload=None):
    connection = Connection("localhost", timeout=660)
    try:
        connection.request("POST", "/" + action, json.dumps(payload or {}),
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise RuntimeError(body.decode())
        return body
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("start", "state", "claim-done", "result"):
        sub.add_parser(name)
    screenshot = sub.add_parser("screenshot")
    screenshot.add_argument("path", type=Path)
    read_image = sub.add_parser("read-image")
    read_image.add_argument("source", help="Image path inside the action sandbox")
    read_image.add_argument("path", type=Path, help="Local destination")
    rpc = sub.add_parser("rpc", help="Navigation lifecycle and action transport")
    rpc.add_argument("endpoint", choices=["rpc", "session", "events", "claim-done", "result"])
    rpc.add_argument("payload", nargs="?", default="{}")
    execute = sub.add_parser("exec")
    execute.add_argument("command", help="Shell command inside the game action sandbox")
    execute.add_argument("--timeout", type=exec_timeout, default=30,
                         help="Seconds to wait (default: 30; maximum: 120)")
    args = parser.parse_args()
    payload = ({"command": args.command, "timeout": args.timeout}
               if args.action == "exec" else {})
    if args.action == "read-image":
        payload = {"path": args.source}
    if args.action == "rpc":
        print(request(args.endpoint, json.loads(args.payload)).decode())
        return 0
    body = request(args.action, payload)
    if args.action in {"screenshot", "read-image"}:
        args.path.write_bytes(body)
        print(args.path)
    else:
        print(body.decode())
        if args.action == "exec":
            return 0 if json.loads(body).get("exit_code") == 0 else 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as error:
        sys.exit(str(error))
