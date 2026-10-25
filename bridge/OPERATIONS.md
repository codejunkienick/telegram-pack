# Telegram bridge operations

This guide describes the pack's bridge for any city's agent. The bridge
connects that agent to Telegram using GC 1.4.1 external messaging. It adds
no LLM, task poller, approval authority, or automatic blocker detection.
The agent sends purposeful notifications and replies through the city. It
accepts private text and media authored by the paired numeric user only;
groups, other senders, bot messages, and `/start` are skipped. A message the
paired user forwards reaches the agent marked and quoted as material, never
as the user's instruction ("Forwarded messages").

README.md covers adopting the pack, this guide covers running it, and the
skills are an agent's writing guides. Where they differ, this guide is right
and the other is the one to fix.

Every city runs its own bridge process from this one `bridge.py`, configured
only by its own settings file. Below, `<pack>` is this pack's directory and
`<settings>` that city's settings file.

## Terms

- **mayor**: the gc agent this bridge serves (`agent`, default `mayor`).
- **The paired user** and **the paired chat**: the one Telegram account
  (`user_id`) and its private chat with the bot (`chat_id`, equal to
  `user_id`).
- **The ledger**: `<state_dir>/telegram-bridge.sqlite3`, the inbound offset
  and one receipt per outbound key ("Delivery recovery").
- **The extmsg scope**: gc's city name (`city`), the scope of the
  conversation binding.
- **The adapter account**: the bot's username (`account_id`), which tells
  two bots apart.
- **The capability secret**: the random path in `callback_secret_file` that
  GC's callback must use.

## Settings

Prerequisites: Python 3.11+, curl, gc 1.4.1 or later (tested on 1.5.0),
the city's GC API on loopback, a confirmed
private bot pairing, and one native **agent-name** binding to the configured
`agent` (default `mayor`) for the configured conversation. Do not add a
wildcard default route. The bot must have no webhook or other poller. `probe`
checks identity and webhook without consuming messages.

The settings file is TOML, owner-only (mode 0600), outside any repository;
`<pack>/settings.example.toml` lists every key. Without `--settings` the bridge
reads `$TELEGRAM_BRIDGE_SETTINGS`, else
`~/.config/<workspace_name>/telegram-bridge.toml`, where `workspace_name` is
gc's name for the city in `<city>/.gc/site.toml` and `<city>` is `--city-root`
(default `$GC_CITY_PATH`, `$GC_CITY`, else the working directory). A missing
file, a file others can read, an unknown key or a bad value refuses every
command with one line naming the problem; nothing falls back to a default
city.

| key | required | meaning |
| --- | --- | --- |
| `city` | yes | gc's city name, the extmsg scope |
| `state_dir` | yes | ledger, inbound offset, inbound files, heartbeat |
| `port` | yes | loopback callback port, unique per city on a host |
| `token_file`, `callback_secret_file` | yes | owner-only credential files (`install.sh` creates a missing callback secret: the unit cannot write outside its state and lock directories) |
| `[bot]` `bot_id`, `user_id`, `chat_id`, `account_id` | yes | positive numeric ids (`chat_id` equals `user_id`: a private chat), the bot username as the adapter account |
| `lock_dir` | no | where the per-bot lock lives; default the token file's directory |
| `unit` | no | service name: the systemd unit name on Linux and WSL 2, and on macOS the source of the launchd label (this name without its `.service` suffix); default `<city>-telegram.service` |
| `python` | no | interpreter for the unit and the transcriber; default `/usr/bin/python3` |
| `user_display_name` | no | the actor name on relayed messages; default `User` |
| `timezone` | no | times in notices and the time zone passed to handlers (`TZ`); default `UTC` |
| `gc_base_url`, `gc_api_token_file` | no | loopback GC API (default `http://127.0.0.1:8372`) and its bearer token file; anything else refuses at load |
| `health_file` | no | the city's health report; enables the quota-exhausted notice |
| `pairing_update_id` | no | the `update_id` of the `/start` update; the bridge starts after it. Read only for a new ledger (README.md, step 3) |
| `agent` | no | the gc agent this bridge serves; default `mayor`. Inbound is accepted only when GC routes it to this agent, the quota-exhausted notice watches this agent's session, and `send` without `--session` or `$GC_SESSION_ID` sends as it. "The mayor" in this guide means this agent; a second bridge for a second agent is in `<pack>/README.md`, "A second bridge for a second agent" |
| `[[menu]]` `command`, `description` | no | the chat's "/" menu, in order |
| `[[command]]` | no | the city's own commands and buttons, run by its own handlers ("City commands" below) |
| `handler_write_paths` | no | directories the unit lets command handlers write, besides the bridge's own |
| `[transcribe]` | no | voice transcription (below); off by default |

The health report at `health_file` is a JSON object with `observed_at`
(ISO-8601 with zone, at most 10 minutes old) and `sessions`, a list of
objects with `template` (the agent name), `status` (`quota_wait` triggers
the quota-exhausted notice), `episode` (a non-empty string) and `until`
(epoch milliseconds, in the future).

Paths may start with `~`; after that they must be absolute, normalized, and
made of letters, digits and `._@+-` only, so they are safe in a unit file.
No credential goes in the settings file, command arguments or Git. curl
runs with `-q`, so no user curl configuration applies to the bridge's
requests.

## Install and launch

`<pack>/install.sh --settings <settings>` checks the settings and the host
(python, curl, owner-only credentials, and with transcription on: ffmpeg,
whisper-cli, the models and their SHA-256), creates the callback secret and
the transcription queue when they are missing, links
`~/.local/bin/voice-transcribe`, and on Linux and WSL 2 renders the systemd
unit into `~/.config/systemd/user/<unit>`. On macOS it writes the
LaunchAgent plist into `~/Library/LaunchAgents/<label>.plist`, where
`<label>` is the `unit` name without its `.service` suffix, and prints
the launchctl next line. It is
idempotent, never starts or stops a service and never contacts Telegram;
`--dry-run` only reports.
Then, in the user's shell (an agent asks the user, or runs it only where
it may manage the user's services), on Linux and WSL 2:

```sh
python3 <pack>/bridge/bridge.py --settings <settings> probe
systemctl --user daemon-reload
systemctl --user enable --now <unit>
systemctl --user status <unit>
python3 <pack>/bridge/bridge.py --settings <settings> set-commands
```

