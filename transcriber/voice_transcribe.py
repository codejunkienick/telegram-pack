#!/usr/bin/env python3
"""voice-transcribe: speech to text for voice notes and audio files,
with whisper.cpp, on the GPU when it is free for it and on the CPU otherwise.
Writes <name>.txt next to each audio file and prints the text.

    voice-transcribe [--model NAME|PATH] [--force] [--quiet] <file|dir>...

Whisper decodes in the configured language (VOICE_TRANSCRIBE_LANGUAGE, a
whisper language code such as en or ru, or "auto" to detect it; en when
unset). Silero VAD runs first, so a note without speech gives no text
instead of a hallucinated subtitle line. A phrase repeated three or more
times in a row is retried once without text context and the result with
fewer repeats is kept. No transcript is refused because of its language
or script.

Settings come from the environment (the Telegram bridge passes its city's
settings this way):
  VOICE_TRANSCRIBE_WHISPER_CLI  whisper-cli binary (~/work/whisper.cpp/build/bin/whisper-cli)
  VOICE_TRANSCRIBE_MODEL_DIR    ggml models directory (~/work/whisper.cpp/models)
  VOICE_TRANSCRIBE_MODEL        model name or path (large-v3-turbo-q5_0)
  VOICE_TRANSCRIBE_VAD_MODEL    VAD model name or path (silero-v6.2.0)
  VOICE_TRANSCRIBE_LANGUAGE     whisper language code or "auto" (en)
  VOICE_TRANSCRIBE_THREADS      CPU threads, at most 64 (4)
  VOICE_TRANSCRIBE_CPUS         CPU list such as 0-3 (unset: every CPU the caller has)
  VOICE_TRANSCRIBE_TIMEOUT      run limit in seconds, before the per-minute allowance (120)
  VOICE_TRANSCRIBE_TIMEOUT_PER_MINUTE  seconds added per minute of audio (120)
  VOICE_TRANSCRIBE_GPU          1 = method auto, anything else = CPU only (unset; legacy, only without a method)
  VOICE_TRANSCRIBE_METHOD       auto, cpu or gpu (unset: the legacy VOICE_TRANSCRIBE_GPU rule)
  VOICE_TRANSCRIBE_GPU_CHECK    GPU check command for method auto (the pack's gpu_check.py beside this file)
  VOICE_TRANSCRIBE_GPU_MIN_FREE_MB  free video memory needed for the GPU, MiB (2048)

See bridge/OPERATIONS.md, "Voice-note transcription", for the queue, fence and device rules.
"""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time

AUDIO_RE = re.compile(r'\.(ogg|oga|opus|mp3|m4a|wav|flac|webm|mp4)$', re.IGNORECASE)
DEFAULT_LANGUAGE = 'en'
LANGUAGE_RE = re.compile(r'(?:auto|[a-z]{2,3})')
LOOP_MIN_REPEATS = 3
LOOP_MAX_PHRASE_WORDS = 16
MAX_THREADS = 64
DEFAULT_THREADS = 4
NICE = 10
IONICE = ['-c', '2', '-n', '7']
# The queue: one file per waiting or running transcription, named by its
# arrival number, each held under flock by its owner for as long as it lives.
TICKET_RE = re.compile(r'([0-9]{12})-([0-9]{1,10})\.ticket')
QUEUE_SUBDIR = '.local/state/voice-transcribe'
QUEUE_LOCK = 'queue.lock'
QUEUE_POLL = 0.25
# The line the adapter prints when its turn starts: the bridge's own run
# limit starts there, not while the note waits in the queue.
RUNNING_LINE = '{"state": "running"}'
CPU_LIST_RE = re.compile(r'[0-9]{1,4}(?:-[0-9]{1,4})?(?:,[0-9]{1,4}(?:-[0-9]{1,4})?)*')
GPU_MIN_FREE_MB = 2048
# A GPU run gets a quarter of the CPU run limit (at least 30 s) before it
# counts as failed and the CPU takes over; the bridge allows the same extra.
GPU_LIMIT_SHARE = 4
GPU_MIN_LIMIT = 30
# The GPU check for method auto: one command, at most 15 s, at most 4 KiB of
# stdout, one JSON object with a reason of lower-case letters, digits or '-'.
GPU_CHECK_TIMEOUT = 15
GPU_CHECK_MAX_BYTES = 4 * 1024
GPU_CHECK_REASON_RE = re.compile(r'[a-z0-9-]{1,32}')
GPU_CHECK_RESERVED_REASONS = frozenset({'gpu-failed', 'gpu-off', 'check-error'})
GPU_CHECK_PATH = '/usr/local/bin:/usr/bin:/bin'
PLATFORMS = ('linux', 'wsl', 'darwin')
PLATFORM_ENV = 'TELEGRAM_PACK_PLATFORM'


