#!/bin/sh
# Set up the transcriber's host side for one city's settings, idempotently:
#   transcriber/setup.sh --settings <file> [--dry-run] [--whisper-ref <ref>]
# Builds whisper.cpp for the settings' method, then downloads the chosen
# model and the VAD model and checks listed models against
# transcriber/models.sha256 (a present, non-empty unlisted transcribe model
# file is reused as-is; the VAD model must be listed),
# so install.sh can say ok without guessing. The whisper.cpp build and the
# models are shared by every city on the host (never under a city). It
# installs nothing system-wide, uses no sudo and starts nothing; a missing
# prerequisite is reported, never installed. It prints one ok/FAIL/would
# line per step like install.sh, and never prints a settings value other
# than the paths it acts on; it never reads the token or secret files.
# WHISPER_REF below is the known-good whisper.cpp commit; --whisper-ref
# takes a tag or a commit instead. The model list comes from the file
# named by TELEGRAM_PACK_MODELS_SHA256 when that is set (for tests only),
# else from transcriber/models.sha256 beside this script's pack.
set -u
tmpfile=
_cleanup_tmp() { if [ -n "$tmpfile" ]; then rm -f "$tmpfile"; fi; }
trap _cleanup_tmp EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# v1.9.5 (2026-10-06), the build the pack is tested with; it builds with
# CUDA 13.4, which the earlier pin v1.8.3-120-g764482c3 does not.
WHISPER_REF=d1be6fde11ac6e0407606b4e42fe72d34add8037
pack=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd -P)
bridge="$pack/bridge/bridge.py"
sums_file=${TELEGRAM_PACK_MODELS_SHA256:-"$pack/transcriber/models.sha256"}
usage() {
  echo "usage: transcriber/setup.sh --settings <file> [--dry-run] [--whisper-ref <ref>]" >&2
  exit 64
}
settings= dry_run=0 whisper_ref=$WHISPER_REF whisper_ref_given=0
while [ $# -gt 0 ]; do
  case $1 in
    --settings) [ $# -ge 2 ] || { echo "setup.sh: --settings needs a file" >&2; usage; }; settings=$2; shift 2 ;;
    --settings=*) settings=${1#*=}; shift ;;
    --dry-run) dry_run=1; shift ;;
    --whisper-ref) [ $# -ge 2 ] || { echo "setup.sh: --whisper-ref needs a ref" >&2; usage; }; whisper_ref=$2; whisper_ref_given=1; shift 2 ;;
    --whisper-ref=*) whisper_ref=${1#*=}; whisper_ref_given=1; shift ;;
    *) echo "setup.sh: unknown argument $1" >&2; usage ;;
  esac
done
[ -n "$settings" ] || { echo "setup.sh: --settings <file> is required" >&2; usage; }
[ -n "$whisper_ref" ] || { echo "setup.sh: --whisper-ref needs a ref" >&2; usage; }
if [ -n "${TELEGRAM_PACK_MODELS_SHA256:-}" ]; then
  printf 'note  checksum list from TELEGRAM_PACK_MODELS_SHA256: %s\n' "$sums_file"
fi

# Read the transcribe paths and method the way install.sh does, through
# the bridge's own settings loader. It validates the file and reads no
# credential file's contents.
settings_out=$(python3 -c '
import runpy, shlex, sys
try:
    bridge_ns = runpy.run_path(sys.argv[1])
    s = bridge_ns["load_settings"](sys.argv[2])
except Exception as exc:
    print("FAIL  " + str(exc), file=sys.stderr)
    sys.exit(1)
enabled = "1" if s.transcribe is not None else "0"
if s.transcribe is not None:
    method = s.transcribe["method"]
else:
    method = "cpu"
try:
    sums = bridge_ns["recorded_sums"](sys.argv[3])
except Exception:
    sums = {}
print("enabled=" + shlex.quote(enabled))
print("whisper_cli=" + shlex.quote(str(s.whisper_cli)))
print("models_dir=" + shlex.quote(str(s.models_dir)))
print("model=" + shlex.quote(str(s.model)))
print("vad_model=" + shlex.quote(str(s.vad_model)))
print("method=" + shlex.quote(str(method)))
print("model_sum=" + shlex.quote(sums.get("ggml-" + str(s.model) + ".bin", "")))
print("vad_model_sum=" + shlex.quote(sums.get("ggml-" + str(s.vad_model) + ".bin", "")))
' "$bridge" "$settings" "$sums_file") || exit 1
eval "$settings_out"

if [ "$enabled" != 1 ]; then
  printf 'skip  transcription is off in %s\n' "$settings"
  exit 0
fi

# Platform: TELEGRAM_PACK_PLATFORM (tests only) when set, else uname;
# only darwin differs, everything else (WSL included) is linux.
os=${TELEGRAM_PACK_PLATFORM:-}
if [ -z "$os" ]; then
  case $(uname -s 2>/dev/null) in
    Darwin) os=darwin ;;
    *) os=linux ;;
  esac