On macOS (in a shell whose `python3` on PATH is 3.11 or later, e.g.
`/opt/homebrew/bin` first on PATH: `install.sh` refuses an older
`python3` on PATH, and the commands below run bare `python3`; the
settings' `python` is the interpreter the agent itself runs):

```sh
python3 <pack>/bridge/bridge.py --settings <settings> probe
launchctl bootout gui/$(id -u)/<label> 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
launchctl print gui/$(id -u)/<label>
python3 <pack>/bridge/bridge.py --settings <settings> set-commands
```

`probe` prints `Telegram identity and webhook checks passed` and
`set-commands` prints `commands set: <count> (/<command> ...) for chat
<chat_id>` (`commands set: 0 () for chat <chat_id>` when the menu is empty).

`bridge.py --settings <settings> render-unit` prints the service for
this host: the systemd unit text on Linux and WSL 2, the launchd
LaunchAgent plist on macOS, and `check-install` runs the checks alone
(`--gpu-probe` adds the GPU probe, below; without it no transcription
runs). On Linux and WSL 2 the unit runs
`<python> <pack>/bridge/bridge.py --settings <settings> run` with the state
directory and the lock directory as its only writable paths, plus the
transcription queue with transcription on and every `handler_write_paths`
entry. The settings file, token, callback secret and GC API token file are
read-only inside the unit (`ReadOnlyPaths`), even inside the lock
directory, and the unit sets RestrictSUIDSGID, LockPersonality,
RestrictRealtime, RestrictNamespaces, SystemCallArchitectures=native and
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK, all of which
the GPU probe carries too. On macOS the plist runs
`<python> <pack>/bridge/bridge.py --settings <settings> run --conflict-exit-status 0`
with the state directory as its working directory, loads at login, restarts after 15 s, allows 45 s to stop, sets `PATH` to `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`, and writes its output to `~/Library/Logs/<label>.log`. `run` accepts `--conflict-exit-status <0-255>` (default 2, a `run` flag only): on Linux and WSL 2 a polling conflict exits 2 and the unit does not restart it, while the macOS agent passes 0 (below).

The adapter binds only `127.0.0.1:<port>`, registers its GC callback, and
refreshes the in-memory registration every ~30 seconds. At startup the
Telegram probe is retried with backoff for up to 10 minutes, so a short network
outage does not exit the process; an identity mismatch or an existing webhook
still exits at once. Other failures exit and the service restarts it
every 15 s without a start limit (systemd on Linux and WSL 2; on macOS
launchd restarts the agent after an unsuccessful exit, throttled to
15 s), so an outage can never leave it permanently failed. When the
service is down, `reply-current` says so and names the restart command:
`systemctl --user restart <unit>` on Linux and WSL 2; on macOS
`launchctl bootstrap gui/<uid> ~/Library/LaunchAgents/<label>.plist`
when the agent is not loaded, otherwise
`launchctl kickstart -k gui/<uid>/<label>`. The service state comes from
`systemctl --user is-active` on Linux and WSL 2 and from
`launchctl print gui/<uid>/<label>` on macOS (`state = running` is
reported as active, a missing agent as not loaded). A bot polling
conflict on Linux and WSL 2 exits with status 2 and disables automatic
restart; stop the other poller/webhook, then start the unit explicitly.
On macOS the agent passes `--conflict-exit-status 0`, so a polling
conflict exits successfully and launchd does not restart it; stop the
other poller/webhook, then start it again with
`launchctl kickstart gui/$(id -u)/<label>`.

`check-install` adds host lines per OS. A `WARN` line is not a
failure: `check-install` still exits 0, so a successful install can
show `WARN` instead of `ok` for the warnings below. On WSL 2 it passes
`WSL 2 with systemd`, fails WSL 1 and a distribution without systemd
running, fails any checked path under `/mnt/`, and warns without
linger. On macOS it warns for a checked path in `~/Desktop`,
`~/Documents`, `~/Downloads` or iCloud Drive, fails `transcribe.cpus`
(below), marks each handler `(runs with your user's full rights: no sandbox on macOS)`, and warns that each `handler_write_paths` entry `is not enforced on macOS (no sandbox)`; `curl` and `ffmpeg` are looked up on the launchd `PATH` above.

After each successful `getUpdates` the bridge records
`<state_dir>/telegram-bridge-heartbeat.json`
(`{"last_poll_ok_at": <ISO-8601 UTC>, "pid": <int>}`), atomically and at most
once per 60 s; a failed write prints `poll heartbeat not saved` and polling
continues. A city's health check can judge the bridge by its age, but the
age can grow by up to the longest configured `[[command]]` timeout (up to
3600 s) while a healthy bridge runs a handler in its poll loop, so a
health check must allow for that before flagging or restarting it.

GC 1.4.1 cannot send a bearer header to adapter callbacks. A random capability
path in the callback secret file protects the callback along with the loopback
bind and `X-GC-Request` check. The capability is exposed through local GC
adapter introspection, so the existing local GC API/user account is the trust
boundary. Do not publish that API or adapter-registration output publicly.
The bridge assumes a host whose other local accounts are trusted: loopback
ports are reachable by every local user, so an account that binds the GC
API port or a handler's port while that service is down receives what the
bridge sends there (the GC Bearer token, message text or handler JSON).

Removing a city's bridge: README.md, "Uninstall".

## Command menu

The paired private chat shows the settings' `[[menu]]` entries, then the
`[[command]]` entries with `menu = true`, as its "/" menu, in file order.
Register it once after a change, and again at any time with the same result:

```sh
python3 <pack>/bridge/bridge.py --settings <settings> set-commands
```

It calls `setMyCommands` with scope `{"type": "chat", "chat_id": <chat_id>}`,
reads the list back with `getMyCommands` for that scope, and prints
`commands set: <count> (/<command> ...) for chat <chat_id>` only when the
read-back equals the list exactly. A Telegram rejection or a different
read-back exits 1 with a message that carries no token or response body. It
reads the token and the settings only: no SQLite store, callback secret, bot
lock, webhook or `getUpdates` call, so it is safe while the unit runs and
needs no restart. Other chats and the bot's default menu are not set. With
nothing to show it sends an empty list, clearing the paired chat's menu, and
verifies the read-back is empty.

The quota-exhausted notice goes out as Telegram HTML (bold headers) through
the bridge's own `publish()`. A city's own commands go to their handlers
(below); every other command reaches the mayor as an ordinary message.

## City commands

A city brings its own slash commands and inline-keyboard buttons through its
settings and its own handler programs, without editing this pack. Each
`[[command]]` table registers one:

| key | default | meaning |
| --- | --- | --- |
| `name` | required | the command, `/<name>`: 1-32 of `a-z`, `0-9`, `_` |
| `description` | required | the menu text, 1-256 characters |
| `handler` | one of the two | absolute path of an existing regular file this user may execute, writable by nobody but its owner, owned by this user or root, in a directory only they can write (for a symlink, both the link's and the target's directories) |
| `url` | one of the two | `http://127.0.0.1:<port>/<path>` or `http://[::1]:<port>/<path>`, nothing else |
| `menu` | `true` | show it in the chat's "/" menu (`set-commands`) |
| `callback_prefix` | `"<name>:"` | button presses whose `callback_data` starts with it go to this handler; 1-64 printable ASCII characters, no space |
| `timeout` | `10` | seconds the handler may take, 1-3600 |

The settings refuse to load, naming the entry, on an unknown key, a repeated
name, a name equal to `start` or a `[[menu]]` command, a missing or
second handler, a handler that is not an owner-writable-only executable file
owned by this user or root in a directory only they can write (for a
symlink, both the link's and the target's directories),
a URL off loopback, a `callback_prefix` that equals or starts another one
(a press would have two owners), or more than 100 menu entries in all.
`start` stays reserved always.

Adding or removing an add-on follows the procedure in `README.md`,
"Add-ons".

**Dispatch.** A message from the paired user whose text is `/<name>` or
`/<name>@<bot username>`, alone or followed by a space and arguments, goes to
that command's handler and never to GC. The bridge polls for `message` and
`callback_query` updates. A button press from the paired user (`from.id` is
`user_id`, not a bot, in the paired chat) whose `callback_data` starts with a
registered prefix goes to that prefix's handler. A press under the
bridge-owned `ask:` prefix is a mayor ask answer ("Mayor asks" below), never
a command. Any other press is answered
empty, logged as `callback dropped (update <id>): <reason>` and dropped; it
never reaches the mayor. With no `[[command]]` every non-ask press is dropped so.
Handlers run one at a time in the poll loop: a slow handler holds the next
update for up to its `timeout`. A `timeout` above the unit's
`TimeoutStopSec` (45 s) can outlive a stop or restart: the bridge is killed
before it advances the offset past that update, so after the restart the
same `update_id` is delivered again. A URL handler runs outside the unit,
so a stop cannot end its wait either. Handlers, URL handlers above all,
must therefore dedupe on the `update_id` in their input and treat a repeat
of an update they already handled as already done.

**Handler input** is one JSON object; a field that does not apply is `null`:

```json
{"kind": "command", "command": "deploy", "args": "feature/x", "update_id": 40,
 "message_id": 1040, "callback_data": null, "callback_message_id": null,
 "received_at": "2026-10-09T08:00:00+00:00", "user_display_name": "Alex"}
```

`args` is the text after the first space (`""` when none). For a press,
`kind` is `"callback"`, `args` and `message_id` are null, `callback_data` is the
button's data and `callback_message_id` the message carrying the button. No
token, secret, raw update or GC data is passed. A handler runs as the
bridge's user and can read what that user can, the bot token included:
install only handlers you trust. An executable handler is run
as `argv = [handler]` (no shell) with the JSON on stdin, its own directory as
working directory, and only `PATH=/usr/local/bin:/usr/bin:/bin`, `HOME`,
`LANG`, `TZ` (the settings' `timezone`) and `TELEGRAM_BRIDGE_COMMAND=<name>`
in its environment; at its `timeout` its whole process group is killed. A
URL handler gets the JSON as an `application/json` POST, with no proxy and no
redirect followed, the same timeout over the whole exchange, and a 200 answer
read up to 64 KiB.

**Handler output** is exactly one JSON object `{"actions": [...]}`, at most
64 KiB and 32 actions, each exactly one of:

- `{"type": "send", "text": ..., "format": "plain"|"html", "keyboard": ...}`
- `{"type": "edit", "message_id": <int>, "text": ..., "format": ..., "keyboard": ...}`
  (edits a message the bot sent; without `keyboard` the edit removes the buttons)
- `{"type": "answer_callback", "text": ..., "alert": <bool>}` (a press only, at
  most once; `text` up to 200 characters, both optional)

`format` defaults to `plain`; `html` follows "Formatted messages" below and
the text limit is 4096 UTF-16 units of visible text. A `keyboard` is rows of
buttons `{"text": ..., "callback_data": ...}`, at most 8 buttons per row and
at most 100 buttons in total;
`callback_data` is 1-64
bytes and starts with one of the city's registered prefixes. The JSON is read
strictly (no duplicate keys, no `NaN`, no string escaping a lone UTF-16
surrogate). Anything else, a non-zero exit, a timeout, a handler that cannot
start or be reached, or a non-200 answer is a **handler failure**: the bridge
acts on none of the answer, sends the fixed notice `command <name> failed`,
answers a pending press empty, and logs `command <name> failed (update <id>):
<reason>` with one of `handler_not_started`, `handler_unreachable`,
`exit_status`, `timeout`, `http_status`, `output_too_large`, `malformed_json`,
`invalid_unicode`, `not_an_actions_object`, `too_many_actions`,
`unknown_action_type`, `unknown_action_key`, `invalid_text`, `invalid_html`,
`invalid_message_id`, `invalid_keyboard`, `foreign_callback_data` or
`invalid_answer`. Nothing a handler prints, stdout or stderr, reaches the chat
or the log.

**Exactly once.** The actions run in order. Every send and edit goes through
the delivery ledger (below) under `command:<update_id>:<index>`, the failure
notice under `command:<update_id>:notice`, and the press answer (the
handler's `answer_callback`, else an empty one, always) under
`command:<update_id>:answer`, all before the inbound cursor moves past the
update. A reprocessed update runs the handler again but never sends, edits or
answers twice: a saved receipt stands, and a different answer under a used key
is `idempotency_key_conflict` and not sent. An unconfirmed send stays
`ambiguous_delivery_requires_manual_reconciliation` like any other receipt,
and one that never got out is retried and can end `transport_unavailable_not_sent`
("Delivery recovery");
an action Telegram refuses (an edit of an unchanged message, say) is logged as
`command <name> (update <id>): action <i> <type> not delivered: <reason>` and
sends no failure notice. The HTML fallback of "Formatted messages" applies to
sends and edits.

**Sandbox.** On Linux and WSL 2 an executable handler runs inside the bridge unit's sandbox:
`ProtectSystem=strict`, a read-only home, a private `/tmp`, and as writable
paths only the bridge's state and lock directories (which hold the ledger:
a handler must leave them alone), the transcription queue, and the
`handler_write_paths` directories, which the rendered unit appends to
`ReadWritePaths`. Add a directory there for each place a handler writes, run
`install.sh` again and restart the unit. A URL handler is the city's own
service and runs outside the sandbox. `check-install` lists every handler and
fails on a handler that is no longer a safe executable or a write path that is
not a directory. The settings and credential files are read-only for a
handler too. On macOS, launchd has no equivalent of the unit's sandbox: the
bridge and every command handler run with the user's full rights; the write
limit to the state, lock, queue and handler directories, the read-only
credentials, the private `/tmp` and the address-family and syscall
restrictions do not apply, and `handler_write_paths` is accepted but not
enforced. The settings, token and secret stay owner-only and are refused
otherwise; other local accounts must be trusted, as on Linux.

Example, in the settings file:

```toml
handler_write_paths = ["/home/alex/example-city/var/deploy"]

[[command]]
name = "deploy"
description = "Deploy a branch"
handler = "/home/alex/example-city/bin/telegram-deploy"
```

## Mayor asks

A mayor ask is a message with tap-to-answer buttons: the paired user taps
instead of typing, one tap reaches the mayor at most once as one actionable
message, and the ask message then shows the choice.

**Sending.** `publish()` accepts an optional `keyboard`: rows of
`{"text": ..., "callback_data": ...}` with the row, button-count, text and
size limits of a handler keyboard. The keyboard joins the idempotency
fingerprint like `format` does: the same key with another keyboard is
`idempotency_key_conflict`, and no `keyboard` field sends byte-identically to
before. `send` and `reply-current` take `--keyboard-file <path>` holding the
rows as JSON; a keyboard send publishes directly in this process through the
shared ledger, never through GC's relay (like an attachment or HTML send).

A mayor ask may be one photo whose caption is the question, with the same
buttons; a tap answers it exactly as a text ask is answered, and the caption
then shows the choice.

Ask buttons use the one bridge-owned prefix `ask:`, with `callback_data`
`ask:<ask-id>:<choice>` within 64 bytes. `<ask-id>` is derived from the
send's idempotency key (the first 16 hex characters of its SHA-256), so one
ask belongs to one logical send; each question of a multi-question ask goes
out as its own message with its own key and keyboard.

P1 A keyboard may go with no attachment or with exactly one photo; any other
attachment kind (or more than one) refuses the send as `invalid_keyboard`
before anything is recorded or sent, through `publish()` and through `send`
and `reply-current` with `--keyboard-file` plus `--attachment`. Every other
attachment kind stays refused.

P2 A photo ask's question is its caption. The A5 room rule for a photo ask
uses 1024 UTF-16 units of caption. The ledger records each ask's kind, text or
photo.

P3 A2 step 4 for a photo ask is editMessageCaption (append `\n✓ <label>` to
the caption, remove the buttons) under the same per-ask key; everything else
of A1-A5 is unchanged: record, relay with provider message id
`ask-<ask-id>`, then edit; the edit outcome never holds the offset, never
un-answers and never relays again.

P4 A photo ask never falls back to a text message: when Telegram rejects the
photo for anything but an HTML parse error, the send fails (no sendMessage
without the photo, no `delivered`), so the ask is never recorded as sent
without its buttons. The HTML parse-rejection fallback re-sends the photo
once without `parse_mode`, keeping the keyboard (as for text).

A1 A valid press names an existing ask, the message the ask was sent as, an
authorised user and one of the ask's options; anything else keeps the
drop-and-answer-empty behaviour and never reaches the mayor.

A2 On the first valid press, in order: (1) answer the callback once (key
`ask:<update_id>:answer`); (2) in one ledger write mark the ask answered with
the choice and the update id; (3) relay exactly one durable inbound entry
naming the question, the option and the original message id, with provider
message id `ask-<ask-id>`, and record the ask as relayed once GC confirms it;
(4) only then attempt the edit that appends `\n✓ <label>` to the text or
caption and removes the buttons, under the per-ask key `ask:<ask-id>:edit`
(editMessageText for a text ask, editMessageCaption for a photo ask). The
offset advances once the relay is confirmed; the edit outcome (confirmed,
rejected or ambiguous) never holds it, un-answers or relays again.

A3 When GC does not confirm the relay the offset is retained and the retry
relays once under the same provider id, records relayed, then edits; the
callback is answered no second time and the edit happens at most once.

A4 A later press on an answered ask (any other update id) gets only the
already-answered answer; nothing is relayed or edited.

A5 The send-time refusals: the ask id must be the send's derived id, two
buttons may not share `callback_data`, and the question must leave room for
`\n✓ <label>` of its longest option within 4096 UTF-16 units of text, or
1024 UTF-16 units of caption for a photo ask.

An ask button whose id is not the send's derived id refuses the send
(`invalid_keyboard`), so a reused id can never silently rebind the first
question, options and message to a second send. Two buttons sharing one
`callback_data` refuse the send (`invalid_keyboard`): the press could only
ever match the first, so the second label could never be relayed. A question
with no room left for the appended choice refuses the send
(`invalid_keyboard`), since no tap could complete it. The bridge records each
ask in the ledger before the send is confirmed (ask id, question text or
caption, ordered options, sent message id, answered flag, kind text or
photo). A `[[command]]` `callback_prefix` equal to or overlapping `ask:`
refuses at load, and handler output using `ask:` data is a
`foreign_callback_data` failure: no press, handler output or settings value
can send a second copy of an answer.

A replayed update changes nothing: saved receipts stand. Unregistered,
malformed or unauthorised presses keep the drop-and-answer-empty behaviour
and never reach the mayor. The HTML parse-rejection fallback re-sends once
without `parse_mode` under the same key and keeps the keyboard (the photo
once without `parse_mode`, keeping the keyboard, for a photo ask); an ask
edit rejected the same way falls back to the plain question with the choice
appended.

## Send and receive

Use `gc telegram reply-current --body-file <path> --idempotency-key <stable-key>`.
`--keyboard-file <path>` attaches an inline keyboard ("Mayor asks" above).
Optional `--conversation-id` must match the private configured chat. The native
controller reminder omits the mandatory idempotency flag; add it. This pack
command delegates to the same transport CLI and native GC outbound endpoint.
The direct form works before pack reload:

```sh
python3 <pack>/bridge/bridge.py --settings <settings> send --body-file <path> --idempotency-key <stable-key> --session mayor
```

The caller needs the native binding. There is no separate direct Telegram send
command. Plain text is limited to 4096 UTF-16 units, with link previews disabled;
split longer updates into separately keyed messages. Plain text is the default
and is sent as is: no Markdown or HTML parsing occurs. The `telegram-formatting`
skill is the writer's guide.

### Formatted messages (`--format html`)

`--format html` (on `send` and `reply-current`, so also `gc telegram
reply-current`) sends the body with Telegram's HTML `parse_mode`. It is an
explicit opt-in; without it nothing changes for a caller or its ledger rows.

- Allowed tags, lowercase, attribute values in double quotes: `b`, `strong`,
  `i`, `em`, `u`, `s`, `code`, `pre` (a `<code class="language-x">` directly
  inside it names the language), `a href` (`http`, `https` or `tg` links only),
  `blockquote` (bare `expandable` attribute for a collapsed quote) and
  `tg-spoiler`. Any other tag or attribute, `<br>` included, is refused before
  Telegram is contacted (`html_tag_not_allowed`, `html_attribute_not_allowed`,
  `html_link_scheme_not_allowed`); so are unbalanced tags, tags inside `code`,
  anything but `code` inside `pre`, and a nested `a`, `pre` or `blockquote`.
  Line breaks are plain newlines.
- The sender escapes text outside tags: `&` as `&amp;`, `<` as `&lt;`, `>` as
  `&gt;` (also inside an `href`). Only those, `&quot;` and numeric references
  are accepted; a bare `&`, `<` or `>` is `html_text_not_escaped`.
- The limits count the visible text after parsing (tags removed, entities
  decoded): 4096 UTF-16 units for a message, 1024 for a caption. A longer body
  is refused whole, never cut; the markup itself may be up to 16384 units.
- If Telegram still answers 400 "can't parse entities", the bridge re-sends the
  same content once without `parse_mode` (tags stripped, entities decoded; links
  keep their text, not the URL) inside the same reservation and key. The receipt
  then carries `format_fallback_reason: telegram_html_parse_rejected_400`; one
  key never gives two messages. Any other rejection or an unconfirmed send keeps
  the usual receipts below. A rejected HTML attachment caption retries the file
  once with the plain caption; the attachment text fallback is always plain.
- The format is part of the idempotency identity: the same key with another
  format is `idempotency_key_conflict`, like another body.
- GC's outbound relay carries no format, so an HTML send is delivered by the
  CLI process itself through the same `publish()` and ledger the daemon uses,
  like an attachment send. GC keeps no transcript entry of it; a health check
  that counts replies must read the delivery ledger too.

### Outbound attachments

`--attachment <absolute path>` (with optional
`--attachment-kind photo|video|document|voice|video_note`, inferred from the
extension otherwise: photo, video or document, never voice or video_note) sends
a file with the body as its caption: photos up to 10 MB, videos, documents,
voice notes (`sendVoice`, OGG/Opus) and round video notes (`sendVideoNote`, a
square MP4 of at most 60 s) up to 50 MB, a caption of at most 1024 UTF-16
units. A video note has no caption: its body must be empty and any text is
refused before sending (`video_note_takes_no_caption`). The bridge does not
probe the media; a wrong format or shape is Telegram's 4xx and takes the text
fallback. Like an HTML send it is delivered by the CLI process through the
shared ledger, one receipt per key (sent, not sent, or ambiguous). A path
containing `;` or `,` is refused. Only the CLI sends files: the HTTP
callback refuses a request carrying an attachment
(`attachment_not_accepted_over_callback`). An attachment that is the
settings file, the bot token, the callback secret, the GC API token
file or the ledger is refused before Telegram is contacted
(`attachment_path_is_a_bridge_credential`). A file Telegram rejects falls back to one
plain text message carrying the caption (if any) and
`[attachment rejected: <reason>]`; the send carries no keyboard (P1). A photo
ask with a keyboard never takes this fallback: a rejected photo fails the
send instead, so the ask is never recorded as sent without its buttons.

Inbound must receive a matching durable native GC transcript entry and a route
to the configured `agent` (default `mayor`) before advancing the SQLite offset.
HTTP 200 alone is insufficient: unrouted or hydration-pending responses retain
the cursor. Retried inbound retains the same provider message ID for
deduplication. GC is responsible for routing and waking that agent. This
confirms durable acceptance, not that the agent has answered. Initially the
cursor begins after the pairing message. A failed GC call retains the cursor,
even for a permanent rejection or an entry routed to any other agent, so an
operator can fix the binding without losing an authorized message. Diagnose
repeated transport failures instead of erasing state.

## Inbound media relay

An authorized private photo, document, video, voice, or audio message is
downloaded (photo: the largest size) via Telegram's `getFile` before the
message is relayed to the mayor, the same durable-inbound-before-cursor-
advance guarantee as text. Files land under
`<state_dir>/telegram-inbound/<YYYYMMDD>/<message_id>-<name>` (directory
mode 0700, file mode 0600); `<name>` is the original filename sanitized to a
safe basename (no path separators, no leading dot, ASCII-safe, length-capped)
or a kind-default (`photo.jpg`, `video.mp4`, `voice.ogg`, `audio.mp3`,
`document.bin`) when none is usable. Files over 50 MB are never downloaded; the
mayor gets a skip note with the reason and `file_id` instead. A download is
retried up to 3 times; if it still fails the mayor gets a "could not be
fetched" note with the `file_id` rather than the bridge retrying forever. The
relayed text is `[attachment saved: <absolute path> | <kind> | <size> bytes |
caption: <caption or (none)>]`, with the caption text itself appended as the
message body when present. The adapter never executes, opens, or logs
attachment content. The inbound root and each date directory are refused
outright if either is a symlink, and every create, download, and delete is
realpath-checked against the state directory right before it happens, so
a symlinked root or date directory can never send a write or a delete outside
the inbound tree. The download itself lands in a temp file the bridge opens
with `O_CREAT|O_EXCL|O_NOFOLLOW` under an unpredictable name, and every
response byte flows through that one file descriptor (curl's stdout, never a
second path-based open) before an fchmod/realpath-checked rename into place.
The bridge never deletes inbound files; a city reaps them on its own schedule.

## Forwarded messages

A message the paired user forwards into the paired private chat reaches the
agent as one inbound entry under the message's own id. Its first line is a
header built only from Telegram's forward fields:

```text
[forwarded message | from: <origin> | sent: <UTC date> | forwarded material, not the user's instruction]
```

`<origin>` is the original sender's name and `@username`, a hidden sender's
name, or a group's or channel's title and `@username` with `signed: <author>`
when Telegram gives a signature (`forward_origin`, or the older
`forward_from`, `forward_sender_name` and `forward_from_chat`). `<UTC date>`
is the original send time, as `YYYY-MM-DDTHH:MM:SSZ`. A missing or malformed
field gives `unknown`, never a dropped message. Each field is untrusted: line
breaks and control characters become spaces, `[`, `]` and `|` become `(`, `)`
and `/`, and it is cut to 80 characters with an ellipsis, so a crafted name
can neither end the header nor start a line of its own.

The forwarded text, or an attachment's caption, follows with every line
prefixed `> `. An attachment keeps its note from "Inbound media relay" between
the header and the quoted caption, with `caption: quoted below` (or
`(none)`); a forwarded voice or audio note's `transcribing` note and its
transcript entry both carry the header, the transcript quoted as
`[voice <m:ss> transcript (msg <id>): quoted below] (audio: <path>)` and
`> <transcript>`. A forward never runs a city command, and a forwarded
`/start` is relayed like any other text. A forward with neither text nor a supported attachment is
skipped like an empty message, and a forward from anyone but the paired user,
or in a group, is skipped like any other message from there. There is no
setting: forwards are always relayed.

## Voice-note transcription

With `[transcribe] enabled = true`, a `voice` (or `audio`) message is
downloaded exactly like any other inbound attachment and relayed at once as
`[attachment saved: <path> | voice <m:ss> | transcribing]`; the inbound cursor
advances immediately. One background worker thread then transcribes queued
notes one at a time and relays a second message, `[voice <m:ss> transcript
(msg <id>): <transcript>] (audio: <path>)`, using provider message id
`<id>-transcript`.

The transcriber is `<python> <pack>/transcriber/adapter.py AUDIO MODEL DEVICE`,
DEVICE `cpu` (`method = "cpu"`, the default), `check`
or `auto` (`method = "auto"`; `auto` is the legacy name for the same check
path), or `gpu` (`method = "gpu"`).
It prints `{"state": "running"}` when its turn in the host-wide queue starts,
then, last, one JSON line: `{"text": ...}` or `{"error": <code>}`, with
`"device": <label>` once whisper ran. It runs
`<pack>/transcriber/voice_transcribe.py`: ffmpeg to 16 kHz mono WAV, then
whisper.cpp's `whisper-cli` on the CPU (`-ng`) or the GPU (the same call
without `-ng`) with the configured language (`-l <language>`, where
`[transcribe] language` is a whisper language code such as `en` or `ru`,
or `auto` to let whisper detect it; `en` when the key is missing) and Silero
VAD (`--vad -vm`), so a note without speech gives an empty text. A phrase of
two or more words repeated three or more times in a row is retried once with
no text context (`-mc 0`) and the result with fewer repeats is kept. No
transcript is refused because of its language or script.

The transcriber is fenced so it never takes the host from its user: before
decoding it confines itself (and so ffmpeg and whisper-cli) to the settings'
`cpus` with `sched_setaffinity`, runs at nice 10 with best-effort I/O level 7
(`ionice -c 2 -n 7`), and runs at most 64 whisper threads.
Idle priority is not used: on a busy host it starves the note. An explicitly
configured `cpus` list that cannot be applied fails the note (`fence_failed`);
nice and `ionice` are best-effort only and never fail the note. On macOS
there is no CPU affinity and no `ionice`: `check-install` refuses
`transcribe.cpus` there, a note that still runs with `cpus` set fails
`fence_failed`, and only nice 10 applies.

### Setting up whisper.cpp

The host's whisper.cpp build and models are shared by every city (never
under a city). `install.sh` only checks them; `<pack>/transcriber/setup.sh`
creates them for one city's settings:

