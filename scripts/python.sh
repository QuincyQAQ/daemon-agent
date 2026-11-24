#!/usr/bin/env bash
# Run a repository command with a supported interpreter.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
for candidate in "${FOLDJAY_PYTHON:-${BEAR_PYTHON:-}}" "$project_dir/.venv/bin/python" python3 python3.13 python3.12 python3.11 python3.10 /usr/bin/python3 python; do
    if [[ -n "$candidate" ]] && command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        exec "$candidate" "$@"
    fi
done
echo "Python 3.10+ is required. Set FOLDJAY_PYTHON to a supported interpreter." >&2
exit 1