def host_platform(environ=None, system=None, osrelease='/proc/sys/kernel/osrelease'):
    """One of PLATFORMS for this host. TELEGRAM_PACK_PLATFORM is for tests
    only, like TELEGRAM_PACK_MODELS_SHA256: a value in PLATFORMS wins and
    any other value is ignored."""
    env = os.environ if environ is None else environ
    pinned = env.get(PLATFORM_ENV)
    if pinned in PLATFORMS:
        return pinned
    if (sys.platform if system is None else system) == 'darwin':
        return 'darwin'
    try:
        release = Path(osrelease).read_text()
    except (OSError, UnicodeDecodeError):
        return 'linux'
    return 'wsl' if 'microsoft' in release.lower() else 'linux'


class TranscribeError(Exception):
    """A failure with a fixed code the bridge maps to one relay phrase."""

    def __init__(self, code, detail=''):
        super().__init__(detail or code)
        self.code = code


class Options:
    def __init__(self, environ=None, model=None, gpu=None, method=None, gpu_check=None, language=None):
        env = os.environ if environ is None else environ
        home = Path.home()
        self.whisper_cli = Path(env.get('VOICE_TRANSCRIBE_WHISPER_CLI')
                                or home / 'work/whisper.cpp/build/bin/whisper-cli').expanduser()
        self.model_dir = Path(env.get('VOICE_TRANSCRIBE_MODEL_DIR') or home / 'work/whisper.cpp/models').expanduser()
        self.model = model or env.get('VOICE_TRANSCRIBE_MODEL') or 'large-v3-turbo-q5_0'
        self.vad_model = env.get('VOICE_TRANSCRIBE_VAD_MODEL') or 'silero-v6.2.0'
        self.language = language or env.get('VOICE_TRANSCRIBE_LANGUAGE') or DEFAULT_LANGUAGE
        self.threads = min(_positive(env.get('VOICE_TRANSCRIBE_THREADS'), DEFAULT_THREADS), MAX_THREADS)
        self.cpus = env.get('VOICE_TRANSCRIBE_CPUS') or None
        self.queue_dir = home / QUEUE_SUBDIR
        self.timeout = _positive(env.get('VOICE_TRANSCRIBE_TIMEOUT'), 120)
        self.timeout_per_minute = _positive(env.get('VOICE_TRANSCRIBE_TIMEOUT_PER_MINUTE'), 120)
        if method is not None:
            self.method = method
        elif env.get('VOICE_TRANSCRIBE_METHOD') in ('auto', 'cpu', 'gpu'):
            self.method = env['VOICE_TRANSCRIBE_METHOD']
        elif gpu is not None:
            self.method = 'auto' if gpu else 'cpu'
        else:
            # The legacy switch: gpu = true asks the GPU check (method auto).
            self.method = 'auto' if env.get('VOICE_TRANSCRIBE_GPU') == '1' else 'cpu'
        self.gpu = self.method in ('auto', 'gpu')
        self.gpu_check = (str(gpu_check) if gpu_check is not None
                          else env.get('VOICE_TRANSCRIBE_GPU_CHECK')
                          or str(Path(__file__).resolve().parent / 'gpu_check.py'))
        self.gpu_min_free_mb = _positive(env.get('VOICE_TRANSCRIBE_GPU_MIN_FREE_MB'), GPU_MIN_FREE_MB)

    def run_limit(self, audio_seconds):
        """Seconds one run may take: the base plus the allowance per minute."""
        return self.timeout + math.ceil(self.timeout_per_minute * audio_seconds / 60)

    def gpu_run_limit(self, audio_seconds):
        """Seconds a GPU run may take before the CPU takes over."""
        return max(GPU_MIN_LIMIT, math.ceil(self.run_limit(audio_seconds) / GPU_LIMIT_SHARE))

    def model_file(self, name):
        if '/' in name or name.endswith('.bin'):
            return Path(name).expanduser()
        return self.model_dir / f'ggml-{name}.bin'