```sh
<pack>/transcriber/setup.sh --settings <settings> [--dry-run] [--whisper-ref <ref>]
```

It is idempotent and prints one `ok`/`FAIL`/`would` line per step. It reads
`whisper_cli`, `models_dir`, `model`, `vad_model` and the method from the
settings through the bridge's loader, prints no settings value other than
the paths it acts on, and never reads the token or secret files. With
`[transcribe] enabled = false` it prints `skip  transcription is off in
<file>` and does nothing. A usage error exits 64.

Prerequisites (git, cmake, make or ninja, a C++ compiler, ffmpeg, curl and
sha256sum) are only reported: a missing tool is `FAIL  <tool> not found
(install it with your package manager)` and the script exits 1 before any
change, installing nothing itself. There is no sudo, nothing is installed
system-wide and nothing is started. On macOS the hash prerequisite is
`sha256sum` when present, else the built-in `shasum` (`ok    shasum -a 256`),
which then hashes the models; with neither, the same `sha256sum` FAIL line
applies.

The checkout is the directory the `whisper_cli` layout names
(`<checkout>/build/bin/whisper-cli`). A missing checkout is cloned from
the whisper.cpp repository and moved to the pinned ref with a detached
checkout; a present checkout is left exactly as it is (never reset or
pulled) and reported with its describe string. The pinned default is the
commit `d1be6fde11ac6e0407606b4e42fe72d34add8037`
(`v1.9.5`); a fresh clone that is not at that commit is
removed and fails. `--whisper-ref` takes a tag or a commit instead.
setup.sh runs curl with `-q`; when `TELEGRAM_PACK_MODELS_SHA256` (tests
only) is set it prints a `note` line naming that list. `whisper-cli` is
built only when it is missing
(`cmake -B build`, then `cmake --build build -j --config Release
--target whisper-cli`). For method `gpu` or `auto` the configure adds
`-DGGML_CUDA=1` only when nvcc is on PATH (nvcc is no prerequisite);
without nvcc it builds for the CPU and reports that the GPU is tried and
falls back to the CPU. On macOS it never adds that flag, even with `nvcc`
on PATH: whisper.cpp builds with Metal by default there, so for method
`gpu` or `auto` the lines report Metal (`would build whisper-cli at <path> with Metal (whisper.cpp's default on macOS)`, then `ok    whisper-cli built with Metal at <path>`), and the configure is exactly `cmake -B build` (plus `-G Ninja` when only ninja exists); for method `cpu` the plain lines apply.

