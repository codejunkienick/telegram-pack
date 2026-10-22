# telegram-voice-transcripts citations

The source file and symbol behind the claims in SKILL.md, one row per claim.
The rows live only here; SKILL.md holds none.

```text
<!-- citations -->
| kind | path | symbol | note |
| pack | bridge/bridge.py | note = f'[attachment saved: {path} \| {kind} {duration} \| transcribing]' | first note |
| pack | bridge/bridge.py | return f'[{kind} {duration} transcript (msg {mid}): {body}] (audio: {path})' | follow-up |
| pack | bridge/bridge.py | body = transcript if reason is None else f'transcription failed: {reason}' | failure text |
| pack | bridge/bridge.py | 'VOICE_TRANSCRIBE_LANGUAGE': language, | configured language reaches the transcriber |
| pack | bridge/bridge.py | is English-only and cannot decode | English-only (.en) model requires language = "en" |
| pack | transcriber/voice_transcribe.py | DEFAULT_LANGUAGE = 'en' | default language |
| pack | transcriber/voice_transcribe.py | '-l', getattr(options, 'language', DEFAULT_LANGUAGE) | language passed to whisper-cli |
| pack | transcriber/voice_transcribe.py | '--vad', '-vm' | Silero VAD |
| pack | transcriber/voice_transcribe.py | LOOP_MIN_REPEATS = 3 | loop guard |
| pack | transcriber/voice_transcribe.py | argv += ['-mc', '0'] | retry without context |
| pack | install.sh | link="$HOME/.local/bin/voice-transcribe" | voice-transcribe link in ~/.local/bin |
| pack | install.sh | target="$pack/transcriber/voice_transcribe.py" | fallback script when the link is missing |
| pack | install.sh | install.sh --settings <file> [--dry-run] | operator runs install.sh with the settings file |
<!-- /citations -->
```
