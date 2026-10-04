#!/usr/bin/env bash
# Build and run the Minecraft 1.21.11 navigation stack on Apple Silicon.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${MCBOTS_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
CONTAINER_ROOT="${NAV_CONTAINER_ROOT:-/workspace/mcbots}"
CONTAINER_NAME="${NAV_CONTAINER_NAME:-mcbots-navigation-12111}"
IMAGE="${NAV_CONTAINER_IMAGE:-mcbots-navigation-cpu:1.21.11}"
PLATFORM="${NAV_CONTAINER_PLATFORM:-linux/arm64}"
PLATFORM_ARCH="${PLATFORM#linux/}"
CPUS="${NAV_CONTAINER_CPUS:-10}"
MEMORY="${NAV_CONTAINER_MEMORY:-20G}"
BUILD_CPUS="${NAV_CONTAINER_BUILD_CPUS:-6}"
BUILD_MEMORY="${NAV_CONTAINER_BUILD_MEMORY:-8G}"
NOVNC_HOST_PORT="${NAV_CONTAINER_NOVNC_PORT:-6080}"
RUNTIME_LOCAL_ROOT="$PROJECT_ROOT/eval/templates/_local/navigation-linux-${PLATFORM_ARCH}"
CONTAINER_RUNTIME_LOCAL_ROOT="$CONTAINER_ROOT/eval/templates/_local/navigation-linux-${PLATFORM_ARCH}"
APPLE_CONTAINER_VERSION="1.2.0"
APPLE_CONTAINER_PKG="container-${APPLE_CONTAINER_VERSION}-installer-signed.pkg"
APPLE_CONTAINER_PKG_SHA256="d140d4076ff0593d6b4f7c58722717b2abe87d75452cfe0a203792ba7f48f07c"
APPLE_CONTAINER_PKG_URL="https://github.com/apple/container/releases/download/${APPLE_CONTAINER_VERSION}/${APPLE_CONTAINER_PKG}"

usage() {
    cat <<'EOF'
Usage:
  ./scripts/containers/navigation-cpu-macos.sh install-runtime
  ./scripts/containers/navigation-cpu-macos.sh build
  ./scripts/containers/navigation-cpu-macos.sh start
  ./scripts/containers/navigation-cpu-macos.sh prepare-runtime
  ./scripts/containers/navigation-cpu-macos.sh review TASK_ID
  ./scripts/containers/navigation-cpu-macos.sh pilot TASK_ID [runner args...]
  ./scripts/containers/navigation-cpu-macos.sh formal TASK_ID|--all [runner args...]
  ./scripts/containers/navigation-cpu-macos.sh shell
  ./scripts/containers/navigation-cpu-macos.sh exec COMMAND [args...]
  ./scripts/containers/navigation-cpu-macos.sh status
  ./scripts/containers/navigation-cpu-macos.sh stop
  ./scripts/containers/navigation-cpu-macos.sh delete

Environment overrides:
  NAV_CONTAINER_NAME          Container name (mcbots-navigation-12111)
  NAV_CONTAINER_IMAGE         Image tag (mcbots-navigation-cpu:1.21.11)
  NAV_CONTAINER_PLATFORM      linux/arm64; linux/amd64 enables Rosetta
  NAV_CONTAINER_CPUS          Runtime CPUs (10)
  NAV_CONTAINER_MEMORY        Runtime memory (20G)
  NAV_CONTAINER_BUILD_CPUS    Builder CPUs (6)
  NAV_CONTAINER_BUILD_MEMORY  Builder memory (8G)
  NAV_CONTAINER_NOVNC_PORT    Host noVNC port (6080)

The repository is mounted at /workspace/mcbots. Linux runtime artifacts use
eval/templates/_local/navigation-linux-<arch> and never reuse the macOS runtime.
EOF
}

require_macos_arm() {
    if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
        echo "This launcher requires an Apple Silicon Mac." >&2
        exit 2
    fi
}

