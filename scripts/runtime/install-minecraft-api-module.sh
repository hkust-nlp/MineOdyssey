#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
    echo "  ⚠ python3 not found, skip minecraft_api module install"
    exit 0
fi

ensure_python_package() {
    local module_name="$1"
    local package_spec="$2"

    if python3 - "$module_name" <<'PY' >/dev/null 2>&1
import importlib.util
import sys
module = sys.argv[1]
raise SystemExit(0 if importlib.util.find_spec(module) else 1)
PY
    then
        echo "  ✓ python module available: ${module_name}"
        return 0
    fi

    if ! command -v pip3 >/dev/null 2>&1; then
        echo "  ⚠ pip3 not found, cannot install ${package_spec}"
        return 1
    fi

    echo "  installing missing python package: ${package_spec}"
    if pip3 install --no-cache-dir "${package_spec}" >/tmp/mcbots-pip-install.log 2>&1; then
        echo "  ✓ installed python package: ${package_spec}"
        return 0
    fi

    echo "  ⚠ failed to install ${package_spec}, tail log:"
    tail -n 40 /tmp/mcbots-pip-install.log || true
    return 1
}

ensure_python_package "openai" "openai>=1.0.0" || true

SOURCE_CANDIDATES=(
    "${PROJECT_ROOT}/agent/minecraft_api.py"
    "/workspace/mcbots/agent/minecraft_api.py"
    "/app/agent/minecraft_api.py"
    "/workspace/agent/minecraft_api.py"
)

SRC=""
for candidate in "${SOURCE_CANDIDATES[@]}"; do
    if [[ -f "$candidate" ]]; then
        SRC="$candidate"
        break
    fi
done

if [[ -z "$SRC" ]]; then
    echo "  ⚠ minecraft_api source not found, skip install"
    exit 0
fi

# Serialize concurrent installer calls across parallel clients so the global
# site-packages target is written at most once at a time.
if command -v flock >/dev/null 2>&1; then
    exec 9>/tmp/mcbots-minecraft-api.lock
    flock -x 9
fi

python3 - "$SRC" <<'PY'
import os
import pathlib
import sys
import sysconfig

src = pathlib.Path(sys.argv[1])
if not src.is_file():
    print("  ⚠ minecraft_api source missing, skip install")
    raise SystemExit(0)

paths = sysconfig.get_paths()
target_dir = paths.get("purelib") or paths.get("platlib")
if not target_dir:
    print("  ⚠ cannot resolve python site-packages path, skip install")
    raise SystemExit(0)

dst = pathlib.Path(target_dir) / "minecraft_api.py"
src_bytes = src.read_bytes()
if dst.is_file():
    try:
        if dst.read_bytes() == src_bytes:
            print(f"  ✓ minecraft_api already up-to-date -> {dst}")
            raise SystemExit(0)
    except Exception:
        pass
tmp = dst.with_name(f"{dst.name}.tmp.{os.getpid()}")
try:
    tmp.write_bytes(src_bytes)
    os.replace(tmp, dst)
except PermissionError:
    print(f"  ⚠ permission denied writing {dst}, skip global install")
    raise SystemExit(0)
except OSError as e:
    print(f"  ⚠ failed to install minecraft_api -> {dst}: {e}")
    raise SystemExit(0)
print(f"  ✓ installed minecraft_api -> {dst}")
PY
