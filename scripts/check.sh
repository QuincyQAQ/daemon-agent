#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
./scripts/python.sh -m compileall -q foldjay examples tests
./scripts/python.sh -m unittest discover -s tests -v
./daemon-agent.sh doctor
