#!/usr/bin/env python3
"""The GPU probe's side inside the bridge unit's sandbox
(bridge.py check-install --gpu-probe runs it through systemd-run).

Usage: gpu_probe.py MODEL < CLIP
Copies the clip from stdin into a private temporary directory (TMPDIR, the
unit's PrivateTmp), transcribes it with voice_transcribe and the
VOICE_TRANSCRIBE_* environment (VOICE_TRANSCRIBE_GPU=1 from the probe), and
prints, like the adapter, {"state": "running"} when its turn in the host-wide
queue starts, then one JSON line: {"device": <label>} or {"error": <code>},
with "device" once whisper ran and "seconds" (the run, from the turn's
start) once the turn started. The transcript itself is never printed.
Exits non-zero only on a usage error or a crash.
"""
import json
import os
import sys
import tempfile
import time

sys.dont_write_bytecode = True  # the pack directory is read-only under the unit
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voice_transcribe  # noqa: E402

CLIP_MAX_BYTES = 25 * 1024 * 1024


def read_clip(stream, limit=CLIP_MAX_BYTES):
    """The clip's bytes, or None when it is empty or over the limit."""
    chunks, size = [], 0
    while size <= limit:
        chunk = stream.read(min(1 << 20, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b''.join(chunks) if 0 < size <= limit else None


def main(argv, stdin=None, transcribe=voice_transcribe.transcribe, clock=time.monotonic):
    if len(argv) != 2:
        print('usage: gpu_probe.py MODEL < CLIP', file=sys.stderr)
        return 2
    clip = read_clip(stdin or sys.stdin.buffer)
    if clip is None:
        print(json.dumps({'error': 'clip_unreadable'}))
        return 0
    started, device = {}, {}

    def running():
        started['at'] = clock()
        print(voice_transcribe.RUNNING_LINE, flush=True)

    options = voice_transcribe.Options(model=argv[1])
    with tempfile.TemporaryDirectory(prefix='gpu-probe.') as tmp:
        path = os.path.join(tmp, 'clip')
        with open(path, 'wb') as stream:
            stream.write(clip)
        try:
            transcribe(path, options, on_turn=running, on_device=lambda label: device.update(device=label))
            result = {}
        except voice_transcribe.TranscribeError as error:
            result = {'error': error.code}
    if 'at' in started:
        result['seconds'] = round(clock() - started['at'], 1)
    print(json.dumps(dict(result, **device)))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
