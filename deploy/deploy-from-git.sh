#!/usr/bin/env bash
# Backward-compatible entry point; release.py defaults to read-only preflight.
set -e
cd "$(dirname "$0")/.."
exec python deploy/release.py "$@"
