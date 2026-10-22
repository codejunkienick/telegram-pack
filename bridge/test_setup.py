"""Transcriber setup script: fake git, cmake and curl, temp dirs only."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bridge import write_settings  # noqa: E402

PACK = Path(__file__).resolve().parents[1]
SETUP = PACK / "transcriber" / "setup.sh"
FAKE_BYTES = b"fake-model\n"
DESCRIBE = "v1.9.5"
PIN = "d1be6fde11ac6e0407606b4e42fe72d34add8037"
USAGE = "usage: transcriber/setup.sh --settings <file> [--dry-run] [--whisper-ref <ref>]"
CPU_FALLBACK = (
    "ok    whisper-cli built for the CPU "
    "(no nvcc on PATH; the GPU is tried and falls back to the CPU)"
)

GIT_FAKE = """#!/bin/sh
printf 'git %s\\n' "$*" >> "$FAKE_LOG"
if [ "$1" = clone ]; then
  for last; do :; done
  mkdir -p "$last"
  exit 0
fi
if [ "$1" = -C ] && [ "$3" = describe ]; then
  printf '%s\\n' 'v1.9.5'
  exit 0
fi
if [ "$1" = -C ] && [ "$3" = rev-parse ]; then
  printf '%s\\n' "${FAKE_GIT_HEAD:-d1be6fde11ac6e0407606b4e42fe72d34add8037}"
  exit 0
fi
if [ "$1" = -C ] && [ "$3" = checkout ] && [ -n "${FAKE_GIT_FAIL_DETACH:-}" ]; then
  exit 1
fi
exit 0
"""

CMAKE_FAKE = """#!/bin/sh
printf 'cmake %s\\n' "$*" >> "$FAKE_LOG"
case " $* " in
  *" --build "*)
    mkdir -p build/bin
    printf 'fake whisper-cli\\n' > build/bin/whisper-cli
    chmod 755 build/bin/whisper-cli
    exit 0 ;;
