#!/bin/sh
set -eu
city=${GC_CITY_PATH:-${GC_CITY:-}}
pack=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd -P)
if [ -z "$city" ]; then
  city=$(pwd -P)
fi
exec python3 "$pack/bridge/bridge.py" --city-root "$city" reply-current "$@"