def _positive(value, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def parse_cpus(text):
    """{0, 1, 2, 3} from a list such as '0-3'; ValueError when malformed."""
    if not CPU_LIST_RE.fullmatch(text):
        raise ValueError(f'bad CPU list {text!r}')
    cpus = set()
    for part in text.split(','):
        low, _, high = part.partition('-')
        low, high = int(low), int(high or low)
        if high < low:
            raise ValueError(f'bad CPU range {part!r}')
        cpus.update(range(low, high + 1))
    return cpus


def fence(options, run=subprocess.run):
    """Confine this process, and so ffmpeg and whisper-cli, to its CPUs at
    nice 10 and best-effort I/O level 7. An explicitly configured CPU list
    that cannot be applied is an error; nice and ionice are best-effort
    only and never fail the transcription. A caller already nicer than 10
    keeps its niceness."""
    try:
        if options.cpus:
            os.sched_setaffinity(0, parse_cpus(options.cpus))
    except (OSError, ValueError) as error:
        raise TranscribeError('fence_failed', f'CPU affinity {options.cpus}: {error}') from None
    try:
        if os.getpriority(os.PRIO_PROCESS, 0) < NICE:
            os.setpriority(os.PRIO_PROCESS, 0, NICE)
    except OSError:
        pass
    try:
        run(['ionice', *IONICE, '-p', str(os.getpid())], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def whisper_argv(options, wav, no_context=False, gpu=False):
    """The whisper-cli call: plain text in the configured language, Silero
    VAD; on the CPU (-ng) unless gpu, which is the same call without -ng."""
    argv = [str(options.whisper_cli), '-ng', '-t', str(options.threads), '-nt', '-np',
            '-l', getattr(options, 'language', DEFAULT_LANGUAGE), '-m', str(options.model_file(options.model)), '-f', str(wav),
            '--vad', '-vm', str(options.model_file(options.vad_model))]
    if gpu:
        argv.remove('-ng')
    if no_context:
        argv += ['-mc', '0']
    return argv


def _kill_check_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


def run_gpu_check(options, popen=subprocess.Popen):
    """Run the method-auto GPU check once and return (device, reason).

    argv is the check alone, no shell, empty stdin, its own directory as the
    working directory, and only PATH, HOME, LANG and GPU_MIN_FREE_MB in the
    environment. At most 15 s and 4 KiB of stdout; anything outside exactly
    one {"gpu": bool} or {"gpu": bool, "reason": "<1-32 of a-z 0-9 ->"}
    object with a reason that is not one of this module's own means the CPU
    with check-error. Nothing the check prints is returned beyond that."""
    check = str(Path(getattr(options, 'gpu_check', '') or '').expanduser())
    env = {'PATH': GPU_CHECK_PATH, 'HOME': os.environ.get('HOME') or str(Path.home()),
           'LANG': os.environ.get('LANG') or 'C.UTF-8',
           'GPU_MIN_FREE_MB': str(getattr(options, 'gpu_min_free_mb', GPU_MIN_FREE_MB))}
    try:
        proc = popen([check], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                     env=env, cwd=str(Path(check).parent or '.'), start_new_session=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 'cpu', 'check-error'
    out, deadline = bytearray(), time.monotonic() + GPU_CHECK_TIMEOUT
    selector = selectors.DefaultSelector()
    try:
        selector.register(proc.stdout, selectors.EVENT_READ)
        eof = False
        while not eof:
            if len(out) > GPU_CHECK_MAX_BYTES:
                _kill_check_group(proc)
                proc.wait()
                return 'cpu', 'check-error'
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_check_group(proc)
                proc.wait()
                return 'cpu', 'check-error'
            if not selector.select(remaining):
                _kill_check_group(proc)
                proc.wait()
                return 'cpu', 'check-error'
            chunk = os.read(proc.stdout.fileno(), GPU_CHECK_MAX_BYTES + 1 - len(out))
            if chunk:
                out += chunk
            else:
                eof = True
        try:
            proc.wait(timeout=max(deadline - time.monotonic(), 0))
        except subprocess.TimeoutExpired:
            _kill_check_group(proc)
            proc.wait()
            return 'cpu', 'check-error'
    finally:
        selector.close()
        if proc.stdout is not None:
            proc.stdout.close()
    if proc.returncode != 0 or len(out) > GPU_CHECK_MAX_BYTES:
        return 'cpu', 'check-error'
    try:
        answer = json.loads(bytes(out).decode('utf-8'))
    except (UnicodeError, ValueError):
        return 'cpu', 'check-error'
    if not isinstance(answer, dict) or set(answer) not in ({'gpu'}, {'gpu', 'reason'}) \
            or type(answer.get('gpu')) is not bool:
        return 'cpu', 'check-error'
    if 'reason' in answer:
        reason = answer['reason']
        if not isinstance(reason, str) or not GPU_CHECK_REASON_RE.fullmatch(reason) \
                or reason in GPU_CHECK_RESERVED_REASONS:
            return 'cpu', 'check-error'
    else:
        reason = None
    if answer['gpu']:
        return 'gpu', reason or 'gpu'
    return 'cpu', reason or 'busy'


def choose_device(options):
    """('gpu', 'gpu') for method gpu, the GPU check's answer for method
    auto, else ('cpu', reason)."""
    method = getattr(options, 'method', 'cpu')
    if method == 'gpu':
        return 'gpu', 'gpu'
    if method == 'auto':
        return run_gpu_check(options)
    return 'cpu', 'gpu-off'


class Device:
    """The device of one note: starts as choose_device() decided and only
    ever moves from the GPU to the CPU. label is its one log line."""

    def __init__(self, name, reason):
        self.name, self.reason = name, reason

    def to_cpu(self, reason):
        self.name, self.reason = 'cpu', reason

    @property
    def label(self):
        if self.name == 'gpu':
            return 'gpu' if self.reason == 'gpu' else f'gpu: {self.reason}'
        return f'cpu: {self.reason}'


def clean(text):
    return ' '.join(text.split())


def longest_repeat(text):
    """The most times one phrase of 2..16 words repeats back to back."""
    words = re.findall(r"[\w']+", text.lower())
    best = 1
    for size in range(2, min(LOOP_MAX_PHRASE_WORDS, len(words) // 2) + 1):
        for start in range(0, len(words) - size * 2 + 1):
            phrase, count, at = words[start:start + size], 1, start + size
            while words[at:at + size] == phrase:
                count, at = count + 1, at + size
            best = max(best, count)
    return best


def _run_whisper(argv, run, deadline, env=None):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TranscribeError('timeout', 'the run limit was reached')
    try:
        result = run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=remaining, env=env)
    except subprocess.TimeoutExpired:
        raise TranscribeError('timeout', 'whisper-cli was killed at the run limit') from None
    except (OSError, subprocess.SubprocessError):
        raise TranscribeError('whisper_failed') from None
    if result.returncode != 0:
        lines = (result.stderr or '').strip().splitlines()
        raise TranscribeError('whisper_failed', lines[-1][:200] if lines else '')
    text = clean(result.stdout)
    # whisper-cli exits 0 with an "error:" line and no output for a language
    # it does not know: that is a failure, not silence.
    errors = [line.strip() for line in (result.stderr or '').splitlines()
              if line.strip().lower().startswith('error:')]
    unknown = next((line for line in errors if 'unknown language' in line.lower()), None)
    if unknown is not None:
        raise TranscribeError('whisper_failed', unknown[:200])
    if errors and not text:
        raise TranscribeError('whisper_failed', errors[-1][:200])
    return text


class Queue:
    """The host-wide FIFO of transcriptions, in one directory every caller
    shares. Taking a ticket and scanning the queue happen under a short flock
    on queue.lock, so arrival numbers are strictly ordered. Each owner holds
    an flock on its own ticket while it waits and runs: a ticket nobody holds
    belongs to a process that died, and the next scan removes it. The head
    ticket runs; everyone else waits with no time limit."""

    def __init__(self, path, sleep=time.sleep, poll=QUEUE_POLL):
        self.path, self.sleep, self.poll = Path(path), sleep, poll
        self.dir_fd = self.lock_fd = self.ticket_fd = None
        self.ticket = None

    def __enter__(self):
        try:
            os.makedirs(self.path, mode=0o700, exist_ok=True)
            self.dir_fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            st = os.fstat(self.dir_fd)
            if st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise TranscribeError('queue_failed', f'queue directory must be owner-only: {self.path}')
            self.lock_fd = os.open(QUEUE_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                                   dir_fd=self.dir_fd)
            with self._locked():
                number = max((n for n, _ in self._tickets()), default=0) + 1
                self.ticket = f'{number:012d}-{os.getpid()}.ticket'
                self.ticket_fd = os.open(self.ticket, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                         0o600, dir_fd=self.dir_fd)
                fcntl.flock(self.ticket_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            while not self._my_turn():
                self.sleep(self.poll)
        except OSError as error:
            self.__exit__(None, None, None)
            raise TranscribeError('queue_failed', f'queue {self.path}: {error.strerror}') from None
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self.ticket_fd is not None and self.lock_fd is not None:
                with self._locked():
                    try:
                        os.unlink(self.ticket, dir_fd=self.dir_fd)
                    except FileNotFoundError:
                        pass
        finally:
            for fd in (self.ticket_fd, self.lock_fd, self.dir_fd):
                if fd is not None:
                    os.close(fd)
            self.dir_fd = self.lock_fd = self.ticket_fd = None
        return False

    class _Held:
        def __init__(self, fd):
            self.fd = fd

        def __enter__(self):
            fcntl.flock(self.fd, fcntl.LOCK_EX)

        def __exit__(self, *exc):
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            return False

    def _locked(self):
        return self._Held(self.lock_fd)

    def _tickets(self):
        found = []
        for name in os.listdir(self.dir_fd):
            match = TICKET_RE.fullmatch(name)
            if match:
                found.append((int(match.group(1)), name))
        return sorted(found)

    def _my_turn(self):
        """True when every ticket ahead of ours is gone; removes dead ones."""
        with self._locked():
            for _, name in self._tickets():
                if name == self.ticket:
                    return True
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.dir_fd)
                except FileNotFoundError:
                    continue
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False  # alive: waiting or running ahead of us
                finally:
                    os.close(fd)
                os.unlink(name, dir_fd=self.dir_fd)  # its owner died
        raise TranscribeError('queue_failed', 'our ticket left the queue')


def _whisper(options, wav, run, device, deadline, gpu_limit, no_context=False):
    """One whisper pass on the note's device. On the GPU: at most gpu_limit
    seconds, and any failure moves the note to the CPU for this pass and the
    rest of it. The CPU pass never sees a GPU (CUDA_VISIBLE_DEVICES empty)."""
    if device.name == 'gpu':
        try:
            return _run_whisper(whisper_argv(options, wav, no_context, gpu=True), run,
                                min(deadline, time.monotonic() + gpu_limit))
        except TranscribeError:
            device.to_cpu('gpu-failed')
    return _run_whisper(whisper_argv(options, wav, no_context), run, deadline,
                        env=dict(os.environ, CUDA_VISIBLE_DEVICES=''))


def _wav_seconds(path):
    """Seconds of a 16 kHz mono 16-bit WAV, from its size."""
    return max(os.path.getsize(path) - 44, 0) / 32000


def transcribe(audio, options, run=subprocess.run, on_turn=None, on_device=None):
    """Text of one audio file in the configured language ('' when it holds no speech), or
    TranscribeError. Waits for its turn in the host-wide queue, calls
    on_turn() when it starts, picks the device, and runs within
    options.run_limit() (plus the GPU allowance with options.gpu).
    on_device(label) gets the note's final device line once whisper ran."""
    if not (options.whisper_cli.is_file() and os.access(options.whisper_cli, os.X_OK)):
        raise TranscribeError('whisper_missing', f'whisper-cli not found: {options.whisper_cli}')
    for name in (options.model, options.vad_model):
        if not options.model_file(name).is_file():
            raise TranscribeError('model_missing', f'model not found: {options.model_file(name)}')
    fence(options)
    with Queue(options.queue_dir), tempfile.TemporaryDirectory(prefix='voice-transcribe.') as tmp:
        if on_turn is not None:
            on_turn()
        wav = Path(tmp) / 'in.wav'
        try:
            converted = run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-y', '-i', str(audio),
                             '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(wav)],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=options.timeout)
        except subprocess.TimeoutExpired:
            raise TranscribeError('timeout', 'ffmpeg was killed at the run limit') from None
        except (OSError, subprocess.SubprocessError):
            raise TranscribeError('decode_failed', 'ffmpeg could not run') from None
        if converted.returncode != 0:
            raise TranscribeError('decode_failed', 'ffmpeg could not decode it')
        seconds = _wav_seconds(wav)
        gpu_limit = options.gpu_run_limit(seconds)
        deadline = time.monotonic() + options.run_limit(seconds) + (gpu_limit if options.gpu else 0)
        device = Device(*choose_device(options))
        try:
            text = _whisper(options, wav, run, device, deadline, gpu_limit)
            if longest_repeat(text) >= LOOP_MIN_REPEATS:
                # A decoding loop: retry with no text context, keep the better one.
                try:
                    retry = _whisper(options, wav, run, device, deadline, gpu_limit, no_context=True)
                except TranscribeError as error:
                    if error.code != 'timeout':
                        raise
                    retry = text  # out of time: keep the first text
                if longest_repeat(retry) < longest_repeat(text):
                    text = retry
        finally:
            if on_device is not None:
                on_device(device.label)
    return text


def _audio_files(paths, force):
    todo = []
    for given in paths:
        path = Path(given)
        if path.is_dir():
            for found in sorted(path.rglob('*')):
                if found.is_file() and AUDIO_RE.search(found.name) and (force or not found.with_suffix('.txt').exists()):
                    todo.append(found)
        elif path.is_file():
            todo.append(path)
        else:
            raise SystemExit(f'voice-transcribe: no such file or directory: {given}')
    return todo


def _write_text(path, text):
    part = path.with_name(path.name + '.part')
    part.write_text(text + '\n')
    os.replace(part, path)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='voice-transcribe', description=__doc__.split('\n\n')[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument('--model', help='model name (ggml-NAME.bin in the models directory) or a path')
    parser.add_argument('--force', action='store_true', help='transcribe again even if the .txt exists')
    parser.add_argument('-q', '--quiet', action='store_true', help='print only the text')
    parser.add_argument('paths', nargs='+', metavar='file|dir')
    args = parser.parse_args(argv)
    options = Options(model=args.model)
    todo = _audio_files(args.paths, args.force)
    status = 0
    for audio in todo:
        started = time.monotonic()
        try:
            text = transcribe(audio, options,
                              on_device=lambda label, audio=audio: print(f'voice-transcribe: {audio}: device {label}',
                                                                         file=sys.stderr, flush=True))
        except TranscribeError as error:
            print(f'voice-transcribe: {audio}: {error}', file=sys.stderr)
            status = 1
            continue
        out = audio.with_suffix('.txt')
        _write_text(out, text)
        if args.quiet:
            print(text)
        else:
            seconds = time.monotonic() - started
            print(f'== {audio} ({options.language}, {options.model}, {seconds:.1f} s) -> {out}\n{text or "(no speech)"}')
    return status


if __name__ == '__main__':
    sys.exit(main())