require_container_cli() {
    if ! command -v container >/dev/null 2>&1; then
        echo "Apple container is not installed." >&2
        echo "Run: $0 install-runtime" >&2
        exit 2
    fi
}

ensure_system() {
    require_container_cli
    if ! container system status >/dev/null 2>&1; then
        echo "Starting Apple container services..."
        container system start --enable-kernel-install
    fi
}

container_exists() {
    container inspect "$CONTAINER_NAME" >/dev/null 2>&1
}

container_running() {
    container list --quiet | grep -Fxq "$CONTAINER_NAME"
}

ensure_running() {
    ensure_system
    if ! container_exists; then
        echo "Container does not exist: $CONTAINER_NAME" >&2
        echo "Run: $0 start" >&2
        exit 2
    fi
    if ! container_running; then
        container start "$CONTAINER_NAME"
    fi
}

container_exec() {
    container exec \
        --uid "$(id -u)" \
        --gid "$(id -g)" \
        --workdir "$CONTAINER_ROOT" \
        "$CONTAINER_NAME" \
        "$@"
}

install_runtime() {
    require_macos_arm
    if command -v container >/dev/null 2>&1; then
        container system version
        return
    fi
    local downloads_dir="${HOME:?}/Downloads"
    local destination="$downloads_dir/$APPLE_CONTAINER_PKG"
    mkdir -p "$downloads_dir"
    if [[ ! -f "$destination" ]] \
        || [[ "$(shasum -a 256 "$destination" | awk '{print $1}')" != "$APPLE_CONTAINER_PKG_SHA256" ]]; then
        echo "Downloading Apple container ${APPLE_CONTAINER_VERSION}..."
        curl --fail --location --progress-bar \
            "$APPLE_CONTAINER_PKG_URL" \
            --output "$destination"
    fi
    local actual
    actual="$(shasum -a 256 "$destination" | awk '{print $1}')"
    if [[ "$actual" != "$APPLE_CONTAINER_PKG_SHA256" ]]; then
        echo "Installer SHA-256 mismatch: $actual" >&2
        exit 3
    fi
    echo "Opening the signed Apple installer: $destination"
    echo "Complete the administrator-password prompt, then run: $0 build"
    open "$destination"
}

build_image() {
    require_macos_arm
    ensure_system
    container build \
        --platform "$PLATFORM" \
        --cpus "$BUILD_CPUS" \
        --memory "$BUILD_MEMORY" \
        --progress plain \
        --file "$PROJECT_ROOT/containers/Containerfile.navigation-cpu" \
        --tag "$IMAGE" \
        "$PROJECT_ROOT"
}

start_container() {
    require_macos_arm
    ensure_system
    if container_exists; then
        if container_running; then
            echo "Container is already running: $CONTAINER_NAME"
        else
            container start "$CONTAINER_NAME"
            echo "Container started: $CONTAINER_NAME"
        fi
        return
    fi

    mkdir -p \
        "$RUNTIME_LOCAL_ROOT/home" \
        "$RUNTIME_LOCAL_ROOT/gradle" \
        "$RUNTIME_LOCAL_ROOT/uv-cache"

    local run_args=(
        run
        --detach
        --name "$CONTAINER_NAME"
        --init
        --platform "$PLATFORM"
    )
    if [[ "$PLATFORM" == "linux/amd64" ]]; then
        run_args+=(--rosetta)
    fi
    run_args+=(
        --cpus "$CPUS"
        --memory "$MEMORY"
        --shm-size 2G
        --cap-add ALL
        --uid "$(id -u)"
        --gid "$(id -g)"
        --workdir "$CONTAINER_ROOT"
        --volume "$PROJECT_ROOT:$CONTAINER_ROOT"
        --publish "127.0.0.1:${NOVNC_HOST_PORT}:6080"
        --env "HOME=$CONTAINER_RUNTIME_LOCAL_ROOT/home"
        --env "GRADLE_USER_HOME=$CONTAINER_RUNTIME_LOCAL_ROOT/gradle"
        --env "UV_CACHE_DIR=$CONTAINER_RUNTIME_LOCAL_ROOT/uv-cache"
        --env "UV_PROJECT_ENVIRONMENT=/opt/mcbots-venv"
        --env "MCBOTS_PROJECT_ROOT=$CONTAINER_ROOT"
        --env "MCBOTS_NAV_RUNTIME_TEMPLATE_ROOT=$CONTAINER_RUNTIME_LOCAL_ROOT"
        --env "MCBOTS_NAV_RUNTIME_BACKEND=apple-container-cpu"
        --env "MCBOTS_PORTABLEMC_LWJGL_VERSION=3.3.3"
        --env "MCBOTS_NAV_VNC_PORT=5900"
        --env "MCBOTS_NAV_NOVNC_PORT=6080"
        --env "PYTHONPATH=$CONTAINER_ROOT"
        --env "LIBGL_ALWAYS_SOFTWARE=1"
        "$IMAGE"
    )
    container "${run_args[@]}"

    echo "Container started: $CONTAINER_NAME"
    echo "noVNC during review: http://127.0.0.1:${NOVNC_HOST_PORT}/vnc.html?autoconnect=1&resize=scale"
}

