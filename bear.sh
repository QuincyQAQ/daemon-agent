#!/usr/bin/env bash
# Compatibility entry point for existing checkouts.
exec "$(dirname -- "${BASH_SOURCE[0]}")/foldjay.sh" "$@"
