# telegram pack

This pack provides a private Telegram bridge for a Gas City. It connects the
paired chat with the mayor through a bridge process that each city runs for
itself.

(Terms such as the mayor, the paired chat and the ledger: `bridge/OPERATIONS.md`, "Terms".)

## What it does

- The paired user gets a private chat with the mayor for text in both
  directions, with Telegram HTML formatting on outbound text.
- The agent gets inbound photos, documents, videos, voice and audio files
  saved on the host, and can send photos, videos, documents, voice notes
  and round video notes to the chat.
- When transcription is enabled, the mayor gets each voice and audio note
  transcribed on the host with whisper.cpp in the configured language, with
  no cloud service. With `method = "auto"`, it uses the GPU when one is free.
- The mayor gets a delivery ledger that stops a retried send from posting
  twice and reports an unknown outcome instead of resending it.
- The paired user gets a slash command menu, and a city adds commands
  through its own handler programs that answer with text and inline
  buttons and need no enable flag.
- Each city gets its own bot, settings file, bridge process and delivery
  ledger on one host, and cities share one copy of the code and one
  whisper.cpp build.
- The operator gets setup checks from `install.sh` and `check-install`
  before the bridge runs, and a service for each city: a systemd user
  unit on Linux and WSL, a launchd LaunchAgent on macOS.
- The agent gets ready-made skills for Telegram formatting, attachments
  and voice transcripts.

