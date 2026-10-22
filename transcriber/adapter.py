#!/usr/bin/env python3
"""The bridge's transcriber contract over voice_transcribe.

Usage: adapter.py AUDIO MODEL DEVICE
Prints {"state": "running"} when its turn in the host-wide queue starts,
then, last, one JSON line: {"text": ...} ("" when the note holds no speech),
or {"error": <code>} for a known failure (see TRANSCRIBER_ERRORS in the
bridge), with "device": <label> (gpu, or cpu: <reason>) once whisper ran.
Exits non-zero only on a usage error or a crash. DEVICE is cpu (CPU only),
auto or check (method auto: the configured GPU check decides; auto is the
legacy name for the same path) or gpu (method gpu: the GPU, no check);
the paths and limits come from the VOICE_TRANSCRIBE_* environment.
"""
import json
import os
import sys

sys.dont_write_bytecode = True  # the pack directory is read-only under the unit
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voice_transcribe  # noqa: E402


def main(argv):
    if len(argv) != 4 or argv[3] not in ('cpu', 'auto', 'check', 'gpu'):
        print('usage: adapter.py AUDIO MODEL cpu|auto|check|gpu', file=sys.stderr)
        return 2
    def running():
        # The turn started: from here the bridge's run limit applies.
        print(voice_transcribe.RUNNING_LINE, flush=True)

    device = {}
    method = {'cpu': 'cpu', 'auto': 'auto', 'check': 'auto', 'gpu': 'gpu'}[argv[3]]
    options = voice_transcribe.Options(model=argv[2], method=method)
    try:
        result = {'text': voice_transcribe.transcribe(argv[1], options, on_turn=running,
                                                      on_device=lambda label: device.update(device=label))}
    except voice_transcribe.TranscribeError as error:
        result = {'error': error.code}
    print(json.dumps(dict(result, **device)))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
