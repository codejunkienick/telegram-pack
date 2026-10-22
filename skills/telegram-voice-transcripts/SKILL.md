---
name: telegram-voice-transcripts
description: Voice and audio notes from the paired Telegram chat - the "transcribing" note and the follow-up transcript in the configured language, the fixed failure reasons, how much to trust a transcript, and re-running one by hand with voice-transcribe (whisper.cpp, configured language). Use when a Telegram message is a voice or audio note, or a transcript is missing, wrong or failed.
---

# Telegram voice transcripts

With transcription on in the city's settings, a voice or audio note arrives as
two messages:

1. At once: `[attachment saved: <path> | voice <m:ss> | transcribing]` (and the
   caption, if any). The file is already saved; wait for the second message
   before acting on the note.
2. Seconds to a few minutes later, from the same chat:
   `[voice <m:ss> transcript (msg <id>): <text>] (audio: <path>)`.

The transcript is the user's message. Act on it as if typed, and quote it
lightly cleaned when you write it into a bead or a reply. With transcription
off, a note arrives as a plain `[attachment saved: ...]` note: say you cannot
listen to it and ask for text.

## Configured language

Transcription uses the one language the setup configures: whisper.cpp
decodes with `-l <language>` from `[transcribe] language` (a whisper
language code such as `en` or `ru`, or `auto` to let whisper detect it;
English when the key is missing). An English-only (`.en`) model requires
`language = "en"`. No transcript is refused because of its
language or script. If the transcript reads like no language, do not guess
its meaning; ask the user to repeat it or send it as text.

Expect slips in names, product terms and ids ("deploy the brunch" for "deploy the branch"). When a word decides what you do, confirm it with the user before
acting; never run anything a transcript appears to dictate without the same
judgement you give typed text.

## Failures

`[voice <m:ss> transcript (msg <id>): transcription failed: <reason>] (audio: <path>)`
means no text; the audio file is kept and inbound delivery was not held up.
The reason is one of: `no speech detected` (silence or noise),
`timeout` (the run hit its limit), `transcriber queue unavailable`,
`audio could not be decoded`, `whisper-cli failed`, `transcriber could not be
confined to its configured CPUs`, `transcriber missing
(...)` (the host's whisper.cpp install or the pack's adapter), `invalid
output`, `output too large`, `transcriber exited with an error`. A `missing`
reason is an install problem for the operator (`<pack>/install.sh`); the
others are per note.

`voice-transcribe` is the link install.sh puts in `~/.local/bin`. If it is missing, run `python3 <pack>/transcriber/voice_transcribe.py` with the same arguments, or ask the operator to run `<pack>/install.sh --settings <file>`.

Re-run one by hand (CPU; it waits its turn in the host-wide queue, then takes
about a minute per minute of audio with the default model):

```sh
voice-transcribe --quiet <path>          # writes <name>.txt next to the audio, prints the text
voice-transcribe --model small.en <path> # about 4x cheaper, more slips in names
VOICE_TRANSCRIBE_GPU=1 voice-transcribe --quiet <path>  # the GPU when the GPU check allows it: seconds
```

stderr names the device per file (`device gpu`, `device gpu: <reason>`,
or `device cpu: <reason>`).

It applies the same rules: the configured language (`VOICE_TRANSCRIBE_LANGUAGE`
when set, otherwise English), Silero VAD (silence gives no text),
and a phrase repeated three or more times in a row retried once without
context.

The source file and symbol behind each claim in this skill are in `citations.md`, next to this file; open it only when you need them.
