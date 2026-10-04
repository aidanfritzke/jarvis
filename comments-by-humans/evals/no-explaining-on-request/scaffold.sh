#!/usr/bin/env bash
# Builds this case's workspace through the gate's own hooks (see ../_lib/scaffold.py).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$HERE/../_lib/scaffold.py" --fixture "$HERE/fixture" --depth strict
