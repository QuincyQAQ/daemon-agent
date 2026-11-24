#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [ "$#" -eq 0 ]; then
    set -- run
fi
exec "$project_dir/bear.sh" --profile "$project_dir/.bear/campus.json" "$@"