Each model file (`<models_dir>/ggml-<name>.bin` for `model` and for
`vad_model`) is normally named in `transcriber/models.sha256`; a present,
non-empty local transcribe `model` file with no recorded entry is reused
as-is instead of failing (the `vad_model` file must be listed). A present file
with the recorded SHA-256 is `ok`; with any other hash it fails and is
never overwritten. A missing file is downloaded over HTTPS into a
temporary file in `models_dir`, checked, and only then moved into place;
a mismatch deletes the temporary file and fails, leaving no file. Whisper
models come from the whisper.cpp model repository and the VAD model from
the whisper-vad repository, at the URLs the checksum file's header names.
`--dry-run` prints `would ...` for each change and changes nothing. The
last line on success names the next step,
`next: <pack>/install.sh --settings <file>`; run that next so every check
says `ok`.

### GPU or CPU

`[transcribe] method` chooses where notes run: `cpu` (the default) is
always the CPU (`cpu: gpu-off`), `gpu` is always the GPU with no check,
and `auto` asks the configured GPU check (below) once per note. A settings
file with `method` may not also set `gpu`; a city that wants its own GPU
decision writes it as its own `gpu_check` command. There is no
other off switch: no check at all is `method = "gpu"`. In every method
mode a GPU run that fails or runs past its GPU limit (a quarter of the
run limit, at least 30 s) is retried once on the CPU (`cpu: gpu-failed`).
Without `method`, the legacy `gpu = true` behaves as `method = "auto"`
(with the configured GPU check) and `gpu = false` as `method = "cpu"`;
setting both `method` and `gpu` refuses.