fi
hash_cmd=sha256sum

# Prerequisites: report every missing tool, then fail before any change.
missing=0
if command -v git >/dev/null 2>&1; then printf 'ok    git\n'; else printf 'FAIL  git not found (install it with your package manager)\n'; missing=$((missing + 1)); fi
if command -v cmake >/dev/null 2>&1; then printf 'ok    cmake\n'; else printf 'FAIL  cmake not found (install it with your package manager)\n'; missing=$((missing + 1)); fi
cmake_generator=
if command -v make >/dev/null 2>&1; then printf 'ok    make\n'; elif command -v ninja >/dev/null 2>&1; then printf 'ok    ninja\n'; cmake_generator=Ninja; else printf 'FAIL  make not found (install it with your package manager)\n'; missing=$((missing + 1)); fi
if command -v c++ >/dev/null 2>&1; then printf 'ok    c++\n'; elif command -v g++ >/dev/null 2>&1; then printf 'ok    g++\n'; elif command -v clang++ >/dev/null 2>&1; then printf 'ok    clang++\n'; else printf 'FAIL  c++ not found (install it with your package manager)\n'; missing=$((missing + 1)); fi
if command -v ffmpeg >/dev/null 2>&1; then printf 'ok    ffmpeg\n'; else printf 'FAIL  ffmpeg not found (install it with your package manager)\n'; missing=$((missing + 1)); fi
if command -v curl >/dev/null 2>&1; then printf 'ok    curl\n'; else printf 'FAIL  curl not found (install it with your package manager)\n'; missing=$((missing + 1)); fi
if [ "$os" = darwin ]; then
  if command -v sha256sum >/dev/null 2>&1; then printf 'ok    sha256sum\n'; elif command -v shasum >/dev/null 2>&1; then printf 'ok    shasum -a 256\n'; hash_cmd=shasum; else printf 'FAIL  sha256sum not found (install it with your package manager)\n'; missing=$((missing + 1)); fi
else
  if command -v sha256sum >/dev/null 2>&1; then printf 'ok    sha256sum\n'; else printf 'FAIL  sha256sum not found (install it with your package manager)\n'; missing=$((missing + 1)); fi
fi
# nvcc is no prerequisite: without it a gpu/auto build is a CPU build.
if [ "$missing" -ne 0 ]; then exit 1; fi

