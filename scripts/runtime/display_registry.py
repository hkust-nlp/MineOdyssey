#!/usr/bin/env python3
"""Locked display-index registry for shared X servers."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        rparen = stat_text.rfind(")")
        if rparen != -1:
            rest = stat_text[rparen + 2 :].split()
            if rest:
                state = rest[0]
                if state == "Z":
                    return False
    except FileNotFoundError:
        return False
    except Exception:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except Exception:
        return ""
    if not raw:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def _x_process_holds_display(display_index: int) -> bool:
    needle = f":{display_index}"
    try:
        proc_entries = os.listdir("/proc")
    except Exception:
        proc_entries = []
    for entry in proc_entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if not pid_alive(pid):
            continue
        cmdline = _read_cmdline(pid)
        if not cmdline:
            continue
        if ("Xvfb" in cmdline or "Xorg" in cmdline) and needle in cmdline:
            return True
    return False


def _unlink_if_stale(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except IsADirectoryError:
        return
    except PermissionError:
        return
    except OSError:
        return


def display_available(display_index: int) -> bool:
    lock_file = Path(f"/tmp/.X{display_index}-lock")
    socket_file = Path(f"/tmp/.X11-unix/X{display_index}")

    lock_pid = 0
    if lock_file.exists():
        try:
            digits = "".join(ch for ch in lock_file.read_text(encoding="utf-8", errors="ignore") if ch.isdigit())
            lock_pid = int(digits) if digits else 0
        except Exception:
            lock_pid = 0

    if lock_pid > 0 and pid_alive(lock_pid):
        return False

    if _x_process_holds_display(display_index):
        return False

    # If we are here, lock/socket are considered stale.
    _unlink_if_stale(lock_file)

    try:
        st = socket_file.stat()
        if stat.S_ISSOCK(st.st_mode):
            _unlink_if_stale(socket_file)
    except FileNotFoundError:
        pass
    except Exception:
        # Best-effort cleanup only.
        pass

    return True


def _default_registry(start: int, end: int) -> Dict:
    return {
        "version": 1,
        "range": {"start": start, "end": end},
        "updated_at": now_iso(),
        "allocations": {},
    }


def _normalize_registry(data: Dict, start: int, end: int) -> Dict:
    if not isinstance(data, dict):
        data = {}
    alloc = data.get("allocations")
    if not isinstance(alloc, dict):
        alloc = {}
    normalized: Dict[str, Dict] = {}
    for key, value in alloc.items():
        try:
            idx = int(key)
        except Exception:
            continue
        if not isinstance(value, dict):
            continue
        normalized[str(idx)] = value
    return {
        "version": int(data.get("version", 1)),
        "range": {"start": start, "end": end},
        "updated_at": data.get("updated_at") or now_iso(),
        "allocations": normalized,
    }


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_snapshot_text(path: Path, data: Dict) -> None:
    lines = []
    r = data["range"]
    lines.append("# mcbots display registry")
    lines.append(f"# range: {r['start']}-{r['end']}")
    lines.append(f"# updated_at: {data.get('updated_at', '')}")
    lines.append("DISPLAY\tSERVICE\tOWNER\tPID\tTOKEN\tCREATED_AT")
    alloc = data.get("allocations", {})
    for key in sorted(alloc.keys(), key=lambda x: int(x)):
        entry = alloc[key]
        lines.append(
            "\t".join(
                [
                    key,
                    str(entry.get("service", "")),
                    str(entry.get("owner", "")),
                    str(entry.get("pid", 0)),
                    str(entry.get("token", "")),
                    str(entry.get("created_at", "")),
                ]
            )
        )
    _write_atomic(path, "\n".join(lines).rstrip() + "\n")


class Registry:
    def __init__(self, file: Path, start: int, end: int):
        if start < 1 or end < start:
            raise RuntimeError(f"invalid display range: {start}-{end}")
        self.file = file
        self.start = start
        self.end = end
        self.lock_file = Path(str(file) + ".lock")
        self.snapshot_file = file.with_suffix(".txt")

    @contextmanager
    def locked(self):
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_file.open("a+", encoding="utf-8") as lf:
            locked = False
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                locked = True
            except OSError:
                locked = False
            try:
                yield
            finally:
                if locked:
                    try:
                        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass

    def load(self) -> Dict:
        if not self.file.exists():
            return _default_registry(self.start, self.end)
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        return _normalize_registry(raw, self.start, self.end)

    def save(self, data: Dict) -> None:
        data["updated_at"] = now_iso()
        text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
        _write_atomic(self.file, text + "\n")
        _write_snapshot_text(self.snapshot_file, data)

    @staticmethod
    def cleanup_stale(data: Dict) -> int:
        alloc = data.get("allocations", {})
        remove_keys: List[str] = []
        for key, entry in alloc.items():
            pid = int(entry.get("pid", 0) or 0)
            if pid > 0 and not pid_alive(pid):
                remove_keys.append(key)
        for key in remove_keys:
            alloc.pop(key, None)
        return len(remove_keys)

    def _candidate_indices(self, preferred: int, scan_limit: int) -> List[int]:
        full = list(range(self.start, self.end + 1))
        if preferred < self.start or preferred > self.end:
            return full
        if scan_limit < 0:
            scan_limit = 0
        upper = min(self.end, preferred + scan_limit)
        preferred_window = list(range(preferred, upper + 1))
        preferred_set = set(preferred_window)
        rest = [idx for idx in full if idx not in preferred_set]
        return preferred_window + rest

    def allocate(
        self,
        *,
        count: int,
        service: str,
        owner: str,
        pid: int,
        meta: Dict,
        preferred: int,
        scan_limit: int,
    ) -> List[Dict]:
        if count < 1:
            raise RuntimeError("count must be >= 1")
        with self.locked():
            data = self.load()
            self.cleanup_stale(data)
            alloc = data["allocations"]
            used = {int(k) for k in alloc.keys()}
            free: List[int] = []
            for idx in self._candidate_indices(preferred, scan_limit):
                if idx in used:
                    continue
                if not display_available(idx):
                    continue
                free.append(idx)
                if len(free) >= count:
                    break
            if len(free) < count:
                raise RuntimeError(
                    f"not enough free displays in range {self.start}-{self.end}, "
                    f"requested={count}, available={len(free)}"
                )
            created: List[Dict] = []
            ts = now_iso()
            for idx in free:
                token = uuid.uuid4().hex
                entry = {
                    "display_index": idx,
                    "token": token,
                    "service": service,
                    "owner": owner,
                    "pid": int(pid),
                    "created_at": ts,
                    "updated_at": ts,
                    "meta": meta or {},
                }
                alloc[str(idx)] = entry
                created.append(entry)
            self.save(data)
            return created

    def bind(self, pid: int, tokens: List[str]) -> int:
        if pid <= 0:
            raise RuntimeError("pid must be > 0")
        token_set = set(tokens)
        updated = 0
        with self.locked():
            data = self.load()
            self.cleanup_stale(data)
            alloc = data["allocations"]
            ts = now_iso()
            for entry in alloc.values():
                if entry.get("token") in token_set:
                    entry["pid"] = pid
                    entry["updated_at"] = ts
                    updated += 1
            self.save(data)
        return updated

    def release(self, displays: List[int], tokens: List[str], owner: str) -> int:
        display_set = {int(d) for d in displays}
        token_set = set(tokens)
        removed = 0
        with self.locked():
            data = self.load()
            self.cleanup_stale(data)
            alloc = data["allocations"]
            rm_keys: List[str] = []
            for key, entry in alloc.items():
                match = False
                if display_set and int(key) in display_set:
                    match = True
                if token_set and entry.get("token") in token_set:
                    match = True
                if not display_set and not token_set and owner:
                    match = True
                if not match:
                    continue
                if owner and str(entry.get("owner", "")) != owner:
                    continue
                rm_keys.append(key)
            for key in rm_keys:
                alloc.pop(key, None)
                removed += 1
            self.save(data)
        return removed

    def list_allocations(self, do_cleanup: bool) -> Dict:
        with self.locked():
            data = self.load()
            if do_cleanup:
                self.cleanup_stale(data)
                self.save(data)
            return data


def parse_meta(text: str) -> Dict:
    if not text:
        return {}
    try:
        raw = json.loads(text)
    except Exception as e:
        raise argparse.ArgumentTypeError(f"invalid meta json: {e}") from e
    if not isinstance(raw, dict):
        raise argparse.ArgumentTypeError("meta json must be an object")
    return raw


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Locked display-index registry for mcbots")
    parser.add_argument("--file", default="/workspace/runtime/display-registry.json")
    parser.add_argument("--range-start", type=int, default=200)
    parser.add_argument("--range-end", type=int, default=2000)

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_alloc = sub.add_parser("allocate")
    p_alloc.add_argument("--count", type=int, default=1)
    p_alloc.add_argument("--service", required=True)
    p_alloc.add_argument("--owner", required=True)
    p_alloc.add_argument("--pid", type=int, default=0)
    p_alloc.add_argument("--meta-json", type=parse_meta, default={})
    p_alloc.add_argument("--preferred", type=int, default=0)
    p_alloc.add_argument("--scan-limit", type=int, default=256)

    p_bind = sub.add_parser("bind")
    p_bind.add_argument("--pid", type=int, required=True)
    p_bind.add_argument("--token", action="append", required=True)

    p_rel = sub.add_parser("release")
    p_rel.add_argument("--display", type=int, action="append", default=[])
    p_rel.add_argument("--token", action="append", default=[])
    p_rel.add_argument("--owner", default="")

    p_watch = sub.add_parser("watch")
    p_watch.add_argument("--pid", type=int, required=True)
    p_watch.add_argument("--token", action="append", required=True)
    p_watch.add_argument("--interval-sec", type=float, default=1.5)
    p_watch.add_argument("--owner", default="")

    p_list = sub.add_parser("list")
    p_list.add_argument("--cleanup", action="store_true")

    sub.add_parser("cleanup")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reg = Registry(Path(args.file), args.range_start, args.range_end)

    if args.cmd == "allocate":
        allocs = reg.allocate(
            count=args.count,
            service=args.service,
            owner=args.owner,
            pid=args.pid,
            meta=args.meta_json,
            preferred=args.preferred,
            scan_limit=args.scan_limit,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "registry_file": str(reg.file),
                    "range": {"start": reg.start, "end": reg.end},
                    "allocations": allocs,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.cmd == "bind":
        updated = reg.bind(pid=args.pid, tokens=args.token)
        print(json.dumps({"ok": True, "updated": updated}, ensure_ascii=False))
        return 0

    if args.cmd == "release":
        if not args.display and not args.token and not args.owner:
            raise RuntimeError("release requires --display/--token or --owner")
        removed = reg.release(displays=args.display, tokens=args.token, owner=args.owner)
        print(json.dumps({"ok": True, "removed": removed}, ensure_ascii=False))
        return 0

    if args.cmd == "watch":
        pid = int(args.pid)
        interval = max(0.2, float(args.interval_sec))
        while pid_alive(pid):
            time.sleep(interval)
        reg.release(displays=[], tokens=args.token, owner=args.owner)
        return 0

    if args.cmd == "cleanup":
        with reg.locked():
            data = reg.load()
            removed = reg.cleanup_stale(data)
            reg.save(data)
        print(json.dumps({"ok": True, "removed": removed}, ensure_ascii=False))
        return 0

    if args.cmd == "list":
        data = reg.list_allocations(do_cleanup=bool(args.cleanup))
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    raise RuntimeError(f"unsupported cmd: {args.cmd}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        raise SystemExit(130)
    except Exception as e:
        print(f"display_registry error: {e}", file=sys.stderr)
        raise SystemExit(2)
