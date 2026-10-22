#!/usr/bin/env python3
"""The pack's default GPU check for transcribe method auto.

Prints one JSON line and exits 0: {"gpu": true} when the GPU (a single-GPU host; several GPUs answer unknown) has at least
GPU_MIN_FREE_MB MiB free (default 2048), {"gpu": false, "reason":
"vram-low"} when it has less, and {"gpu": true, "reason": "unknown"} when it
cannot tell, so the GPU is tried and a failing GPU run is retried on the
CPU. It tells from nvidia-smi only when that is found on PATH, runs nothing
else and contacts nothing. Standard library only, well inside 15 s.
nvidia-smi runs in its own process group with a timeout, a capped output
and a process-group kill on overrun, like every pack subprocess.
"""
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time

NVIDIA_SMI = ['nvidia-smi', '--query-gpu=memory.used,memory.total', '--format=csv,noheader,nounits']
NVIDIA_SMI_TIMEOUT = 5
NVIDIA_SMI_MAX_BYTES = 4 * 1024
GPU_MIN_FREE_MB = 2048


def _min_free_mb(environ=None):
    env = os.environ if environ is None else environ
    try:
        number = int(env.get('GPU_MIN_FREE_MB'))
    except (TypeError, ValueError):
        return GPU_MIN_FREE_MB
    return number if number > 0 else GPU_MIN_FREE_MB


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait()


def _nvidia_smi_stdout(popen=subprocess.Popen):
    """nvidia-smi's stdout bytes, or None when it cannot be read in time.

    At most NVIDIA_SMI_TIMEOUT seconds and NVIDIA_SMI_MAX_BYTES of stdout;
    on timeout or overrun its whole process group is killed, so a faulty
    nvidia-smi can neither fill memory nor leave descendants behind."""
    try:
        proc = popen(NVIDIA_SMI, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    out, deadline = bytearray(), time.monotonic() + NVIDIA_SMI_TIMEOUT
    selector = selectors.DefaultSelector()
    try:
        selector.register(proc.stdout, selectors.EVENT_READ)
        eof = False
        while not eof:
            if len(out) > NVIDIA_SMI_MAX_BYTES:
                _kill_group(proc)
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                _kill_group(proc)
                return None
            chunk = os.read(proc.stdout.fileno(), NVIDIA_SMI_MAX_BYTES + 1 - len(out))
            if chunk:
                out += chunk
            else:
                eof = True
        try:
            proc.wait(timeout=max(deadline - time.monotonic(), 0))
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            return None
    finally:
        selector.close()
        if proc.stdout is not None:
            proc.stdout.close()
    return bytes(out) if proc.returncode == 0 else None


def answer(environ=None, which=shutil.which, popen=subprocess.Popen):
    """The check's answer object for the current host."""
    if which('nvidia-smi') is None:
        return {'gpu': True, 'reason': 'unknown'}
    stdout = _nvidia_smi_stdout(popen)
    if stdout is None:
        return {'gpu': True, 'reason': 'unknown'}
    try:
        text = stdout.decode('utf-8')
    except UnicodeError:
        return {'gpu': True, 'reason': 'unknown'}
    lines = text.strip().splitlines()
    match = re.fullmatch(r'([0-9]{1,9}), ([0-9]{1,9})', lines[0].strip()) if len(lines) == 1 else None
    if match is None or int(match.group(1)) > int(match.group(2)):
        return {'gpu': True, 'reason': 'unknown'}
    free_mb = int(match.group(2)) - int(match.group(1))
    if free_mb >= _min_free_mb(environ):
        return {'gpu': True}
    return {'gpu': False, 'reason': 'vram-low'}


def main():
    print(json.dumps(answer()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
