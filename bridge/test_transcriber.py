"""The whisper.cpp transcriber and its bridge adapter, with a fake whisper-cli
and a fake ffmpeg: the configured language is passed to whisper, VAD is on,
loops are retried, no text is refused for its language or script, the adapter
speaks the bridge's JSON-line contract, every caller on the host waits its turn
in one FIFO queue, and the GPU is used only when the GPU check allows it."""
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PACK = Path(__file__).resolve().parents[1]
TRANSCRIBER = PACK / 'transcriber'
spec = importlib.util.spec_from_file_location('voice_transcribe', TRANSCRIBER / 'voice_transcribe.py')
vt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vt)
gpu_spec = importlib.util.spec_from_file_location('gpu_check', TRANSCRIBER / 'gpu_check.py')
gpu_check = importlib.util.module_from_spec(gpu_spec)
gpu_spec.loader.exec_module(gpu_check)

# A fake whisper-cli: records its argv as one JSON line, then prints the next
# queued answer (one per call; the last repeats), or exits 1 on "FAIL".
FAKE_WHISPER = """#!{python}
import json, os, sys
log = os.environ['FAKE_WHISPER_LOG']
try:
    _ionice = __import__('subprocess').run(['ionice', '-p', str(os.getpid())], capture_output=True, text=True).stdout.strip()
except Exception:
    _ionice = ''
fence = dict(cpus=sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None, nice=os.getpriority(os.PRIO_PROCESS, 0),
             ionice=_ionice)
with open(os.environ['FAKE_WHISPER_LOG'] + '.fence', 'w') as stream:
    json.dump(fence, stream)
with open(log, 'a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
with open(log + '.cuda', 'a') as stream:
    stream.write(json.dumps(os.environ.get('CUDA_VISIBLE_DEVICES')) + '\\n')
started = __import__('time').time()
__import__('time').sleep(float(os.environ.get('FAKE_WHISPER_SLEEP', '0')))
if os.environ.get('FAKE_WHISPER_TIMES'):
    with open(os.environ['FAKE_WHISPER_TIMES'], 'a') as stream:
        stream.write(json.dumps(dict(tag=os.environ['FAKE_WHISPER_TAG'], start=started,
                                     end=__import__('time').time())) + '\\n')
answers = json.loads(os.environ['FAKE_WHISPER_ANSWERS'])
calls = sum(1 for _ in open(log))
answer = answers[min(calls, len(answers)) - 1]
if answer == 'FAIL':
    print('whisper: model load failed', file=sys.stderr)
    sys.exit(1)
if answer == 'UNKNOWN_LANGUAGE':
    print("error: unknown language 'zz'", file=sys.stderr)
    sys.exit(0)
print(answer)
"""
# A fake ffmpeg: writes the output file (its last argument), or fails on "bad" input.
FAKE_FFMPEG = """#!/bin/sh
for last; do :; done
case "$*" in *bad.ogg*) echo 'decode error' >&2; exit 1 ;; esac
printf 'RIFF' >"$last"
"""


def _recording_popen(captured, payload=b'{"gpu": false}\n', returncode=0):
    """A fake Popen factory: records (argv, kwargs) and returns a process
    whose stdout is a pipe already holding payload and which exits with
    returncode."""
    def popen(argv, **kwargs):
        captured.append((argv, kwargs))
        read_fd, write_fd = os.pipe()
        if payload:
            os.write(write_fd, payload)
        os.close(write_fd)
        stdout = os.fdopen(read_fd, 'rb')

        class _Proc:
            def __init__(self):
                self.stdout, self.returncode, self.pid = stdout, None, 999999

            def wait(self, timeout=None):
                self.returncode = returncode
                return returncode

        return _Proc()

    return popen


# A fake nvidia-smi: counts its calls, prints FAKE_NVIDIA_SMI and exits with
# FAKE_NVIDIA_SMI_EXIT.
FAKE_NVIDIA_SMI = """#!/bin/sh
echo "$*" >>"$FAKE_WHISPER_LOG.nvidia-smi"
printf '%s' "$FAKE_NVIDIA_SMI"
exit "${FAKE_NVIDIA_SMI_EXIT:-0}"
"""


class FakeWhisperCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        self.cli = bin_dir / 'whisper-cli'
        self.cli.write_text(FAKE_WHISPER.format(python=sys.executable))
        self.cli.chmod(0o755)
        (bin_dir / 'ffmpeg').write_text(FAKE_FFMPEG)
        (bin_dir / 'ffmpeg').chmod(0o755)
        (bin_dir / 'nvidia-smi').write_text(FAKE_NVIDIA_SMI)
        (bin_dir / 'nvidia-smi').chmod(0o755)
        self.models = self.root / 'models'
        self.models.mkdir()
        for name in ('large-v3-turbo-q5_0', 'silero-v6.2.0', 'small'):
            (self.models / f'ggml-{name}.bin').write_bytes(b'model')
        self.audio = self.root / '70-voice.ogg'
        self.audio.write_bytes(b'OggS')
        self.log = self.root / 'whisper-calls.jsonl'
        # The queue is always ~/.local/state/voice-transcribe: a fake home
        # keeps the tests off the real one.
        self.home = self.root / 'home'
        self.home.mkdir()
        self.queue = self.home / vt.QUEUE_SUBDIR
        # Nothing from the caller's environment picks the device.
        inherited = {k: v for k, v in os.environ.items()
                     if not k.startswith('VOICE_TRANSCRIBE_') and k != 'CUDA_VISIBLE_DEVICES'}
        self.env = dict(inherited, PATH=f'{bin_dir}{os.pathsep}{os.environ.get("PATH", "")}', HOME=str(self.home),
                        VOICE_TRANSCRIBE_WHISPER_CLI=str(self.cli), VOICE_TRANSCRIBE_MODEL_DIR=str(self.models),
                        VOICE_TRANSCRIBE_THREADS='3', FAKE_WHISPER_LOG=str(self.log))

    def adapter(self, *answers, audio=None, model='large-v3-turbo-q5_0', device='cpu', extra_env=None):
        env = dict(self.env, FAKE_WHISPER_ANSWERS=json.dumps(list(answers)), **(extra_env or {}))
        return subprocess.run([sys.executable, str(TRANSCRIBER / 'adapter.py'), str(audio or self.audio), model, device],
                              env=env, capture_output=True, text=True, timeout=60)

    def whisper_calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def whisper_cuda(self):
        """CUDA_VISIBLE_DEVICES each whisper run saw (None: not set)."""
        path = Path(str(self.log) + '.cuda')
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def nvidia_smi_calls(self):
        path = Path(str(self.log) + '.nvidia-smi')
        return path.read_text().splitlines() if path.exists() else []

    def result(self, *answers, **kwargs):
        """The adapter's result without its device line, which goes to
        self.device (None when whisper never ran)."""
        done = self.adapter(*answers, **kwargs)
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.splitlines()
        # At most one "running" line (the turn started), then exactly one result.
        self.assertIn(lines[:-1], ([], [vt.RUNNING_LINE]), done.stdout)
        result = json.loads(lines[-1])
        self.device = result.pop('device', None)
        return result


