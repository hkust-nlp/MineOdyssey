"""Local live dashboard for a materialized navigation run."""

from __future__ import annotations

import argparse
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ASSET_ROOT = Path(__file__).with_name("web")


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _assistant_turns(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    turns: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = _content_text(message.get("content"))
        reasoning = message.get("reasoning_content")
        if not isinstance(reasoning, str):
            reasoning = ""
        turns.append(
            {
                "index": index,
                "reasoning": reasoning,
                "content": content,
                "usage": message.get("usage"),
            }
        )
    return turns


def build_state(
    run_dir: Path,
    novnc_url: str,
    results_root: Path | None = None,
) -> dict[str, Any]:
    from agent.main import build_navigation_system_prompt

    run = _load_json(run_dir / "control" / "run.json", {})
    results_dir_raw = run.get("results_dir") if isinstance(run, dict) else None
    if results_root is not None and isinstance(run, dict):
        results_dir = (
            results_root
            / str(run.get("run_id", run_dir.parent.name))
            / str(run.get("task_id", run_dir.name))
        )
    else:
        results_dir = (
            Path(results_dir_raw)
            if isinstance(results_dir_raw, str) and results_dir_raw
            else run_dir / "results"
        )
    # Container paths and host paths share the repository suffix, but not the root.
    if (
        results_root is None
        and not results_dir.is_dir()
        and isinstance(results_dir_raw, str)
    ):
        marker = "/eval/results/"
        if marker in results_dir_raw:
            repo_root = Path(__file__).resolve().parents[2]
            results_dir = repo_root / "eval" / "results" / results_dir_raw.split(marker, 1)[1]

    messages = _load_json(results_dir / "agent-record" / "messages.json", [])
    status = _load_json(run_dir / "control" / "agent-status.json", {})
    completion = _load_json(results_dir / "completion.json", {})
    claims = _read_jsonl(results_dir / "claims.jsonl")
    events = _read_jsonl(run_dir / "control" / "evaluator-events.jsonl")
    agent_setting = run.get("agent", {}) if isinstance(run, dict) else {}
    return {
        "run": run,
        "novnc_url": novnc_url,
        "system_prompt": build_navigation_system_prompt(
            hints_enabled=bool(
                (run.get("eval_setting", {}) if isinstance(run, dict) else {}).get(
                    "navigation_hints_enabled", False
                )
            ),
            panorama_enabled=bool(agent_setting.get("six_view_enabled", False)),
        ),
        "turns": _assistant_turns(messages),
        "agent_status": status,
        "completion": completion,
        "claims": claims,
        "events": events[-100:],
    }


def make_handler(
    run_dir: Path,
    novnc_url: str,
    results_root: Path | None = None,
) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            request_path = urlparse(self.path).path
            if request_path == "/api/state":
                payload = json.dumps(
                    build_state(run_dir, novnc_url, results_root), ensure_ascii=False
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            relative = "dashboard.html" if request_path == "/" else request_path.lstrip("/")
            asset = (ASSET_ROOT / relative).resolve()
            if ASSET_ROOT.resolve() not in asset.parents or not asset.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            payload = asset.read_bytes()
            content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return DashboardHandler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--results-root", type=Path)
    parser.add_argument(
        "--novnc-url",
        default="http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale",
    )
    args = parser.parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    if not (run_dir / "control" / "run.json").is_file():
        parser.error(f"not a materialized navigation run: {run_dir}")
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(
            run_dir,
            args.novnc_url,
            args.results_root.expanduser().resolve()
            if args.results_root is not None
            else None,
        ),
    )
    print(f"Navigation dashboard: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
