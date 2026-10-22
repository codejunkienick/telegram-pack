#!/bin/sh
# The pack's fake-only test suite: no network, no real settings, no unit.
set -eu
cd "$(dirname "$0")/.."
exec python3 -B -m unittest discover -s bridge "$@"