esac
while [ $# -gt 0 ]; do
  if [ "$1" = -B ]; then mkdir -p "$2"; break; fi
  shift
done
exit 0
"""

CURL_FAKE = """#!/bin/sh
printf 'curl %s\\n' "$*" >> "$FAKE_LOG"
out=
while [ $# -gt 0 ]; do
  if [ "$1" = -o ]; then out=$2; shift 2; continue; fi
  shift
done
if [ -n "${FAKE_CURL_INT:-}" ]; then
  printf 'partial\\n' > "$out"
  kill -INT "$PPID"
  exit 0
fi
printf 'fake-model\\n' > "$out"
exit 0
"""

TOOL_FAKE = """#!/bin/sh
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$FAKE_LOG"
exit 0
"""

SHA256SUM_FAKE = """#!/usr/bin/env python3
import hashlib, sys
for name in sys.argv[1:]:
    with open(name, "rb") as fh:
        print(hashlib.sha256(fh.read()).hexdigest() + "  " + name)
"""

SHASUM_FAKE = """#!/usr/bin/env python3
import hashlib, os, sys
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("shasum " + " ".join(sys.argv[1:]) + "\\n")
for name in [a for a in sys.argv[1:] if a not in ("-a", "256")]:
    with open(name, "rb") as fh:
        print(hashlib.sha256(fh.read()).hexdigest() + "  " + name)
"""

UNAME_DARWIN_FAKE = """#!/bin/sh
printf 'Darwin\\n'
"""

UNAME_LINUX_FAKE = """#!/bin/sh
printf 'Linux\\n'
"""


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.home.mkdir()
        self.checkout = self.root / "whisper.cpp"
        self.whisper_cli = self.checkout / "build" / "bin" / "whisper-cli"
        self.models_dir = self.checkout / "models"
        self.log = self.root / "fake.log"
        digest = hashlib.sha256(FAKE_BYTES).hexdigest()
        self.sums = self.root / "models.sha256"
        self.sums.write_text(
            f"{digest}  ggml-large-v3-turbo-q5_0.bin\n"
            f"{digest}  ggml-silero-v6.2.0.bin\n")

    def _write_fake(self, directory, name, body):
        path = directory / name
        path.write_text(body)
        path.chmod(0o755)

    def make_bin(self, with_nvcc=True, omit=(), with_ninja=False,
                 with_shasum=False):
        """A PATH dir: fakes first, real core tools as symlinks, nothing else."""
        directory = Path(tempfile.mkdtemp(dir=self.root))
        self._write_fake(directory, "git", GIT_FAKE)
        self._write_fake(directory, "cmake", CMAKE_FAKE)
        self._write_fake(directory, "curl", CURL_FAKE)
        if with_ninja:
            self._write_fake(directory, "ninja", TOOL_FAKE)
        if with_shasum:
            self._write_fake(directory, "shasum", SHASUM_FAKE)
        for name in ("ffmpeg",):
            if name not in omit:
                self._write_fake(directory, name, TOOL_FAKE)
        if with_nvcc and "nvcc" not in omit:
            self._write_fake(directory, "nvcc", TOOL_FAKE)
        if "ffmpeg" in omit:
            (directory / "ffmpeg").unlink(missing_ok=True)
        for name in ("python3", "sha256sum", "make", "dirname", "mkdir",
                     "mktemp", "mv", "rm", "cut", "awk", "chmod"):
            if name in omit:
                continue
            real = shutil.which(name)
            if name == "sha256sum" and real is None:
                self._write_fake(directory, name, SHA256SUM_FAKE)
                continue
            self.assertIsNotNone(real, name)
            (directory / name).symlink_to(real)
        compiler = shutil.which("c++") or shutil.which("g++")
        self.assertIsNotNone(compiler)
        (directory / "c++").symlink_to(compiler)
        return directory

    def settings(self, method="auto", enabled=True, whisper_cli=None):
        path = write_settings(self.root)
        cli = whisper_cli if whisper_cli is not None else self.whisper_cli
        text = "\n[transcribe]\n"
        text += f"enabled = {'true' if enabled else 'false'}\n"
        if enabled:
            text += f'whisper_cli = "{cli}"\n'
            text += f'models_dir = "{self.models_dir}"\n'
            text += 'model = "large-v3-turbo-q5_0"\n'
            text += 'vad_model = "silero-v6.2.0"\n'
            if method is not None:
                text += f'method = "{method}"\n'
        path.write_text(path.read_text() + text)
        path.chmod(0o600)
        return path

    def run_setup(self, settings, bin_dir, *extra, env_extra=None):
        env = dict(HOME=str(self.home), PATH=str(bin_dir),
                   FAKE_LOG=str(self.log),
                   TELEGRAM_PACK_MODELS_SHA256=str(self.sums),
                   TELEGRAM_PACK_PLATFORM="linux",
                   LANG="C")
        if env_extra:
            for key, value in env_extra.items():
                if value is None:
                    env.pop(key, None)
                else:
                    env[key] = value
        return subprocess.run(
            [str(SETUP), "--settings", str(settings), *extra],
            env=env, capture_output=True, text=True, timeout=60)

    def log_text(self):
        return self.log.read_text() if self.log.exists() else ""

    def clear_log(self):
        self.log.unlink(missing_ok=True)

    def test_dry_run_changes_nothing(self):
        settings = self.settings("auto")
        result = self.run_setup(settings, self.make_bin(), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("would clone", result.stdout)
        self.assertIn("would build", result.stdout)
        self.assertIn("would download", result.stdout)
        self.assertTrue(result.stdout.strip().splitlines()[-1].startswith("next: "))
        self.assertFalse(self.checkout.exists())
        self.assertNotIn("git clone", self.log_text())
        self.assertNotIn("cmake", self.log_text())
        self.assertNotIn("curl", self.log_text())

    def test_full_run_auto_with_nvcc_clones_builds_cuda_and_downloads(self):
        settings = self.settings("auto")
        result = self.run_setup(settings, self.make_bin(with_nvcc=True))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(
            lines[-1], f"next: {PACK}/install.sh --settings {settings}")
        log = self.log_text()
        clone_at = log.index("git clone https://github.com/ggml-org/whisper.cpp")
        detach_at = log.index(f"checkout --detach {PIN}")
        self.assertLess(clone_at, detach_at)
        self.assertIn("rev-parse HEAD", log)
        note = f"note  checksum list from TELEGRAM_PACK_MODELS_SHA256: {self.sums}"
        self.assertEqual(result.stdout.count(note), 1)
        configure = [ln for ln in log.splitlines()
                     if ln.startswith("cmake -B build")]
        self.assertEqual(configure, ["cmake -B build -DGGML_CUDA=1"])
        self.assertIn("cmake --build build -j --config Release "
                      "--target whisper-cli", log)
        curls = [ln for ln in log.splitlines() if ln.startswith("curl ")]
        self.assertEqual(len(curls), 2)
        self.assertTrue(all(ln.split()[1] == "-q" for ln in curls))
        self.assertTrue(all("-fL" in ln and "--proto" in ln for ln in curls))
        self.assertIn(
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
            "ggml-large-v3-turbo-q5_0.bin", curls[0])
        self.assertIn(
            "https://huggingface.co/ggml-org/whisper-vad/resolve/main/"
            "ggml-silero-v6.2.0.bin", curls[1])
        self.assertTrue(self.whisper_cli.is_file())
        for name in ("ggml-large-v3-turbo-q5_0.bin", "ggml-silero-v6.2.0.bin"):
            self.assertEqual((self.models_dir / name).read_bytes(), FAKE_BYTES)
        self.assertIn(f"ok    whisper.cpp at {self.checkout} ({DESCRIBE})",
                      result.stdout)
        self.assertIn("built with CUDA", result.stdout)

    def test_full_run_cpu_builds_without_cuda(self):
        settings = self.settings("cpu")
        result = self.run_setup(settings, self.make_bin(with_nvcc=True))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        configure = [ln for ln in self.log_text().splitlines()
                     if ln.startswith("cmake -B build")]
        self.assertEqual(configure, ["cmake -B build"])
        self.assertNotIn(CPU_FALLBACK, result.stdout)

    def test_auto_without_nvcc_builds_cpu_with_fallback_line(self):
        settings = self.settings("auto")
        result = self.run_setup(settings, self.make_bin(with_nvcc=False))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        configure = [ln for ln in self.log_text().splitlines()
                     if ln.startswith("cmake -B build")]
        self.assertEqual(configure, ["cmake -B build"])
        self.assertIn(CPU_FALLBACK, result.stdout.splitlines())

    def test_darwin_auto_with_nvcc_builds_metal_without_cuda(self):
        settings = self.settings("auto")
        result = self.run_setup(
            settings, self.make_bin(with_nvcc=True),
            env_extra={"TELEGRAM_PACK_PLATFORM": "darwin"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        configure = [ln for ln in self.log_text().splitlines()
                     if ln.startswith("cmake -B build")]
        self.assertEqual(configure, ["cmake -B build"])
        self.assertIn(
            f"ok    whisper-cli built with Metal at {self.whisper_cli}",
            result.stdout.splitlines())

    def test_darwin_dry_run_reports_metal(self):
        settings = self.settings("auto")
        result = self.run_setup(
            settings, self.make_bin(with_nvcc=True), "--dry-run",
            env_extra={"TELEGRAM_PACK_PLATFORM": "darwin"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            f"would build whisper-cli at {self.whisper_cli} with Metal "
            "(whisper.cpp's default on macOS)",
            result.stdout.splitlines())
        self.assertFalse(self.checkout.exists())

    def test_darwin_cpu_uses_plain_lines(self):
        settings = self.settings("cpu")
        result = self.run_setup(
            settings, self.make_bin(with_nvcc=True),
            env_extra={"TELEGRAM_PACK_PLATFORM": "darwin"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        configure = [ln for ln in self.log_text().splitlines()
                     if ln.startswith("cmake -B build")]
        self.assertEqual(configure, ["cmake -B build"])
        self.assertIn(f"ok    whisper-cli built at {self.whisper_cli}",
                      result.stdout.splitlines())
        self.assertNotIn("Metal", result.stdout)

    def test_darwin_without_sha256sum_uses_shasum(self):
        settings = self.settings("cpu")
        bin_dir = self.make_bin(omit=("sha256sum",), with_shasum=True)
        result = self.run_setup(
            settings, bin_dir,
            env_extra={"TELEGRAM_PACK_PLATFORM": "darwin"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok    shasum -a 256", result.stdout.splitlines())
        shasum_lines = [ln for ln in self.log_text().splitlines()
                        if ln.startswith("shasum ")]
        self.assertTrue(shasum_lines)
        self.assertTrue(all("-a 256" in ln for ln in shasum_lines))
        for name in ("ggml-large-v3-turbo-q5_0.bin", "ggml-silero-v6.2.0.bin"):
            self.assertEqual((self.models_dir / name).read_bytes(), FAKE_BYTES)

    def test_darwin_without_sha256sum_or_shasum_fails(self):
        settings = self.settings("auto")
        bin_dir = self.make_bin(omit=("sha256sum",))
        result = self.run_setup(
            settings, bin_dir,
            env_extra={"TELEGRAM_PACK_PLATFORM": "darwin"})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(
            "FAIL  sha256sum not found (install it with your package manager)",
            result.stdout)
        self.assertFalse(self.checkout.exists())
        self.assertNotIn("git clone", self.log_text())

    def test_uname_darwin_without_platform_reports_metal(self):
        settings = self.settings("auto")
        bin_dir = self.make_bin(with_nvcc=True)
        self._write_fake(bin_dir, "uname", UNAME_DARWIN_FAKE)
        for platform in (None, ""):
            with self.subTest(platform=platform):
                result = self.run_setup(
                    settings, bin_dir, "--dry-run",
                    env_extra={"TELEGRAM_PACK_PLATFORM": platform})
                self.assertEqual(result.returncode, 0,
                                 result.stdout + result.stderr)
                self.assertIn(
                    f"would build whisper-cli at {self.whisper_cli} with Metal "
                    "(whisper.cpp's default on macOS)",
                    result.stdout.splitlines())
                self.assertFalse(self.checkout.exists())

    def test_uname_linux_without_platform_uses_linux(self):
        settings = self.settings("auto")
        bin_dir = self.make_bin(with_nvcc=True)
        self._write_fake(bin_dir, "uname", UNAME_LINUX_FAKE)
        result = self.run_setup(
            settings, bin_dir, "--dry-run",
            env_extra={"TELEGRAM_PACK_PLATFORM": None})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            f"would build whisper-cli at {self.whisper_cli} "
            "with CUDA (-DGGML_CUDA=1)",
            result.stdout.splitlines())
        self.assertNotIn("Metal", result.stdout)
        self.assertFalse(self.checkout.exists())

    def test_second_run_is_all_ok(self):
        settings = self.settings("auto")
        bin_dir = self.make_bin(with_nvcc=True)
        first = self.run_setup(settings, bin_dir)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.clear_log()
        second = self.run_setup(settings, bin_dir)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        for line in second.stdout.strip().splitlines():
            self.assertTrue(
                line.startswith("ok    ") or line.startswith("next: ")
                or line.startswith("note  "),
                line)
        self.assertNotIn("FAIL", second.stdout)
        self.assertNotIn("would", second.stdout)
        log = self.log_text()
        self.assertNotIn("git clone", log)
        self.assertNotIn("cmake", log)
        self.assertNotIn("curl", log)

    def test_hash_mismatch_leaves_no_file(self):
        self.sums.write_text(
            f"{'0' * 64}  ggml-large-v3-turbo-q5_0.bin\n"
            f"{hashlib.sha256(FAKE_BYTES).hexdigest()}  "
            "ggml-silero-v6.2.0.bin\n")
        settings = self.settings("cpu")
        result = self.run_setup(settings, self.make_bin())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL", result.stdout)
        self.assertNotIn("next:", result.stdout)
        self.assertFalse(
            (self.models_dir / "ggml-large-v3-turbo-q5_0.bin").exists())
        self.assertEqual(list(self.models_dir.iterdir()), [])

    def test_missing_tool_fails_before_any_change(self):
        settings = self.settings("auto")
        result = self.run_setup(
            settings, self.make_bin(omit=("ffmpeg",)))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(
            "FAIL  ffmpeg not found (install it with your package manager)",
            result.stdout)
        self.assertFalse(self.checkout.exists())
        self.assertNotIn("git clone", self.log_text())
        self.assertNotIn("cmake", self.log_text())
        self.assertNotIn("curl", self.log_text())

    def test_transcription_off_skips(self):
        settings = self.settings(enabled=False)
        result = self.run_setup(settings, self.make_bin())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            result.stdout,
            f"note  checksum list from TELEGRAM_PACK_MODELS_SHA256: {self.sums}\n"
            f"skip  transcription is off in {settings}\n")
        self.assertEqual(self.log_text(), "")
        self.assertFalse(self.checkout.exists())

    def test_existing_bad_model_is_never_overwritten(self):
        self.models_dir.mkdir(parents=True)
        target = self.models_dir / "ggml-large-v3-turbo-q5_0.bin"
        target.write_bytes(b"bad bytes")
        settings = self.settings("cpu")
        result = self.run_setup(settings, self.make_bin())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL", result.stdout)
        self.assertEqual(target.read_bytes(), b"bad bytes")

    def test_unknown_model_fails(self):
        self.sums.write_text(
            f"{hashlib.sha256(FAKE_BYTES).hexdigest()}  "
            "ggml-silero-v6.2.0.bin\n")
        settings = self.settings("cpu")
        result = self.run_setup(settings, self.make_bin())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL", result.stdout)
        self.assertFalse(
            (self.models_dir / "ggml-large-v3-turbo-q5_0.bin").exists())

    def test_an_unlisted_local_model_is_reused_instead_of_failing(self):
        self.sums.write_text(
            f"{hashlib.sha256(FAKE_BYTES).hexdigest()}  "
            "ggml-silero-v6.2.0.bin\n")
        self.models_dir.mkdir(parents=True)
        target = self.models_dir / "ggml-large-v3-turbo-q5_0.bin"
        target.write_bytes(b"local model bytes")
        settings = self.settings("cpu")
        result = self.run_setup(settings, self.make_bin())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("local file, no recorded SHA-256; skipping hash check", result.stdout)
        self.assertEqual(target.read_bytes(), b"local model bytes")
        curls = [ln for ln in self.log_text().splitlines() if ln.startswith("curl ")]
        self.assertEqual(len(curls), 1)  # only the VAD model is downloaded

    def test_an_unlisted_local_vad_model_still_fails(self):
        self.sums.write_text(
            f"{hashlib.sha256(FAKE_BYTES).hexdigest()}  "
            "ggml-large-v3-turbo-q5_0.bin\n")
        self.models_dir.mkdir(parents=True)
        vad = self.models_dir / "ggml-silero-v6.2.0.bin"
        vad.write_bytes(b"local vad bytes")
        settings = self.settings("cpu")
        result = self.run_setup(settings, self.make_bin())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL  model", result.stdout)
        self.assertIn("no recorded SHA-256", result.stdout)
        self.assertEqual(vad.read_bytes(), b"local vad bytes")

    def test_failed_detach_removes_fresh_clone_and_retry_clones(self):
        settings = self.settings("cpu")
        bin_dir = self.make_bin()
        result = self.run_setup(
            settings, bin_dir, env_extra={"FAKE_GIT_FAIL_DETACH": "1"})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL", result.stdout)
        self.assertNotIn("next:", result.stdout)
        self.assertFalse(self.checkout.exists())
        self.clear_log()
        retry = self.run_setup(settings, bin_dir)
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertIn("git clone", self.log_text())
        self.assertIn(f"checkout --detach {PIN}", self.log_text())

    def test_default_ref_head_mismatch_fails_and_removes_checkout(self):
        settings = self.settings("cpu")
        head = "0" * 40
        result = self.run_setup(settings, self.make_bin(),
                                env_extra={"FAKE_GIT_HEAD": head})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(
            f"FAIL  whisper.cpp at {self.checkout} is {head}, not the pinned {PIN}",
            result.stdout)
        self.assertFalse(self.checkout.exists())
        self.assertNotIn("next:", result.stdout)

    def test_build_that_misses_configured_whisper_cli_fails(self):
        other = self.checkout / "build" / "bin" / "main"
        settings = self.settings("cpu", whisper_cli=other)
        result = self.run_setup(settings, self.make_bin())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL", result.stdout)
        self.assertIn("did not produce", result.stdout)
        self.assertNotIn("next:", result.stdout)
        self.assertFalse(other.exists())
        self.assertTrue(self.whisper_cli.is_file())
        self.assertNotIn("curl", self.log_text())

    def test_non_executable_whisper_cli_is_built(self):
        self.whisper_cli.parent.mkdir(parents=True)
        self.whisper_cli.write_text("not a binary\n")
        self.whisper_cli.chmod(0o644)
        settings = self.settings("cpu")
        result = self.run_setup(settings, self.make_bin())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cmake --build", self.log_text())
        self.assertTrue(os.access(self.whisper_cli, os.X_OK))

    def test_cuda_with_ninja_generator_configures_both(self):
        settings = self.settings("auto")
        bin_dir = self.make_bin(omit=("make",), with_ninja=True, with_nvcc=True)
        result = self.run_setup(settings, bin_dir)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        configure = [ln for ln in self.log_text().splitlines()
                     if ln.startswith("cmake -B build")]
        self.assertEqual(configure, ["cmake -B build -DGGML_CUDA=1 -G Ninja"])

    def test_ninja_only_host_configures_with_ninja_generator(self):
        settings = self.settings("cpu")
        bin_dir = self.make_bin(omit=("make",), with_ninja=True)
        result = self.run_setup(settings, bin_dir)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok    ninja", result.stdout)
        configure = [ln for ln in self.log_text().splitlines()
                     if ln.startswith("cmake -B build")]
        self.assertEqual(configure, ["cmake -B build -G Ninja"])

    def test_failed_move_fails_without_next(self):
        settings = self.settings("cpu")
        bin_dir = self.make_bin()
        (bin_dir / "mv").unlink()
        self._write_fake(bin_dir, "mv", "#!/bin/sh\nexit 1\n")
        result = self.run_setup(settings, bin_dir)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL", result.stdout)
        self.assertNotIn("next:", result.stdout)
        self.assertFalse(
            (self.models_dir / "ggml-large-v3-turbo-q5_0.bin").exists())
        self.assertEqual(list(self.models_dir.iterdir()), [])

    def test_interrupted_download_leaves_no_temp_file(self):
        settings = self.settings("cpu")
        result = self.run_setup(
            settings, self.make_bin(), env_extra={"FAKE_CURL_INT": "1"})
        self.assertEqual(result.returncode, 130, result.stdout + result.stderr)
        self.assertNotIn("next:", result.stdout)
        self.assertFalse(
            (self.models_dir / "ggml-large-v3-turbo-q5_0.bin").exists())
        self.assertEqual(list(self.models_dir.iterdir()), [])

    def test_whisper_ref_override_and_usage_errors(self):
        settings = self.settings("cpu")
        bin_dir = self.make_bin()
        result = self.run_setup(settings, bin_dir, "--whisper-ref", "v1.8.3",
                                env_extra={"FAKE_GIT_HEAD": "0" * 40})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("checkout --detach v1.8.3", self.log_text())
        self.assertNotIn("rev-parse", self.log_text())
        env = dict(HOME=str(self.home), PATH=str(bin_dir),
                   FAKE_LOG=str(self.log), LANG="C")
        cases = (
            [str(SETUP)],
            [str(SETUP), "--settings", str(settings), "--bogus"],
            [str(SETUP), "--settings"],
            [str(SETUP), "--settings", str(settings), "--whisper-ref"],
            [str(SETUP), "--settings", str(settings), "--whisper-ref="],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                done = subprocess.run(
                    argv, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 64)
                self.assertIn(USAGE, done.stderr)


if __name__ == "__main__":
    unittest.main()