| Path | What |
| --- | --- |
| `bridge/bridge.py` | the bridge: `run`, `probe`, `send`, `reply-current`, `set-commands`, `render-unit`, `check-install` |
| `bridge/OPERATIONS.md` | the operations guide: settings, launch, delivery recovery, transcription |
| `bridge/test_*.py` | `sh scripts/test.sh` (fake Telegram, GC and whisper-cli; no network) |
| `settings.example.toml` | every settings key, with example values |
| `install.sh` | checks a city's settings and host, links `voice-transcribe`, renders its service |
| `systemd/telegram-bridge.service.in` | the unit template |
| `transcriber/` | `voice_transcribe.py` (whisper.cpp, configured language), `adapter.py` (the bridge's contract), `models.sha256` |
| `commands/reply-current/` | `gc telegram reply-current`: reply in the paired chat |
| `skills/` | the shared skills: `telegram-formatting`, `telegram-attachments`, `telegram-voice-transcripts` |

## Prerequisites

- Linux: check `uname -s` prints `Linux`.
- Python 3.11 or later: check `python3 --version`.
- curl: check `curl --version`.
- gc 1.4.1 or later on PATH, its city running (`gc version`; tested on 1.5.0): check `gc version`.
- On Linux and WSL, a systemd user session (`systemctl --user`): check
  `systemctl --user status`; on macOS, launchd: check
  `launchctl print gui/$(id -u)`.
- For transcription also git, cmake, make or ninja, a C++ compiler, ffmpeg, curl, sha256sum, and network access to clone whisper.cpp and download the models (the default model `ggml-large-v3-turbo-q5_0.bin` is about 574 MB, plus the small VAD model): check `git --version`, `cmake --version` and `ffmpeg -version`.

## Adopt in a city

Each step says who does it: (you) for the Telegram app and BotFather, (shell) for a command in the user's terminal, (agent or shell) for what an agent may also do. An agent stops at a (you) step and asks the user.

For a city named `<city>` (gc's `workspace_name` in `<city>/.gc/site.toml`),
with this pack at `<pack>`:

1. (agent or shell) Import the pack in the city's `pack.toml`, either from a
   local path:

   ```toml
   [imports.telegram]
   source = "<pack>"
   ```

   or from the repository URL of this pack with a pinned version:

   ```toml
   [imports.telegram]
   source = "<repository URL of this pack>"
   version = "<tag>"
   ```

   This brings the `reply-current` command and the skills. Then run
   `gc reload`, and check that `gc config show` lists the telegram import.
2. (you) Create a bot with BotFather, open a private chat with it and send `/start`.
   Save the token owner-only: first
   `install -d -m 700 ~/.config/<city>`, then
   `install -m 600 /dev/stdin ~/.config/<city>/telegram-bot-token`, paste the token, Enter, then Ctrl-D
   (never in a shell argument or history). Check
   `ls -l ~/.config/<city>/telegram-bot-token` shows `-rw-------`.
3. (agent or shell, with the ids from step 2's chat) First read the ids without
   putting the token in argv or history, run before the bridge's first start:

   ```sh
   python3 - ~/.config/<city>/telegram-bot-token <<'EOF'
   import json, sys, urllib.request
   token = open(sys.argv[1]).read().strip()
   for method in ('getMe', 'getUpdates'):
       with urllib.request.urlopen(f'https://api.telegram.org/bot{token}/{method}', timeout=20) as answer:
           print(method, json.dumps(json.load(answer)['result'], indent=1))
   EOF
   ```

   Then write each value where it goes: getMe's `id` -> `[bot] bot_id`, its
   `username` -> `[bot] account_id`; in the `/start` update,
   `message.from.id` -> `[bot] user_id`, `message.chat.id` -> `[bot] chat_id`
   (equal to user_id), and its `update_id` -> `pairing_update_id`. Then write
   `~/.config/<city>/telegram-bridge.toml` from `settings.example.toml`,
   `chmod 600`: the city name, its own `state_dir` and `port`, the ids above,
   the token file, the menu, and `[transcribe] enabled = true` for voice
   transcription. For transcription, meet the transcription prerequisites
   above first, in this order: the settings file with
   `[transcribe] enabled = true` first, then
   `<pack>/transcriber/setup.sh --settings <file>` (`--dry-run` to preview).
   It clones whisper.cpp and downloads about 600 MB into
   `~/work/whisper.cpp` by default (set `whisper_cli` and `models_dir` to
   place the shared build elsewhere); success is its last line
   `next: <pack>/install.sh --settings <file>`. See
   `bridge/OPERATIONS.md`, "Voice-note transcription", for the queue and
   the GPU.
4. (agent or shell) Bind the conversation to the mayor once:
   `gc extmsg bind --provider telegram --account-id <bot username> --conversation-id <chat id> --agent mayor --json`.
   The printed record names the conversation and the agent mayor. There is
   no wildcard route, and `gc extmsg bind --help` lists every flag.
5. (shell) `<pack>/install.sh --settings ~/.config/<city>/telegram-bridge.toml`: it
   creates the callback secret and the shared queue directory if they are
   missing. On Linux and WSL it renders
   `~/.config/systemd/user/<city>-telegram.service`; the unit reads the
   settings and credential files only (they are read-only inside it)
   and writes its state directory, its lock directory, the queue and any
   `handler_write_paths`. Success: every check line says `ok`, and
   the last line is
   `next: python3 <pack>/bridge/bridge.py --settings <file> probe; systemctl --user daemon-reload; systemctl --user enable --now <unit>; python3 <pack>/bridge/bridge.py --settings <file> set-commands (README.md, "Adopt in a city", step 6)`.
   On macOS `install.sh` does not place the LaunchAgent and its last line
   still names the systemctl commands; render the agent instead with
   `mkdir -p ~/Library/LaunchAgents && python3 <pack>/bridge/bridge.py --settings <file> render-unit > ~/Library/LaunchAgents/<label>.plist`,
   where `<label>` is the unit name without its `.service` suffix.
6. (shell) `python3 <pack>/bridge/bridge.py --settings <file> probe`, then
   on Linux and WSL
   `systemctl --user daemon-reload && systemctl --user enable --now <city>-telegram.service`,
   on macOS
   `launchctl bootout gui/$(id -u)/<label> 2>/dev/null; launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`,
   then `python3 <pack>/bridge/bridge.py --settings <file> set-commands`.
   Success: after probe, its line `Telegram identity and webhook checks passed`;
   after `enable --now`, `systemctl --user status <city>-telegram.service`
   shows it active (on macOS, `launchctl print gui/$(id -u)/<label>`
   shows `state = running`) and
   `<state_dir>/telegram-bridge-heartbeat.json` appears
   within about a minute; after set-commands, its line
   `commands set: <count> (/<command> ...) for chat <chat_id>`.
7. (you, then the agent) Send a message from the chat and check the mayor
   received it; reply with `gc telegram reply-current` (the
   `telegram-formatting` skill). Success: the message reaches the mayor's
   session.

### A second bridge for a second agent

One city can run a second bridge so another of its agents has its own chat.
It is a separate bot with a separate settings file, sharing nothing with the
first bridge but the code: its own `port`, `state_dir`, `unit`, `token_file`
and `callback_secret_file`, and `agent = "<agent>"`, the gc agent it serves.
Bind its conversation to that agent with step 4's command, with
`--agent <agent>` instead of `--agent mayor` (step 4 with the second bot: the
chat id is the same user's, the account tells the two apart), install and
start its unit as in steps 5-6 (`unit` must differ from the first bridge's,
which the default `<city>-telegram.service` does not), and give that agent's
sessions `TELEGRAM_BRIDGE_SETTINGS=<its settings file>` in their
environment so their `gc telegram reply-current` answers through its bot. The
bridge accepts inbound only when GC routes it to its `agent`, and its
quota-exhausted notice watches that agent's session.

A city that transcribes some other way today (its own whisper.cpp command)
joins the host-wide queue only once it runs this pack's transcriber: the
bridge, or `voice-transcribe`, which `install.sh` links into `~/.local/bin`.
Until then the two do not wait for each other.

Run `install.sh` and the unit from a checkout path that stays put: the unit
names `bridge.py` by absolute path. A city that copies the pack runs its copy;
a city that imports it by path runs the shared one. Moving a city to this pack
from an older bridge copy: point `state_dir` at the old state directory so the
ledger, inbound offset and files carry over, keep the same `port`, and stop
the old unit before starting the new one (two pollers on one bot conflict).

## Uninstall

1. (shell) On Linux and WSL `systemctl --user disable --now <unit>`; on
   macOS `launchctl bootout gui/$(id -u)/<label>`.
2. (shell) On Linux and WSL
   `rm ~/.config/systemd/user/<unit> && systemctl --user daemon-reload`;
   on macOS `rm ~/Library/LaunchAgents/<label>.plist`.
3. (agent or shell) `gc extmsg unbind --provider telegram --account-id <bot username> --conversation-id <chat id> --agent <agent>`
4. (agent or shell) Remove the `[imports.telegram]` table and `gc reload`.
5. (shell, optional) Delete the city's settings file, token and callback secret, and its state_dir (the ledger: keep a private copy if the bridge may return).
6. (shell) Keep the host's shared pieces while any other city runs the pack: the queue `~/.local/state/voice-transcribe`, the whisper.cpp build and models, and `~/.local/bin/voice-transcribe`. Remove the link only when no city on the host runs the pack any more.
7. (you, optional) Delete the bot with BotFather.

## Skills

The pack's skills are the shared base set: universal, written for any city
and any bot, naming no city, person or product. Every city that imports the
pack gets them as `telegram.<name>`.

- **A city adds its own skill** in its own `skills/<name>/SKILL.md` (or its own
  pack), never by editing this pack: for example its menu commands' meaning,
  who reads the chat, or its message house style. Its skill may point at these
  ones by name.
- **Promoting a skill into the shared set**: when a city skill turns out to be
  true for every city (a Telegram rule, not a habit of one city or user),
  rewrite it without any city, person, product or path of that city, move it
  to `skills/<name>/SKILL.md` here with a citations table of the pack files it
  relies on in `citations.md` beside it (never in SKILL.md), and delete the city
  copy in the same change. Nothing in the pack names a city, person or company.

## Add-ons

An add-on is one `[[command]]` table in a city's settings, with no enable
flag. Its handler lives outside the pack: the city's own program, or one it
installs. Add one: install the handler at an absolute path (owner-writable
only, executable, owned by this user or root, in a directory owned by
this user or root and writable by neither group nor others), add the
table (and a `handler_write_paths` entry if it writes), then run
`install.sh` again, `systemctl --user daemon-reload`, restart the unit, and run
`set-commands`. Remove one: delete the table, delete the handler program if nothing else uses it, drop its `handler_write_paths` entry if no other handler writes there, run `install.sh` again, `systemctl --user daemon-reload`, restart the unit, then run `set-commands`. The menu loses the command only then.
The handler protocol is in `bridge/OPERATIONS.md`, "City commands".
One ready add-on is
[telegram-quota](https://github.com/codejunkienick/telegram-quota). It adds
a `/quota` command that shows how much of each AI subscription's usage
windows is spent, with the time left until each reset.

## Example

This is a partial file, only the keys that differ from settings.example.toml, which holds the full list.

`my-city` keeps its settings at `~/.config/my-city/telegram-bridge.toml`:

```toml
state_dir = "/home/<user>/my-city/state"
port = <port>
unit = "my-city-telegram.service"
[transcribe]
enabled = true
method = "auto"
[[command]]
name = "deploy"
description = "Deploy a branch"
handler = "/home/<user>/my-city/bin/telegram-deploy"
```
