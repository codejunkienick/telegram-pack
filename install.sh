#!/bin/sh
# Install one city's Telegram bridge from this pack, idempotently:
#   install.sh --settings <file> [--dry-run]
# 1. checks the settings and the host (python, curl, owner-only credentials,
#    and with transcription on: ffmpeg, whisper-cli and the models — a listed
#    model's SHA-256 against transcriber/models.sha256, an unlisted local
#    transcribe model accepted with a warning and no hash check), creating
#    the callback secret and the host-wide transcription queue directory first;
# 2. links ~/.local/bin/voice-transcribe to this pack's transcriber;
# 3. renders the city's service file (its systemd user unit, or its
#    launchd plist on macOS) where bridge.py's service_file names it.
# It never starts, stops or restarts a unit and never contacts Telegram:
# the operator does that afterwards (README.md, "Adopt in a city").
set -eu
pack=$(CDPATH= cd -- "$(dirname "$0")" && pwd -P)
settings= dry_run=0
while [ $# -gt 0 ]; do
  case $1 in
    --settings) settings=${2:?--settings needs a file}; shift 2 ;;
    --settings=*) settings=${1#*=}; shift ;;
    --dry-run) dry_run=1; shift ;;
    *) echo "install.sh: unknown argument $1 (usage: install.sh --settings <file> [--dry-run])" >&2; exit 64 ;;
  esac
done
[ -n "$settings" ] || { echo "install.sh: --settings <file> is required" >&2; exit 64; }
bridge="$pack/bridge/bridge.py"

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || { echo 'install.sh: python3 on PATH must be Python 3.11 or later (python3 --version)' >&2; exit 1; }

# The settings are loaded once for the values this script needs;
# a settings error is the loader's one line on stderr and stops the
# script (set -eu) before anything is created.
settings_out=$(python3 -c '
import runpy, shlex, sys
try:
    m = runpy.run_path(sys.argv[1])
    s = m["load_settings"](sys.argv[2])
    print("queue_dir=" + shlex.quote(str(s.queue_dir) if s.transcribe else ""))
    print("secret=" + shlex.quote(str(s.callback_secret_file)))
    print("unit_name=" + shlex.quote(s.unit))
    print("service_file=" + shlex.quote(str(m["service_file"](s))))
    print("next_line=" + shlex.quote(str(m["service_next_line"](s, sys.argv[1]))))
except Exception as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(1)
' "$bridge" "$settings")
eval "$settings_out"

# The host-wide transcription queue every city's bridge shares; the unit
# cannot create it (its home is read-only), so it is made here.
if [ -n "$queue_dir" ] && [ ! -d "$queue_dir" ]; then
  if [ "$dry_run" = 1 ]; then
    echo "would create $queue_dir (mode 700)"
  else
    mkdir -p "$queue_dir" && chmod 700 "$queue_dir" && echo "created $queue_dir"
  fi
fi

# The callback secret: the unit may write only its state and lock
# directories, so a missing secret is minted here, owner-only, and never
# printed.
if [ ! -e "$secret" ] && [ ! -L "$secret" ]; then
  if [ "$dry_run" = 1 ]; then
    echo "would create callback secret $secret (mode 600)"
  else
    python3 -c 'import runpy, sys; m = runpy.run_path(sys.argv[1]); m["capability"](sys.argv[2])' "$bridge" "$secret" \
      && echo "created callback secret $secret (mode 600)"
  fi
fi

python3 "$bridge" --settings "$settings" check-install

unit_file=$service_file
unit_dir=$(dirname "$unit_file")
unit_text=$(python3 "$bridge" --settings "$settings" render-unit)
link="$HOME/.local/bin/voice-transcribe"
target="$pack/transcriber/voice_transcribe.py"

if [ -e "$unit_file" ] && [ "$(cat "$unit_file")" = "$unit_text" ]; then
  echo "unit $unit_file is current"
elif [ "$dry_run" = 1 ]; then
  echo "would write $unit_file:"; printf '%s\n' "$unit_text"
else
  mkdir -p "$unit_dir"
  tmp=$(mktemp "$unit_dir/.${unit_file##*/}.XXXXXX")
  printf '%s\n' "$unit_text" >"$tmp" && chmod 644 "$tmp" && mv -f "$tmp" "$unit_file"
  echo "wrote $unit_file"
fi

if [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ]; then
  echo "link $link is current"
elif [ -e "$link" ] && [ ! -L "$link" ]; then
  echo "install.sh: $link exists and is not a link; left unchanged" >&2; exit 1
elif [ "$dry_run" = 1 ]; then
  echo "would link $link -> $target"
else
  mkdir -p "$(dirname "$link")" && ln -sfn "$target" "$link"
  echo "linked $link -> $target"
fi

echo "$next_line"