prepare_runtime() {
    ensure_running
    container_exec uv run python \
        scripts/eval/prepare-navigation-runtime.py \
        --replace
}

run_review() {
    local task_id="${1:-}"
    [[ -n "$task_id" ]] || { echo "review requires TASK_ID" >&2; exit 2; }
    ensure_running
    echo "noVNC will be available at:"
    echo "  http://127.0.0.1:${NOVNC_HOST_PORT}/vnc.html?autoconnect=1&resize=scale"
    container exec \
        --interactive \
        --tty \
        --uid "$(id -u)" \
        --gid "$(id -g)" \
        --workdir "$CONTAINER_ROOT" \
        "$CONTAINER_NAME" \
        uv run python scripts/eval/run-navigation-benchmark.py \
        --mode review \
        --task "$task_id" \
        --vnc
}

run_model_mode() {
    local mode="$1"
    shift
    local selector="${1:-}"
    [[ -n "$selector" ]] || { echo "$mode requires TASK_ID or --all" >&2; exit 2; }
    shift
    ensure_running
    local task_args=()
    if [[ "$selector" == "--all" ]]; then
        task_args+=(--all)
    else
        task_args+=(--task "$selector")
    fi
    container exec \
        --interactive \
        --tty \
        --uid "$(id -u)" \
        --gid "$(id -g)" \
        --workdir "$CONTAINER_ROOT" \
        "$CONTAINER_NAME" \
        uv run python scripts/eval/run-navigation-benchmark.py \
        --mode "$mode" \
        "${task_args[@]}" \
        "$@"
}

main() {
    local command="${1:-}"
    shift || true
    case "$command" in
        install-runtime)
            install_runtime
            ;;
        build)
            build_image
            ;;
        start)
            start_container
            ;;
        prepare-runtime)
            prepare_runtime
            ;;
        review)
            run_review "$@"
            ;;
        pilot)
            run_model_mode pilot "$@"
            ;;
        formal)
            run_model_mode formal "$@"
            ;;
        shell)
            ensure_running
            container exec \
                --interactive \
                --tty \
                --uid "$(id -u)" \
                --gid "$(id -g)" \
                --workdir "$CONTAINER_ROOT" \
                "$CONTAINER_NAME" \
                bash
            ;;
        exec)
            [[ $# -gt 0 ]] || { echo "exec requires a command" >&2; exit 2; }
            ensure_running
            container_exec "$@"
            ;;
        status)
            ensure_system
            container system version
            container list --all
            ;;
        stop)
            ensure_system
            if container_exists && container_running; then
                container stop "$CONTAINER_NAME"
            fi
            ;;
        delete)
            ensure_system
            if container_exists; then
                container delete --force "$CONTAINER_NAME"
            fi
            ;;
        -h|--help|help|"")
            usage
            ;;
        *)
            echo "Unknown command: $command" >&2
            usage >&2
            exit 2
            ;;
    esac
}

main "$@"
