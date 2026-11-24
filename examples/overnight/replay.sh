#!/usr/bin/env bash
# Replay the public fixture in the fresh Slurm output directory. No model or GPU is required.
set -eu
cp -R -- "$DAEMON_INPUT_DIR/A" "$DAEMON_INPUT_DIR/B" .