class TranscriberTests(FakeWhisperCase):
    def test_default_language_is_english_with_vad_on_the_cpu(self):
        self.assertEqual(self.result(' Ship it\n  today. '), {'text': 'Ship it today.'})
        self.assertEqual(self.device, 'cpu: gpu-off')
        self.assertEqual(self.whisper_cuda(), [''])  # the CPU run never sees a GPU
        self.assertEqual(self.nvidia_smi_calls(), [])
        [argv] = self.whisper_calls()
        self.assertEqual(argv[argv.index('-l') + 1], 'en')
        self.assertNotIn('auto', argv)
        self.assertNotIn('-dl', argv)
        self.assertEqual(argv.count('-l'), 1)
        self.assertIn('-ng', argv)
        self.assertEqual(argv[argv.index('-t') + 1], '3')
        self.assertEqual(argv[argv.index('-m') + 1], str(self.models / 'ggml-large-v3-turbo-q5_0.bin'))
        self.assertIn('--vad', argv)
        self.assertEqual(argv[argv.index('-vm') + 1], str(self.models / 'ggml-silero-v6.2.0.bin'))
        self.assertNotIn('-mc', argv)

    @unittest.skipUnless(hasattr(os, 'sched_setaffinity'), 'CPU affinity requires sched_setaffinity')
    def test_whisper_runs_fenced_on_its_cpus_at_the_agents_priority(self):
        mine = sorted(os.sched_getaffinity(0))
        cpus = mine[-2:] if len(mine) > 1 else mine
        self.env['VOICE_TRANSCRIBE_CPUS'] = ','.join(str(c) for c in cpus)
        self.result('Fine.', extra_env={'TELEGRAM_PACK_PLATFORM': 'linux'})
        fence = json.loads(Path(str(self.log) + '.fence').read_text())
        self.assertEqual(fence['cpus'], cpus)
        self.assertEqual(fence['nice'], max(10, os.getpriority(os.PRIO_PROCESS, 0)))
        if shutil.which('ionice'):
            self.assertEqual(fence['ionice'], 'best-effort: prio 7')

    def test_unset_threads_defaults_to_four(self):
        self.assertEqual(vt.Options(environ={}).threads, 4)
        self.assertEqual(vt.Options(environ={'VOICE_TRANSCRIBE_THREADS': '16'}).threads, 16)
        self.assertEqual(vt.Options(environ={'VOICE_TRANSCRIBE_THREADS': '100'}).threads, 64)

    def test_more_than_four_threads_is_capped(self):
        self.env['VOICE_TRANSCRIBE_THREADS'] = '12'
        self.result('Fine.')
        argv = self.whisper_calls()[0]
        self.assertEqual(argv[argv.index('-t') + 1], '12')
        self.env['VOICE_TRANSCRIBE_THREADS'] = '100'
        self.result('Fine.')
        argv = self.whisper_calls()[-1]
        self.assertEqual(argv[argv.index('-t') + 1], '64')

    def test_transcription_succeeds_when_ionice_is_absent(self):
        bin_only = self.env['PATH'].split(os.pathsep)[0]
        self.assertFalse((Path(bin_only) / 'ionice').exists())
        self.assertEqual(self.result('Fine.', extra_env={'PATH': bin_only, 'TELEGRAM_PACK_PLATFORM': 'linux'}),
                         {'text': 'Fine.'})

    def test_nice_and_ionice_failures_never_fail_the_fence(self):
        options = vt.Options(environ={})
        def missing(*args, **kwargs):
            raise FileNotFoundError('ionice')
        vt.fence(options, run=missing, platform='linux')
        def refused(*args, **kwargs):
            return subprocess.CompletedProcess(args, 1)
        vt.fence(options, run=refused, platform='linux')
        with patch('os.getpriority', return_value=0), \
                patch('os.setpriority', side_effect=PermissionError('nice')):
            vt.fence(options, run=refused, platform='linux')

    def test_fence_on_darwin_with_cpus_and_no_affinity_fails_and_runs_nothing(self):
        calls = []

        def run(*args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0)

        options = vt.Options(environ={'VOICE_TRANSCRIBE_CPUS': '0-1'})
        with self.assertRaises(vt.TranscribeError) as caught:
            vt.fence(options, run=run, platform='darwin', setaffinity=None)
        self.assertEqual(caught.exception.code, 'fence_failed')
        self.assertEqual(str(caught.exception), 'CPU affinity is not available on this platform')
        self.assertEqual(calls, [])

    def test_fence_on_darwin_without_cpus_runs_no_ionice_and_still_sets_nice(self):
        calls = []

        def run(*args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0)

        options = vt.Options(environ={})
        with patch('os.getpriority', return_value=0), \
                patch('os.setpriority') as setpriority:
            vt.fence(options, run=run, platform='darwin')
        self.assertEqual(calls, [])
        setpriority.assert_called_once_with(os.PRIO_PROCESS, 0, 10)

    def test_fence_on_linux_calls_ionice_exactly_as_before(self):
        calls = []

        def run(*args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0)

        options = vt.Options(environ={})
        with patch('os.getpriority', return_value=10):
            vt.fence(options, run=run, platform='linux')
        self.assertEqual(len(calls), 1)
        (argv,), kwargs = calls[0][0], calls[0][1]
        self.assertEqual(argv, ['ionice', '-c', '2', '-n', '7', '-p', str(os.getpid())])
        self.assertEqual(kwargs.get('timeout'), 10)

    def test_run_gpu_check_on_wsl_appends_the_wsl_lib_path(self):
        captured = []
        options = vt.Options(environ={}, gpu_check=str(self.root / 'check.py'))
        self.assertEqual(
            vt.run_gpu_check(options, popen=_recording_popen(captured), platform='wsl'),
            ('cpu', 'busy'))
        self.assertEqual(captured[0][1]['env']['PATH'], vt.GPU_CHECK_PATH + ':/usr/lib/wsl/lib')
        self.assertTrue(captured[0][1]['env']['PATH'].endswith(':/usr/lib/wsl/lib'))

    def test_run_gpu_check_on_linux_and_darwin_keeps_the_check_path(self):
        for plat in ('linux', 'darwin'):
            with self.subTest(platform=plat):
                captured = []
                options = vt.Options(environ={}, gpu_check=str(self.root / 'check.py'))
                self.assertEqual(
                    vt.run_gpu_check(options, popen=_recording_popen(captured), platform=plat),
                    ('cpu', 'busy'))
                self.assertEqual(captured[0][1]['env']['PATH'], vt.GPU_CHECK_PATH)

    def test_unknown_language_is_matched_case_insensitively(self):
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout='Some text',
                                               stderr="Error: Unknown language 'zz'\n")
        with self.assertRaises(vt.TranscribeError) as caught:
            vt._run_whisper(['whisper-cli'], run, time.monotonic() + 10)
        self.assertEqual(caught.exception.code, 'whisper_failed')

    @unittest.skipUnless(hasattr(os, 'sched_setaffinity'), 'CPU affinity requires sched_setaffinity')
    def test_a_fence_that_cannot_be_set_stops_the_transcription(self):
        for cpus in ('99999', '3-1', 'all'):
            with self.subTest(cpus=cpus):
                self.env['VOICE_TRANSCRIBE_CPUS'] = cpus
                self.assertEqual(self.result('unused', extra_env={'TELEGRAM_PACK_PLATFORM': 'linux'}),
                                 {'error': 'fence_failed'})
        self.assertEqual(self.whisper_calls(), [])

    def test_the_model_argument_picks_the_model_file(self):
        self.result('fine', model='small')
        self.assertEqual(self.whisper_calls()[0][self.whisper_calls()[0].index('-m') + 1],
                         str(self.models / 'ggml-small.bin'))

    def test_no_speech_after_vad_is_an_empty_text(self):
        self.assertEqual(self.result(''), {'text': ''})

    def test_a_loop_is_retried_once_without_context_and_the_better_text_kept(self):
        looped = 'what is it? what is it? what is it? what is it?'
        self.assertEqual(self.result(looped, 'what is it, he asked'), {'text': 'what is it, he asked'})
        first, retry = self.whisper_calls()
        self.assertNotIn('-mc', first)
        self.assertEqual(retry[retry.index('-mc') + 1], '0')
        self.assertEqual(retry[retry.index('-l') + 1], 'en')

    def test_a_retry_that_still_loops_as_much_keeps_the_first_text(self):
        looped = 'go on go on go on'
        self.assertEqual(self.result(looped, 'yes yes, go on go on go on'), {'text': looped})
        self.assertEqual(len(self.whisper_calls()), 2)

    def test_configured_language_is_passed_and_cyrillic_text_is_delivered_unchanged(self):
        cyrillic = 'Что такое? Мы обновим это до пятницы.'
        self.assertEqual(self.result(cyrillic, extra_env={'VOICE_TRANSCRIBE_LANGUAGE': 'ru'}),
                         {'text': cyrillic})
        [argv] = self.whisper_calls()
        self.assertEqual(argv[argv.index('-l') + 1], 'ru')

    def test_auto_language_is_passed_to_whisper(self):
        self.assertEqual(self.result('Bonjour.', extra_env={'VOICE_TRANSCRIBE_LANGUAGE': 'auto'}),
                         {'text': 'Bonjour.'})
        [argv] = self.whisper_calls()
        self.assertEqual(argv[argv.index('-l') + 1], 'auto')

    def test_non_latin_text_is_not_refused_in_the_default_language(self):
        cyrillic = 'Что такое? Мы обновим это до пятницы.'
        self.assertEqual(self.result(cyrillic), {'text': cyrillic})

    def test_an_unknown_language_error_with_exit_zero_is_whisper_failed(self):
        # whisper-cli prints "error: unknown language" and exits 0 with no
        # output for a code it does not know: that is a failure, not silence.
        self.assertEqual(self.result('UNKNOWN_LANGUAGE', extra_env={'VOICE_TRANSCRIBE_LANGUAGE': 'zz'}),
                         {'error': 'whisper_failed'})
        self.assertEqual(self.device, 'cpu: gpu-off')

    def test_a_transcript_starting_with_error_is_returned_unchanged(self):
        # whisper-cli prints its own "error:" lines to stderr; stdout with
        # -nt -np is the transcript itself, so a dictated "Error: ..." is
        # real speech and must be delivered, not reported as a failure.
        text = 'Error: the deploy failed on staging, can you look?'
        self.assertEqual(self.result(text), {'text': text})

    def test_known_failures_are_codes_never_child_text(self):
        self.assertEqual(self.result('FAIL'), {'error': 'whisper_failed'})
        self.assertEqual(self.device, 'cpu: gpu-off')
        bad = self.root / 'bad.ogg'
        bad.write_bytes(b'x')
        self.assertEqual(self.result('unused', audio=bad), {'error': 'decode_failed'})
        self.assertIsNone(self.device)  # whisper never ran: no device line
        (self.models / 'ggml-silero-v6.2.0.bin').unlink()
        self.assertEqual(self.result('unused'), {'error': 'model_missing'})
        self.cli.chmod(0o644)
        self.assertEqual(self.result('unused'), {'error': 'whisper_missing'})

    def test_usage_errors_exit_2(self):
        for device in ('cuda', 'auto2', ''):
            with self.subTest(device=device):
                done = self.adapter('x', device=device)
                self.assertEqual((done.returncode, done.stdout), (2, ''))

    def test_the_command_writes_the_text_next_to_the_audio(self):
        env = dict(self.env, FAKE_WHISPER_ANSWERS='["Hello there."]')
        done = subprocess.run([sys.executable, str(TRANSCRIBER / 'voice_transcribe.py'), '--quiet', str(self.root)],
                              env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout), (0, 'Hello there.\n'), done.stderr)
        self.assertEqual(done.stderr, f'voice-transcribe: {self.audio}: device cpu: gpu-off\n')
        self.assertEqual((self.root / '70-voice.txt').read_text(), 'Hello there.\n')
        again = subprocess.run([sys.executable, str(TRANSCRIBER / 'voice_transcribe.py'), '--quiet', str(self.root)],
                               env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(again.stdout, '')  # a directory skips audio that already has its .txt
        self.assertEqual(len(self.whisper_calls()), 1)

    def test_help_lists_every_environment_variable_the_module_reads(self):
        source = (TRANSCRIBER / 'voice_transcribe.py').read_text()
        names = sorted(set(re.findall(r'VOICE_TRANSCRIBE_[A-Z_]+', source)))
        done = subprocess.run([sys.executable, str(TRANSCRIBER / 'voice_transcribe.py'), '--help'],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        for name in names:
            self.assertIn(name, done.stdout)

    def test_the_adapter_writes_nothing_beside_the_audio(self):
        done = self.adapter('Fine.')
        self.assertEqual(done.stdout.splitlines(), [vt.RUNNING_LINE, '{"text": "Fine.", "device": "cpu: gpu-off"}'])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ['70-voice.ogg', 'bin', 'home', 'models', 'whisper-calls.jsonl', 'whisper-calls.jsonl.cuda',
                          'whisper-calls.jsonl.fence'])
        self.assertEqual(os.listdir(self.queue), ['queue.lock'])  # its ticket is gone


class GpuCheckMethodTests(FakeWhisperCase):
    """method auto / gpu / cpu through the adapter: the check runs once with
    a fixed environment, its contract decides the device, method gpu runs
    no check at all, and a failed GPU run retries once on the CPU."""

    def setUp(self):
        super().setUp()
        self.check_dir = self.root / 'gpu-check'
        self.check_dir.mkdir()
        self.check = self.check_dir / 'check.py'
        self.check_log = self.root / 'check-calls.jsonl'

    def write_check(self, body):
        self.check.write_text(
            f'#!{sys.executable}\nimport json, os, sys, time\n'
            f'with open({str(self.check_log)!r}, "a") as stream:\n'
            '    stream.write(json.dumps(dict(env=dict(os.environ), cwd=os.getcwd(), argv=sys.argv[1:])) + "\\n")\n'
            + body)
        self.check.chmod(0o755)
        return str(self.check)

    def check_calls(self):
        if not self.check_log.exists():
            return []
        return [json.loads(line) for line in self.check_log.read_text().splitlines()]

    def checked(self, *answers, **kwargs):
        kwargs.setdefault('device', 'check')
        kwargs.setdefault('extra_env', {})['VOICE_TRANSCRIBE_GPU_CHECK'] = str(self.check)
        return self.result(*answers, **kwargs)

    def test_a_true_check_runs_whisper_on_the_gpu_with_a_fixed_environment(self):
        self.write_check('print(\'{"gpu": true}\')\n')
        self.assertEqual(self.checked('Ship it.'), {'text': 'Ship it.'})
        self.assertEqual(self.device, 'gpu')
        [argv] = self.whisper_calls()
        self.assertNotIn('-ng', argv)
        self.assertEqual(self.whisper_cuda(), [None])
        [call] = self.check_calls()
        self.assertEqual(call['argv'], [])
        self.assertEqual(call['cwd'], str(self.check_dir))
        observed = set(call['env'])
        if vt.host_platform() == 'darwin':
            # macOS itself adds this one variable to child processes.
            observed.discard('__CF_USER_TEXT_ENCODING')
        self.assertEqual(observed, {'PATH', 'HOME', 'LANG', 'GPU_MIN_FREE_MB'})
        self.assertEqual(call['env']['PATH'], '/usr/local/bin:/usr/bin:/bin')
        self.assertEqual(call['env']['GPU_MIN_FREE_MB'], '2048')

    def test_a_true_check_with_a_reason_labels_the_gpu_with_it(self):
        self.write_check('print(\'{"gpu": true, "reason": "unknown"}\')\n')
        self.assertEqual(self.checked('Fine.'), {'text': 'Fine.'})
        self.assertEqual(self.device, 'gpu: unknown')
        self.assertNotIn('-ng', self.whisper_calls()[0])

    def test_a_false_check_runs_on_the_cpu(self):
        for body, label in (('print(\'{"gpu": false, "reason": "operator-busy"}\')\n', 'cpu: operator-busy'),
                            ('print(\'{"gpu": false}\')\n', 'cpu: busy')):
            with self.subTest(label=label):
                self.log.unlink(missing_ok=True)
                Path(str(self.log) + '.cuda').unlink(missing_ok=True)
                self.check_log.unlink(missing_ok=True)
                self.write_check(body)
                self.assertEqual(self.checked('Fine.'), {'text': 'Fine.'})
                self.assertEqual(self.device, label)
                self.assertIn('-ng', self.whisper_calls()[0])
                self.assertEqual(self.whisper_cuda(), [''])
                self.assertEqual(len(self.check_calls()), 1)

    def test_anything_outside_the_check_contract_means_check_error_on_the_cpu(self):
        for body in ('print("not json")\n',
                     'print(\'{"gpu": true, "reason": null}\')\n',
                     'print(\'{"gpu": false, "reason": null}\')\n',
                     'print(\'{"gpu": true, "reason": "gpu-failed"}\')\n',
                     'print(\'{"gpu": true, "reason": "Bad Reason"}\')\n',
                     'print(\'{"gpu": "yes"}\')\n',
                     'print(\'{"gpu": true, "extra": 1}\')\n',
                     'print("x" * 5000)\n',
                     'print(\'{"gpu": true}\')\nsys.exit(3)\n'):
            with self.subTest(body=body):
                self.log.unlink(missing_ok=True)
                self.write_check(body)
                self.assertEqual(self.checked('Fine.'), {'text': 'Fine.'})
                self.assertEqual(self.device, 'cpu: check-error')
                self.assertIn('-ng', self.whisper_calls()[0])

    def test_a_check_that_times_out_means_check_error(self):
        self.write_check('time.sleep(30)\n')
        options = vt.Options(environ=dict(self.env, VOICE_TRANSCRIBE_GPU_CHECK=str(self.check)), method='auto')
        started = time.monotonic()
        with _patched_timeout():
            self.assertEqual(vt.choose_device(options), ('cpu', 'check-error'))
        self.assertLess(time.monotonic() - started, 10)

    @unittest.skipIf(sys.platform == 'darwin' and platform.machine() == 'arm64',
                     'Apple Silicon answers for Metal without nvidia-smi; '
                     'the default-check unknown case is Linux/Intel only')
    def test_method_auto_with_the_default_check_and_no_nvidia_smi_tries_the_gpu(self):
        # The default check (transcriber/gpu_check.py) run through
        # run_gpu_check's fixed environment, with GPU_CHECK_PATH narrowed to
        # a directory holding only a python3 link: no nvidia-smi is found, so
        # the check answers unknown and whisper runs on the GPU.
        check_bin = self.root / 'default-check-bin'
        check_bin.mkdir()
        (check_bin / 'python3').symlink_to(sys.executable)
        env = dict(self.env, FAKE_WHISPER_ANSWERS='["Ship it."]')
        labels = []
        with patch.dict(os.environ, env):
            os.environ.pop('CUDA_VISIBLE_DEVICES', None)
            options = vt.Options(environ=env, method='auto')
            self.assertEqual(options.gpu_check, str(TRANSCRIBER / 'gpu_check.py'))
            with patch.object(vt, 'GPU_CHECK_PATH', str(check_bin)), \
                    patch.object(vt, 'fence', lambda options: None):
                text = vt.transcribe(str(self.audio), options, on_device=labels.append)
        self.assertEqual(text, 'Ship it.')
        self.assertEqual(labels, ['gpu: unknown'])
        [argv] = self.whisper_calls()
        self.assertNotIn('-ng', argv)
        self.assertEqual(self.whisper_cuda(), [None])
        self.assertEqual(self.nvidia_smi_calls(), [])

    def test_method_gpu_runs_no_check_and_retries_a_failure_on_the_cpu(self):
        self.write_check('print(\'{"gpu": false}\')\n')
        env = {'VOICE_TRANSCRIBE_GPU_CHECK': str(self.check)}
        self.assertEqual(self.result('Ship it.', device='gpu', extra_env=env), {'text': 'Ship it.'})
        self.assertEqual(self.device, 'gpu')
        self.assertNotIn('-ng', self.whisper_calls()[0])
        self.assertEqual(self.check_calls(), [])
        self.log.unlink()
        self.assertEqual(self.result('FAIL', 'Still here.', device='gpu', extra_env=env),
                         {'text': 'Still here.'})
        self.assertEqual(self.device, 'cpu: gpu-failed')
        gpu_run, cpu_run = self.whisper_calls()
        self.assertNotIn('-ng', gpu_run)
        self.assertIn('-ng', cpu_run)
        self.assertEqual(self.check_calls(), [])

    def test_the_legacy_auto_device_runs_the_gpu_check(self):
        # Adapter DEVICE auto is the legacy name for the check path: the
        # fake check decides, and gpu_min_free_mb reaches its environment.
        self.write_check('print(\'{"gpu": true}\')\n')
        env = {'VOICE_TRANSCRIBE_GPU_CHECK': str(self.check), 'VOICE_TRANSCRIBE_GPU_MIN_FREE_MB': '3072'}
        self.assertEqual(self.result('Ship it.', device='auto', extra_env=env), {'text': 'Ship it.'})
        self.assertEqual(self.device, 'gpu')
        self.assertNotIn('-ng', self.whisper_calls()[0])
        [call] = self.check_calls()
        self.assertEqual(call['env']['GPU_MIN_FREE_MB'], '3072')
        # VOICE_TRANSCRIBE_GPU=1 with no method is the same check path.
        options = vt.Options(environ=dict(self.env, VOICE_TRANSCRIBE_GPU='1',
                                          VOICE_TRANSCRIBE_GPU_CHECK=str(self.check)))
        self.assertEqual((options.method, options.gpu), ('auto', True))
        self.assertEqual(vt.Options(environ=dict(self.env, VOICE_TRANSCRIBE_GPU='0')).method, 'cpu')

    def test_method_cpu_never_runs_a_check_or_the_gpu(self):
        self.write_check('print(\'{"gpu": true}\')\n')
        env = {'VOICE_TRANSCRIBE_GPU_CHECK': str(self.check)}
        self.assertEqual(self.result('Fine.', device='cpu', extra_env=env), {'text': 'Fine.'})
        self.assertEqual(self.device, 'cpu: gpu-off')
        self.assertIn('-ng', self.whisper_calls()[0])
        self.assertEqual(self.check_calls(), [])


class _patched_timeout:
    """Shorten the GPU check timeout for one in-process choose_device call."""

    def __enter__(self):
        self.old = vt.GPU_CHECK_TIMEOUT
        vt.GPU_CHECK_TIMEOUT = 0.2
        return self

    def __exit__(self, *exc):
        vt.GPU_CHECK_TIMEOUT = self.old
        return False


class GpuCheckAnswerTests(unittest.TestCase):
    """gpu_check.answer() in process: Apple Silicon, Intel Macs and Linux."""

    def test_apple_silicon_answers_gpu_without_running_anything(self):
        calls = []

        def which(name):
            calls.append(('which', name))
            return '/bin/nvidia-smi'

        def popen(*args, **kwargs):
            calls.append(('popen', args, kwargs))
            raise AssertionError('popen must not run on Apple Silicon')

        self.assertEqual(
            gpu_check.answer(environ={}, which=which, popen=popen, system='darwin', machine='arm64'),
            {'gpu': True})
        self.assertEqual(calls, [])

    def test_intel_mac_without_nvidia_smi_answers_unknown(self):
        calls = []

        def popen(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError('popen must not run without nvidia-smi')

        self.assertEqual(
            gpu_check.answer(environ={}, which=lambda name: None, popen=popen,
                             system='darwin', machine='x86_64'),
            {'gpu': True, 'reason': 'unknown'})
        self.assertEqual(calls, [])

    def test_nvidia_smi_on_linux_answers_from_free_memory(self):
        captured = []
        self.assertEqual(
            gpu_check.answer(environ={'GPU_MIN_FREE_MB': '2048'}, which=lambda name: '/bin/nvidia-smi',
                             popen=_recording_popen(captured, b'926, 16311\n'),
                             system='linux', machine='x86_64'),
            {'gpu': True})
        self.assertEqual(captured[0][0], gpu_check.NVIDIA_SMI)

    def test_nvidia_smi_on_linux_with_too_little_free_memory_means_vram_low(self):
        for environ, payload in (({'GPU_MIN_FREE_MB': '2048'}, b'15000, 16311\n'),
                                 ({'GPU_MIN_FREE_MB': '20000'}, b'926, 16311\n')):
            with self.subTest(environ=environ, payload=payload):
                self.assertEqual(
                    gpu_check.answer(environ=environ, which=lambda name: '/bin/nvidia-smi',
                                     popen=_recording_popen([], payload),
                                     system='linux', machine='x86_64'),
                    {'gpu': False, 'reason': 'vram-low'})

    def test_a_missing_nvidia_smi_on_linux_means_try_the_gpu(self):
        def popen(*args, **kwargs):
            raise AssertionError('popen must not run without nvidia-smi')

        self.assertEqual(
            gpu_check.answer(environ={}, which=lambda name: None, popen=popen,
                             system='linux', machine='x86_64'),
            {'gpu': True, 'reason': 'unknown'})

    def test_a_malformed_or_failing_nvidia_smi_on_linux_means_try_the_gpu(self):
        for payload, returncode in ((b'N/A, 16311\n', 0), (b'', 0),
                                    (b'926, 16311\n926, 16311\n', 0),
                                    (b'926, 16311\n', 9)):
            with self.subTest(payload=payload, returncode=returncode):
                self.assertEqual(
                    gpu_check.answer(environ={}, which=lambda name: '/bin/nvidia-smi',
                                     popen=_recording_popen([], payload, returncode),
                                     system='linux', machine='x86_64'),
                    {'gpu': True, 'reason': 'unknown'})

    def test_a_flooding_nvidia_smi_on_linux_is_capped_and_its_group_is_killed(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        marker = root / 'descendant-ran'
        fake = root / 'fake-nvidia-smi'
        fake.write_text(
            f'#!{sys.executable}\nimport subprocess, sys, time\n'
            f'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.6); '
            f'open({str(marker)!r}, \'w\').write(\'x\')"])\n'
            'while True:\n'
            '    sys.stdout.write("x" * 8192)\n'
            '    sys.stdout.flush()\n')
        fake.chmod(0o755)

        def popen(argv, **kwargs):
            return subprocess.Popen([sys.executable, str(fake)], **kwargs)

        started = time.monotonic()
        self.assertEqual(
            gpu_check.answer(environ={}, which=lambda name: '/bin/nvidia-smi',
                             popen=popen, system='linux', machine='x86_64'),
            {'gpu': True, 'reason': 'unknown'})
        self.assertLess(time.monotonic() - started, 4)
        time.sleep(1)
        self.assertFalse(marker.exists())


@unittest.skipIf(sys.platform == 'darwin' and platform.machine() == 'arm64',
                 'Apple Silicon answers for Metal without nvidia-smi; '
                 'the nvidia-smi script cases pin linux in GpuCheckAnswerTests')
class GpuCheckScriptTests(unittest.TestCase):
    """transcriber/gpu_check.py against a fake nvidia-smi (or none)."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bin = self.root / 'bin'
        self.bin.mkdir()

    def run_check(self, smi=None, exit_code='0', path=None, min_free='2048'):
        if smi is not None:
            fake = self.bin / 'nvidia-smi'
            fake.write_text(FAKE_NVIDIA_SMI)
            fake.chmod(0o755)
        env = dict(PATH=str(self.bin) if path is None else path, HOME=str(self.root),
                   GPU_MIN_FREE_MB=min_free, FAKE_WHISPER_LOG=str(self.root / 'log'),
                   FAKE_NVIDIA_SMI=smi or '', FAKE_NVIDIA_SMI_EXIT=exit_code)
        done = subprocess.run([sys.executable, str(TRANSCRIBER / 'gpu_check.py')],
                              env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_enough_free_memory_means_the_gpu(self):
        self.assertEqual(self.run_check('926, 16311\n'), {'gpu': True})
        self.assertEqual(self.run_check('926, 16311\n', min_free='20000'),
                         {'gpu': False, 'reason': 'vram-low'})

    def test_too_little_free_memory_means_vram_low(self):
        self.assertEqual(self.run_check('15000, 16311\n'), {'gpu': False, 'reason': 'vram-low'})

    def test_a_missing_nvidia_smi_means_try_the_gpu(self):
        empty = self.root / 'empty'
        empty.mkdir()
        self.assertEqual(self.run_check(path=str(empty)), {'gpu': True, 'reason': 'unknown'})

    def test_a_malformed_or_failing_nvidia_smi_means_try_the_gpu(self):
        for smi, code in (('N/A, 16311\n', '0'), ('', '0'), ('926, 16311\n926, 16311\n', '0'),
                          ('926, 16311\n', '9')):
            with self.subTest(smi=smi, code=code):
                self.assertEqual(self.run_check(smi, exit_code=code), {'gpu': True, 'reason': 'unknown'})

    def test_a_flooding_nvidia_smi_is_capped_and_its_group_is_killed(self):
        marker = self.root / 'descendant-ran'
        fake = self.bin / 'nvidia-smi'
        fake.write_text(
            f'#!{sys.executable}\nimport subprocess, sys, time\n'
            f'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.6); '
            f'open({str(marker)!r}, \'w\').write(\'x\')"])\n'
            'while True:\n'
            '    sys.stdout.write("x" * 8192)\n'
            '    sys.stdout.flush()\n')
        fake.chmod(0o755)
        env = dict(PATH=str(self.bin), HOME=str(self.root), GPU_MIN_FREE_MB='2048')
        started = time.monotonic()
        done = subprocess.run([sys.executable, str(TRANSCRIBER / 'gpu_check.py')],
                              env=env, capture_output=True, text=True, timeout=30)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {'gpu': True, 'reason': 'unknown'})
        time.sleep(1)
        self.assertFalse(marker.exists())  # the descendant died with its group


@unittest.skipUnless(sys.platform == 'darwin' and platform.machine() == 'arm64',
                     'Apple Silicon only')
class GpuCheckAppleSiliconScriptTests(unittest.TestCase):
    """The script answers for Metal on Apple Silicon without nvidia-smi."""

    def test_the_script_answers_gpu_without_nvidia_smi(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = dict(PATH=str(root), HOME=str(root), GPU_MIN_FREE_MB='2048')
        done = subprocess.run([sys.executable, str(TRANSCRIBER / 'gpu_check.py')],
                              env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {'gpu': True})


class QueueTests(FakeWhisperCase):
    """One host-wide FIFO: requests from two cities (separate settings, one
    queue directory) never run whisper at the same time and run in arrival
    order, with no give-up while waiting."""

    def setUp(self):
        super().setUp()
        self.times = self.root / 'times.jsonl'
        self.cities = {}
        for city in ('city-a', 'city-b'):
            home = self.root / city
            home.mkdir()
            audio = home / 'note.ogg'
            audio.write_bytes(b'OggS')
            self.cities[city] = audio

    def start(self, city, tag, sleep='0.6', answer='Fine.', **env):
        env = dict(self.env, FAKE_WHISPER_ANSWERS=json.dumps([answer]), FAKE_WHISPER_SLEEP=sleep,
                   FAKE_WHISPER_TIMES=str(self.times), FAKE_WHISPER_TAG=tag, **env)
        before = self.tickets()
        proc = subprocess.Popen([sys.executable, str(TRANSCRIBER / 'adapter.py'), str(self.cities[city]), 'small', 'cpu'],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 20
        while len(self.tickets()) <= len(before) and proc.poll() is None:  # it took its ticket: arrival fixed
            self.assertLess(time.monotonic(), deadline, 'no ticket taken')
            time.sleep(0.02)
        return proc

    def tickets(self):
        return sorted(p for p in os.listdir(self.queue) if p.endswith('.ticket')) if self.queue.exists() else []

    def finish(self, proc):
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        result = json.loads(out.splitlines()[-1])
        self.assertIn(result.pop('device', 'cpu: gpu-off'), ('cpu: gpu-off',))
        return result

    def runs(self):
        return [json.loads(line) for line in self.times.read_text().splitlines()]

    def assert_one_at_a_time_in_order(self, tags):
        runs = self.runs()
        self.assertEqual([r['tag'] for r in runs], tags)
        for earlier, later in zip(runs, runs[1:]):
            self.assertLessEqual(earlier['end'], later['start'])

    def test_two_cities_never_run_whisper_at_once_and_go_in_arrival_order(self):
        first = self.start('city-a', 'a1')
        second = self.start('city-b', 'b1')
        self.assertEqual([self.finish(p) for p in (first, second)], [{'text': 'Fine.'}] * 2)
        self.assert_one_at_a_time_in_order(['a1', 'b1'])

    def test_no_environment_moves_a_city_out_of_the_shared_queue(self):
        # The old per-city queue variable is gone: a caller still setting it
        # (each city a different directory) waits in the one queue.
        first = self.start('city-a', 'a1', VOICE_TRANSCRIBE_QUEUE_DIR=str(self.root / 'city-a' / 'q'))
        second = self.start('city-b', 'b1', VOICE_TRANSCRIBE_QUEUE_DIR=str(self.root / 'city-b' / 'q'))
        self.assertEqual([self.finish(p) for p in (first, second)], [{'text': 'Fine.'}] * 2)
        self.assert_one_at_a_time_in_order(['a1', 'b1'])
        self.assertFalse((self.root / 'city-a' / 'q').exists())
        self.assertFalse((self.root / 'city-b' / 'q').exists())

    def test_three_requests_from_two_cities_run_one_by_one_in_arrival_order(self):
        procs = [self.start('city-b', 'b1'), self.start('city-a', 'a1'), self.start('city-b', 'b2')]
        self.assertEqual([self.finish(p) for p in procs], [{'text': 'Fine.'}] * 3)
        self.assert_one_at_a_time_in_order(['b1', 'a1', 'b2'])
        self.assertEqual(self.tickets(), [])

    def test_a_waiting_note_has_no_fixed_give_up(self):
        # The first run takes longer than any old lock wait would allow the
        # second (its run limit does not tick while it waits).
        first = self.start('city-a', 'a1', sleep='3', VOICE_TRANSCRIBE_TIMEOUT='10')
        second = self.start('city-b', 'b1', sleep='0', VOICE_TRANSCRIBE_TIMEOUT='1', VOICE_TRANSCRIBE_TIMEOUT_PER_MINUTE='1')
        self.assertEqual([self.finish(p) for p in (first, second)], [{'text': 'Fine.'}] * 2)
        self.assert_one_at_a_time_in_order(['a1', 'b1'])

    def test_a_hung_whisper_is_killed_at_its_limit_and_the_next_note_runs(self):
        hung = self.start('city-a', 'hung', sleep='60', VOICE_TRANSCRIBE_TIMEOUT='1', VOICE_TRANSCRIBE_TIMEOUT_PER_MINUTE='1')
        nxt = self.start('city-b', 'next', sleep='0')
        started = time.monotonic()
        self.assertEqual(self.finish(hung), {'error': 'timeout'})
        self.assertEqual(self.finish(nxt), {'text': 'Fine.'})
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual([r['tag'] for r in self.runs()], ['next'])  # the hung run never finished

    def test_a_ticket_left_by_a_dead_process_is_cleared(self):
        self.queue.mkdir(mode=0o700, parents=True)
        (self.queue / '000000000001-99999.ticket').write_text('')  # nobody holds it
        proc = self.start('city-a', 'a1', sleep='0')
        self.assertEqual(self.finish(proc), {'text': 'Fine.'})
        self.assertEqual(self.tickets(), [])

    def test_a_queue_others_can_open_is_refused(self):
        self.queue.mkdir(mode=0o700, parents=True)
        self.queue.chmod(0o755)
        self.assertEqual(self.result('unused'), {'error': 'queue_failed'})
        self.assertEqual(self.whisper_calls(), [])


class GpuRunLimitTests(unittest.TestCase):
    """A GPU run that hangs is stopped at its own limit, so the CPU retry
    still has its whole run limit; in process, with fake runs."""

    def test_a_hung_gpu_run_hands_over_to_the_cpu_with_time_left(self):
        options = vt.Options(environ={'VOICE_TRANSCRIBE_TIMEOUT': '120'}, gpu=True)
        calls = []

        def run(argv, timeout, env, **kwargs):
            calls.append(('-ng' in argv, round(timeout), env and env.get('CUDA_VISIBLE_DEVICES')))
            if '-ng' not in argv:
                raise subprocess.TimeoutExpired(argv, timeout)
            return subprocess.CompletedProcess(argv, 0, stdout='Done.', stderr='')

        device = vt.Device('gpu', 'gpu')
        gpu_limit = options.gpu_run_limit(60)
        deadline = time.monotonic() + options.run_limit(60) + gpu_limit
        text = vt._whisper(options, Path('/x.wav'), run, device, deadline, gpu_limit)
        self.assertEqual((text, device.label), ('Done.', 'cpu: gpu-failed'))
        (gpu, gpu_timeout, gpu_cuda), (cpu, cpu_timeout, cpu_cuda) = calls
        self.assertEqual((gpu, gpu_timeout, gpu_cuda), (False, gpu_limit, None))
        self.assertEqual((cpu, cpu_cuda), (True, ''))
        self.assertGreaterEqual(cpu_timeout, options.run_limit(60) - 1)  # the CPU keeps its whole limit

    def test_the_gpu_limit_is_a_quarter_of_the_run_limit_at_least_30_s(self):
        options = vt.Options(environ={'VOICE_TRANSCRIBE_TIMEOUT': '120', 'VOICE_TRANSCRIBE_TIMEOUT_PER_MINUTE': '120'})
        self.assertEqual([options.gpu_run_limit(s) for s in (0, 72, 600)], [30, 66, 330])
        self.assertIs(options.gpu, False)


class LoopAndScriptTests(unittest.TestCase):
    def test_longest_repeat_counts_back_to_back_phrases_of_two_words_or_more(self):
        self.assertEqual(vt.longest_repeat('Что такое? Что такое? Что такое? Что такое?'), 4)
        self.assertEqual(vt.longest_repeat('very very very good'), 1)  # one word is speech, not a loop
        self.assertEqual(vt.longest_repeat('ship the pack, ship the pack, then test'), 2)
        self.assertEqual(vt.longest_repeat(''), 1)


class HostPlatformTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def release(self, text):
        path = self.root / 'osrelease'
        path.write_text(text)
        return str(path)

    def test_darwin_from_system(self):
        self.assertEqual(
            vt.host_platform(environ={}, system='darwin',
                             osrelease=self.release('5.15.153.1-microsoft-standard-WSL2\n')),
            'darwin')

    def test_wsl_from_osrelease(self):
        for text in ('5.15.153.1-microsoft-standard-WSL2\n', '4.4.0-19041-Microsoft\n'):
            with self.subTest(text=text):
                self.assertEqual(
                    vt.host_platform(environ={}, system='linux', osrelease=self.release(text)),
                    'wsl')

    def test_linux_from_osrelease_and_from_a_missing_file(self):
        self.assertEqual(
            vt.host_platform(environ={}, system='linux',
                             osrelease=self.release('6.17.1-arch1-1\n')),
            'linux')
        self.assertEqual(
            vt.host_platform(environ={}, system='linux',
                             osrelease=str(self.root / 'missing-osrelease')),
            'linux')

    def test_linux_from_a_non_utf8_osrelease(self):
        path = self.root / 'osrelease'
        path.write_bytes(b'\xff\xfe microsoft \x80\n')
        self.assertEqual(
            vt.host_platform(environ={}, system='linux', osrelease=str(path)),
            'linux')

    def test_the_environment_value_wins_for_each_platform(self):
        self.assertEqual(vt.PLATFORMS, ('linux', 'wsl', 'darwin'))
        self.assertEqual(vt.PLATFORM_ENV, 'TELEGRAM_PACK_PLATFORM')
        microsoft = self.release('5.15.153.1-microsoft-standard-WSL2\n')
        for platform in vt.PLATFORMS:
            with self.subTest(platform=platform):
                system = 'linux' if platform == 'darwin' else 'darwin'
                self.assertEqual(
                    vt.host_platform(environ={'TELEGRAM_PACK_PLATFORM': platform}, system=system,
                                     osrelease=microsoft),
                    platform)

    def test_an_unknown_environment_value_is_ignored(self):
        self.assertEqual(
            vt.host_platform(environ={'TELEGRAM_PACK_PLATFORM': 'windows'}, system='linux',
                             osrelease=self.release('6.17.1-arch1-1\n')),
            'linux')


if __name__ == '__main__':
    unittest.main()