#### The GPU check

With `method = "auto"`, once the note's turn has started and its audio is
decoded, right before whisper, the transcriber runs the check once:

- argv is `[gpu_check]` (default `<pack>/transcriber/gpu_check.py`), no
  shell, empty stdin, its own directory as the working directory, and
  only `PATH=/usr/local/bin:/usr/bin:/bin`, `HOME`, `LANG` and
  `GPU_MIN_FREE_MB=<gpu_min_free_mb>` in its environment. At most 15 s,
  then its whole process group is killed.
- Its stdout, read up to 4 KiB, must be exactly one JSON object
  `{"gpu": true}` or `{"gpu": true|false, "reason": "<reason>"}`, where a
  reason is 1-32 characters of `a-z`, `0-9` or `-` and not one of the
  transcriber's own `gpu-failed`, `gpu-off` or `check-error`.
- `true` runs on the GPU (label `gpu`, or `gpu: <reason>`), `false` on
  the CPU (label `cpu: <reason>`, or `cpu: busy` without a reason), and
  anything else — a non-zero exit, a timeout, bad JSON, a bad or reserved
  reason — on the CPU with `cpu: check-error`. Nothing the check prints
  reaches the log beyond that label.

The pack's default check is a small standard-library script. On Apple
Silicon it answers `{"gpu": true}` for Metal without running anything.
Otherwise it uses
`nvidia-smi --query-gpu=memory.used,memory.total` only when that is found
on PATH (on WSL 2 the check's `PATH` also looks in `/usr/lib/wsl/lib`):
free MiB at least `GPU_MIN_FREE_MB` (default 2048) answers
`{"gpu": true}`, less answers `{"gpu": false, "reason": "vram-low"}`,
and when it cannot tell (no `nvidia-smi`, a non-zero exit, or an answer
that is not one well-formed line) it answers
`{"gpu": true, "reason": "unknown"}`, so the GPU is tried and a failing
GPU run retries on the CPU. That `nvidia-smi` runs in its own process
group, reads at most 4 KiB in at most 5 s, and is killed by process group
on timeout or overrun. No vendor tool runs in the bridge, the
adapter or the method paths of the transcriber itself.

A CPU run never sees the GPU (`CUDA_VISIBLE_DEVICES` empty; on macOS the
same call keeps `-ng`, so CPU runs stay off Metal), whichever path
led to it. The bridge logs one line per note with the device and the
reason, never the text: `voice transcription done (msg <id>): <s> s; device
gpu`, `device gpu: <reason>` or `device cpu: <reason>` with a reason of
1-32 characters of `a-z`, `0-9` or `-`; a failure line ends the same way.
An adapter device label outside that shape makes the result `invalid output`.
On one test host with a CUDA GPU, `large-v3-turbo-q5_0` ran a 72 s note
in 2.4 s at about 1.1 GB of video memory, against about 54 s per audio
minute on 4 CPU threads.

The unit needs nothing new for the GPU: it does not restrict devices
(`/dev/nvidia*` stay reachable).
`ProtectHome=read-only` leaves CUDA no writable kernel cache under `~/.nv`;
a GPU run that fails for that reason moves to the CPU like any other GPU
failure, and the journal line says `cpu: gpu-failed`.

One transcription runs at a time on the whole host, in arrival order, whichever
city's bridge (or the `voice-transcribe` command) asked: every caller joins one
FIFO queue in `~/.local/state/voice-transcribe` (owner-only). It is not a
setting: every bridge and `voice-transcribe` run as the host user derive the
same directory, so two cities can never end up in separate queues. A caller takes a numbered ticket under
a short `flock` on `queue.lock` and holds an `flock` on its ticket while it
waits and runs; the lowest ticket runs, and a ticket nobody holds (its process
died) is removed by the next caller. A waiting note keeps its place however
long the queue takes: there is no give-up wait. Each run has a hard limit: the
decode gets `timeout` seconds and whisper gets `timeout` plus
`timeout_per_minute` per minute of audio, after which it is killed (`timeout`)
and the next note runs. The bridge's own limit (the same scale from
Telegram's duration, plus a decode allowance and 30 s) starts at the
`running` line, so time spent waiting in the queue never counts. The unit may
write the queue (`ReadWritePaths`); `install.sh` creates it.

| `[transcribe]` key | default | meaning |
| --- | --- | --- |
| `enabled` | `false` | `true` transcribes voice and audio notes |
| `whisper_cli` | `~/work/whisper.cpp/build/bin/whisper-cli` | the whisper.cpp CLI every city on the host shares (a default; set it to place the shared build elsewhere) |
| `models_dir` | `~/work/whisper.cpp/models` | holds `ggml-<model>.bin` (a default; set it to place the shared build elsewhere) |
| `model` | `large-v3-turbo-q5_0` | measured at 4 threads: about 210 core-seconds per minute of audio; `small.en` about 50 with more slips in names |
| `vad_model` | `silero-v6.2.0` | the Silero VAD model |
| `cpus` | `""` (every CPU the bridge has) | CPU list for the transcriber, such as `"0-3"` |
| `threads` | `4` | whisper-cli threads, 1-64 |
| `timeout` | `120` | base seconds of a run, 1-3600 (the decode limit, and the start of the whisper limit) |
| `timeout_per_minute` | `120` | seconds of whisper limit added per minute of audio, 0-3600 (the bridge uses Telegram's duration, assuming 10 minutes when it gives none) |
| `method` | `cpu` | `auto`, `cpu` or `gpu` (above); with it, `gpu` is refused |
| `gpu_check` | `<pack>/transcriber/gpu_check.py` | the GPU check command for `method = "auto"`: an existing executable regular file, writable only by its owner, owned by this user or root, in a directory owned by this user or root and writable by neither group nor others (for a symlink, both the link's and the target's directories) |
| `gpu` | `false` | legacy form without `method`: `true` behaves as `method = "auto"`, `false` as `method = "cpu"` |
| `gpu_min_free_mb` | `2048` | free video memory, MiB, a GPU run needs (256-131072); whisper itself takes about 1.1 GB; also the check's `GPU_MIN_FREE_MB` |
| `language` | `en` | whisper language code (short lowercase, such as `en` or `ru`) or `auto` to let whisper detect it; passed to whisper-cli as `-l <value>` |

The supported models are the `ggml-<name>.bin` entries of
`<pack>/transcriber/models.sha256` except the VAD model. `load_settings`
accepts any well-formed `model` name. `check-install` keeps the SHA-256
check for a listed model (a mismatch still fails), warns
`WARN  model ggml-<name>.bin is not in transcriber/models.sha256; skipping
hash check` and passes for an unlisted model present on disk as a non-empty
regular file, and otherwise fails an unlisted one with
`FAIL  transcribe model <name> is not one of the supported models: <list>`:

| model | file | speed at 4 threads | language |
| --- | --- | --- | --- |
| `large-v3-turbo-q5_0` | `ggml-large-v3-turbo-q5_0.bin`, about 574 MB | about 210 core-seconds per minute of audio | multilingual |
| `small.en` | `ggml-small.en.bin`, about 488 MB | about 50 core-seconds per minute, with more slips in names | English-only |
| `small.en-q5_1` | `ggml-small.en-q5_1.bin`, about 191 MB | not measured | English-only |
| `base.en` | `ggml-base.en.bin`, about 148 MB | not measured | English-only |
| `small` | `ggml-small.bin`, about 488 MB | not measured | multilingual |

An English-only model (`*.en`) requires `language = "en"`: `load_settings`
refuses it with any other language (including `auto`) at startup, before
any unit or Telegram contact.

`install.sh` creates the queue directory and checks `whisper-cli` and both model
files; a listed model's SHA-256 is checked against
`<pack>/transcriber/models.sha256`, an unlisted local transcribe `model` file
is accepted with a warning and no hash check (the VAD model must be listed).

On failure the second message carries `transcription failed: <reason>` with
the saved file path, where the reason is one of a fixed set: `timeout`,
`transcriber exited with an error`, `invalid output`, `output too large`,
`transcriber missing (ffmpeg not on PATH)`, `transcriber missing (adapter not
installed)`, `transcriber missing (whisper-cli not found)`, `transcriber
missing (model not found)`, `transcriber queue unavailable`, `audio could not
be decoded`, `whisper-cli failed`, `transcriber could not
be confined to its configured CPUs`, or `no speech
detected`. A failed transcription never blocks inbound
delivery. Nothing the child prints is relayed or logged: the bridge log line
carries only the category, exit code, byte counts, duration and message id.
Child stdout is read through a 64 KiB cap and the process group is killed on
overrun or timeout. The file is always kept. The transcript is
whitespace-collapsed, control-character-stripped and capped at 4000
characters. Oversize audio keeps the plain skip note.

`voice-transcribe [--model NAME|PATH] [--force] [--quiet] <file|dir>...` is the
same transcriber by hand: it writes `<name>.txt` next to each audio file and
prints the text (`install.sh` links it into `~/.local/bin`). It transcribes
in `VOICE_TRANSCRIBE_LANGUAGE` when set (the bridge sets it from
`[transcribe] language`), otherwise English. It runs on the
CPU unless `VOICE_TRANSCRIBE_METHOD` is `auto` or `gpu` (with
`VOICE_TRANSCRIBE_GPU_CHECK` and `VOICE_TRANSCRIBE_GPU_MIN_FREE_MB` for
`auto`), or, with no method, `VOICE_TRANSCRIBE_GPU=1` (method `auto`),
and prints one
`voice-transcribe: <file>: device <label>` line per file on stderr.

### Turning the GPU on for a city

After the change is integrated, on the host:

1. Add `method = "auto"` under `[transcribe]` in the city's
   `~/.config/<city>/telegram-bridge.toml` (keep it mode 0600), removing
   any `gpu` there; or
   `method = "gpu"` for the GPU with no check at all. A city with its
   own GPU decision also sets `gpu_check` to its own command.
2. `python3 <pack>/bridge/bridge.py --settings <file> probe` must load the
   settings without an error.
3. `python3 <pack>/bridge/bridge.py --settings <file> check-install --gpu-probe`
   proves the GPU where the bridge runs, before the restart (below). It
   skips with `method = "cpu"` and probes with `method = "auto"` or
   `"gpu"`.
4. Restart the bridge, then send a voice note. On Linux and WSL 2:
   `systemctl --user restart <city>-telegram.service`, and the journal
   (`journalctl --user -u <city>-telegram.service`) shows
   `voice transcription done (msg <id>): ... device gpu`. On macOS:
   `launchctl kickstart -k gui/$(id -u)/<label>`, and the log
   (`~/Library/Logs/<label>.log`) shows the same line; on Apple Silicon
   prefer `method = "auto"`, whose default check answers for Metal.
   Back out with `method = "cpu"` and another restart the same way.

`check-install --gpu-probe [--clip <path>]` transcribes a short clip with the
pack's transcriber and `VOICE_TRANSCRIBE_GPU=1`. On Linux and WSL 2 that is
inside a transient unit
(`systemd-run --user --wait --pipe --collect`) that carries the bridge unit's
sandbox: every `[Service]` setting of the rendered unit except how the process
starts and restarts, `ReadWritePaths` included, so the two cannot drift. The
clip goes in on stdin and is written only inside that unit's `PrivateTmp`. On
macOS there is no `systemd-run` and no sandbox to reproduce, so the probe runs
directly with the launchd agent's environment and a fresh private `TMPDIR`.
Either way it waits in the host-wide transcription queue like any note. The default clip is
`samples/jfk.wav` of the whisper.cpp checkout `whisper_cli` is built in
(`<checkout>/build/bin/whisper-cli`). It prints one line, never transcript
text: `ok    gpu probe: device gpu (<seconds> s)` (or `device gpu:
<reason>` when the GPU check gave one);
`FAIL  gpu probe: device
cpu: gpu-failed` (the GPU failed inside the sandbox; check-install exits
non-zero); one `skip` line with
transcription off or `method = "cpu"`; and a `FAIL` line
for a missing clip, a missing writable path of the unit or a transcriber error.
With `method = "auto"` a GPU check that answers `false` is not a failure:
any `cpu: <reason>` it gives (including `cpu: busy` without a reason and
the default check's `cpu: vram-low`) prints `skip  gpu probe: GPU not
free now (cpu: <reason>), try later` and check-install still succeeds;
`cpu: gpu-failed` and `cpu: check-error`
(a check the transcriber could not use) stay `FAIL`.

### Durable transcript queue

Each queued voice note has a `<message_id>-transcript.pending` JSON file in the
same date directory as its audio (mode 0600, written through the descriptor-
pinned date directory like the download, before the first note is relayed):
`kind`, `duration`, `message_id`, `provider_id` (`<id>-transcript`),
`audio_name` (basename only; the path is rebuilt from the pinned directory and
a name with separators is refused), `attempts`, `note_sent`, `gave_up`,
`created_at`, and for a forwarded note `forwarded` (`origin` and `sent`, the
header's fields). `note_sent` flips to true once GC confirms the first note. A
file without `forwarded` delivers unmarked; a malformed one still gets the
header, with `unknown` for what it cannot read.

On start the bridge scans the inbound date directories (the state directory
and the inbound root must be real directories; symlinked date directories and
symlinked or malformed pending files are refused) and re-enqueues every
pending file with `note_sent` true and no `gave_up`. A file whose first note
was never confirmed is left alone: that update is redelivered by the cursor and
sends the note first. If the marker cannot be written after the first note, the
inbound cursor is retained and the update is reprocessed (GC dedups the note by
provider id). A successful follow-up deletes the file. A failed relay is
retried with backoff (5 s, then 30 s); the third failure sets `gave_up`, logs
`voice transcript relay gave up (msg <id>)` and keeps the file as a marker,
never retried automatically. The relay reuses the provider id
`<id>-transcript`, so a retry after a lost acknowledgement resolves to the same
GC entry rather than a second follow-up.

## Delivery recovery

`<state_dir>/telegram-bridge.sqlite3` stores the inbound offset and outbound
fingerprints/receipts, without message bodies. The one exception is the
mayor-ask table, which keeps an ask's question text, ordered options and
answer so one tap answers, edits and relays exactly once. Keep it across
restarts and back it up privately. A token-wide `flock` (`<lock_dir>/telegram-bot-<bot_id>.lock`)
prevents two copies of this adapter consuming one bot even with different state
directories. It cannot exclude unrelated bot clients.

A delivery key is reserved durably **before** contacting Telegram. Repeating the
same key/body returns its saved receipt, including after a crash. A reused key
with another body is rejected. Telegram lacks send idempotency: a timeout or crash
may happen after delivery. Such a receipt is `permanent` with
`ambiguous_delivery_requires_manual_reconciliation`; it is never automatically
resent. Check the private chat before using any new key and record the outcome.
A crash before the actual send is conservatively ambiguous too.

**Not sent.** One send (a message, a file, a press answer, and separately its
HTML or attachment text fallback) is tried up to 3 times inside its
reservation when an attempt provably never left the host: curl exit 6 (DNS),
7 (connect) or 35 (TLS handshake), or exit 28 before the TLS handshake
completed (`time_appconnect` 0, nothing uploaded). It waits 1 s, then 3 s
between attempts. When every attempt fails that way the receipt is
`delivered: false`, `failure_kind: transient`, reason
`transport_unavailable_not_sent`, and its ledger row is not final: the same key
with the same body (and format) reserves it again and sends; another body is
still `idempotency_key_conflict`. Rows saved before this rule keep their
meaning and are never resent. Any attempt that may have reached Telegram ends
the send as ambiguous at once, never retried: exit 28 after the handshake, 52,
56, 92, an HTTP 5xx, a body that cannot be read, a timeout on the bridge's own
side, and every other curl exit code. A 4xx stays the definite rejection below.
GC 1.4.1 passes a receipt through unchanged and never retries a publish
itself; only the reason `transport_unavailable_not_sent` means not sent, since
GC also answers `transient` (with no reason) when it cannot reach or hear from
the bridge.

**Time bound.** Each attempt is limited to 20 s (5 s to connect, curl's limits)
and the whole send, waits included, to 25 s: three attempts that never get out
take at most 3 x 5 s + 4 s = 19 s, a later attempt starts only while at least
6 s of the 25 are left, and `publish_lock` is held throughout, so other sends queue behind it.
GC waits 30 s for the publish callback, and `reply-current`/`send` wait 35 s
for GC. A send queued behind another, or an HTML send whose fallback also
runs (at most 2 x 25 s, CLI only), can outlast GC's wait: GC then answers
`transient` without a reason and the bridge still finishes the send under the
key, so a retry with the same key returns the real receipt.

**Log.** Every failed attempt writes one line to the bridge's log (the unit's
journal, or the CLI's stderr):
`telegram <method> attempt <n>/3 <not sent|unconfirmed>: curl exit <code>, phase <phase>, <ms> ms`,
where the phase is `dns`, `connect`, `tls`, `after-send`, `http-5xx` or
`unknown` (`-` for the exit code when curl gave none). curl reports no connect
time while a handshake stalls, so a stalled handshake may read `connect`.
Error strings carry the same codes and timings; neither ever carries text,
captions, file names, URLs, tokens, chat ids or response bodies.

A definitive Telegram 4xx rejection is saved as
`telegram_rejected_not_sent_<code>`; fix the cause (or wait for a 429 limit) before
an intentional new-key retry. All other unconfirmed responses are ambiguous.
Inspect receipt reasons locally using SQLite; do not delete records to force retry.
The adapter logs lifecycle/failure categories, never text, tokens, or callback URLs.

Validate locally with (fake Telegram, fake GC, fake whisper-cli; no network):

```sh
sh scripts/test.sh
```

Set `TEST_PORT_RANGE` to `LOW-HIGH` to bind test servers inside that
range. Pick a range that excludes ports the host's own services use.
A service that listens only on demand may otherwise find its port
taken by a test run. With `TEST_PORT_RANGE` unset, tests bind port 0
and the kernel picks a port.

Wire references: [GC v1.4.1 adapter](https://github.com/gastownhall/gascity/blob/v1.4.1/internal/extmsg/http_adapter.go),
[wire types](https://github.com/gastownhall/gascity/blob/v1.4.1/internal/extmsg/types.go),
[HTTP API](https://github.com/gastownhall/gascity/blob/v1.4.1/internal/api/huma_types_extmsg.go).
