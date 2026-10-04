#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Family-agnostic core implementation.
exec "$SCRIPT_DIR/run-openha-eval-core-inside.sh" "$@"
