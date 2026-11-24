#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [ "$#" -eq 0 ]; then
    set -- run
fi
state_dir="$project_dir/.foldjay"
if [[ ! -e "$state_dir" && -d "$project_dir/.bear" ]]; then
    state_dir="$project_dir/.bear"
fi
exec "$project_dir/daemon-agent.sh" --profile "$state_dir/campus.json" "$@"