# The checkout is the directory the whisper_cli layout names:
# <checkout>/build/bin/whisper-cli.
case $whisper_cli in
  */build/bin/*) checkout=${whisper_cli%/build/bin/*}; [ -n "$checkout" ] || checkout=/ ;;
  *) checkout=$(dirname "$(dirname "$whisper_cli")") ;;
esac

if [ -d "$checkout" ]; then
  describe=$(git -C "$checkout" describe --tags --always 2>/dev/null) || describe=unknown
  [ -n "$describe" ] || describe=unknown
  printf 'ok    whisper.cpp at %s (%s)\n' "$checkout" "$describe"
elif [ "$dry_run" = 1 ]; then
  printf 'would clone whisper.cpp to %s at %s\n' "$checkout" "$whisper_ref"
else
  mkdir -p "$(dirname "$checkout")"
  if ! git clone https://github.com/ggml-org/whisper.cpp "$checkout"; then
    printf 'FAIL  whisper.cpp clone to %s failed\n' "$checkout"
    exit 1
  fi
  if ! git -C "$checkout" checkout --detach "$whisper_ref"; then
    rm -rf "$checkout"
    printf 'FAIL  whisper.cpp checkout %s at %s failed\n' "$whisper_ref" "$checkout"
    exit 1
  fi
  if [ "$whisper_ref_given" = 0 ]; then
    head=$(git -C "$checkout" rev-parse HEAD 2>/dev/null) || head=
    if [ "$head" != "$WHISPER_REF" ]; then
      rm -rf "$checkout"
      printf 'FAIL  whisper.cpp at %s is %s, not the pinned %s\n' "$checkout" "$head" "$WHISPER_REF"
      exit 1
    fi
  fi
  describe=$(git -C "$checkout" describe --tags --always 2>/dev/null) || describe=$whisper_ref
  [ -n "$describe" ] || describe=$whisper_ref
  printf 'ok    whisper.cpp at %s (%s)\n' "$checkout" "$describe"
fi

# Build only when whisper_cli is missing. CUDA only for gpu/auto with
# nvcc on PATH, and never on darwin (Metal is the default there);
# without nvcc the build is for the CPU.
want_cuda=0
if [ "$os" != darwin ]; then
  if [ "$method" = gpu ] || [ "$method" = auto ]; then
    if command -v nvcc >/dev/null 2>&1; then want_cuda=1; fi
  fi
fi
if [ -f "$whisper_cli" ] && [ -x "$whisper_cli" ]; then
  printf 'ok    whisper-cli at %s\n' "$whisper_cli"
elif [ "$dry_run" = 1 ]; then
  if [ "$want_cuda" = 1 ]; then
    printf 'would build whisper-cli at %s with CUDA (-DGGML_CUDA=1)\n' "$whisper_cli"
  elif [ "$os" = darwin ] && { [ "$method" = gpu ] || [ "$method" = auto ]; }; then
    printf 'would build whisper-cli at %s with Metal (whisper.cpp'\''s default on macOS)\n' "$whisper_cli"
  elif [ "$method" = gpu ] || [ "$method" = auto ]; then
    printf 'would build whisper-cli at %s for the CPU (no nvcc on PATH; the GPU is tried and falls back to the CPU)\n' "$whisper_cli"
  else
    printf 'would build whisper-cli at %s\n' "$whisper_cli"
  fi
else
  set -- -B build
  if [ "$want_cuda" = 1 ]; then set -- "$@" -DGGML_CUDA=1; fi
  if [ -n "$cmake_generator" ]; then set -- "$@" -G "$cmake_generator"; fi
  if ! (cd "$checkout" && cmake "$@"); then
    printf 'FAIL  whisper-cli configure in %s failed\n' "$checkout"
    exit 1
  fi
  if ! (cd "$checkout" && cmake --build build -j --config Release --target whisper-cli); then
    printf 'FAIL  whisper-cli build in %s failed\n' "$checkout"
    exit 1
  fi
  if [ ! -f "$whisper_cli" ] || [ ! -x "$whisper_cli" ]; then
    printf 'FAIL  whisper-cli build did not produce %s\n' "$whisper_cli"
    exit 1
  fi
  if [ "$want_cuda" = 1 ]; then
    printf 'ok    whisper-cli built with CUDA at %s\n' "$whisper_cli"
  elif [ "$os" = darwin ] && { [ "$method" = gpu ] || [ "$method" = auto ]; }; then
    printf 'ok    whisper-cli built with Metal at %s\n' "$whisper_cli"
  elif [ "$method" = gpu ] || [ "$method" = auto ]; then
    printf 'ok    whisper-cli built for the CPU (no nvcc on PATH; the GPU is tried and falls back to the CPU)\n'
  else
    printf 'ok    whisper-cli built at %s\n' "$whisper_cli"
  fi
fi

# Models: check the recorded SHA-256, download only what is missing,
# never overwrite a file that is already there.
file_hash() {
  if [ "$hash_cmd" = shasum ]; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    sha256sum "$1" | cut -d' ' -f1
  fi
}
setup_model() {
  _name=$1
  _url=$2
  _expected=$3
  _allow_local=${4:-}
  _file="ggml-$_name.bin"
  _dest="$models_dir/$_file"
  if [ -z "$_expected" ] && [ "$_allow_local" = 1 ] && [ -f "$_dest" ] && [ -s "$_dest" ]; then
    printf 'ok    model %s (local file, no recorded SHA-256; skipping hash check)\n' "$_dest"
    return 0
  fi
  if [ -z "$_expected" ]; then
    printf 'FAIL  model %s has no recorded SHA-256 in %s\n' "$_dest" "$sums_file"
    return 1
  fi
  if [ -f "$_dest" ]; then
    _actual=$(file_hash "$_dest")
    if [ "$_actual" = "$_expected" ]; then
      printf 'ok    model %s SHA-256\n' "$_dest"
      return 0
    fi
    printf 'FAIL  model %s SHA-256 mismatch\n' "$_dest"
    return 1
  fi
  if [ "$dry_run" = 1 ]; then
    printf 'would download %s from %s\n' "$_dest" "$_url"
    return 0
  fi
  if [ ! -d "$models_dir" ]; then
    mkdir -p "$models_dir" || { printf 'FAIL  models directory %s could not be created\n' "$models_dir"; return 1; }
  fi
  _tmp=$(mktemp "$models_dir/.ggml-XXXXXXXX") || { printf 'FAIL  model %s download failed\n' "$_dest"; return 1; }
  tmpfile=$_tmp
  if ! curl -q -fL --proto =https -o "$_tmp" "$_url"; then
    rm -f "$_tmp"
    tmpfile=
    printf 'FAIL  model %s download failed\n' "$_dest"
    return 1
  fi
  _actual=$(file_hash "$_tmp")
  if [ "$_actual" != "$_expected" ]; then
    rm -f "$_tmp"
    tmpfile=
    printf 'FAIL  model %s SHA-256 mismatch\n' "$_dest"
    return 1
  fi
  if ! mv "$_tmp" "$_dest"; then
    rm -f "$_tmp"
    tmpfile=
    printf 'FAIL  model %s could not be moved into place\n' "$_dest"
    return 1
  fi
  tmpfile=
  printf 'ok    model %s downloaded (SHA-256)\n' "$_dest"
  return 0
}
setup_model "$model" "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-$model.bin" "$model_sum" 1 || exit 1
setup_model "$vad_model" "https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-$vad_model.bin" "$vad_model_sum" || exit 1

printf 'next: %s/install.sh --settings %s\n' "$pack" "$settings"
