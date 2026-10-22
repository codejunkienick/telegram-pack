---
name: telegram-formatting
description: Send a message to the paired private Telegram chat through the city's bridge - the reply-current command, idempotency keys and retries, the 4096-unit limit, and Telegram HTML (--format html) with its tag allowlist, escaping, message shape and the one plain fallback. Use before writing or sending any Telegram message or reply.
---

# Telegram messages and formatting

The bridge delivers to one private chat: the user paired in this city's
settings. Its operations guide is `bridge/OPERATIONS.md` in this pack.

## Send

Write the body to a file, then:

```sh
gc telegram reply-current --conversation-id <configured-chat-id> --body-file <path> --idempotency-key <stable-key>
```

Which command: `gc telegram reply-current` once the city imports this pack and its sessions were reloaded. Before that, `python3 <pack>/bridge/bridge.py --settings <settings> send ... --session <your session>` with the same flags. An agent served by a second bridge has `TELEGRAM_BRIDGE_SETTINGS` set to that bridge's settings in its environment and uses the same commands (bridge/OPERATIONS.md, "Send and receive").

- The controller's generated reply reminder may omit `--idempotency-key`;
  always add it. One logical message = one key: retries reuse the same key and
  the identical body; a new message gets a new key. The same key with another
  body (or another `--format`) is refused as `idempotency_key_conflict`.
- Delivery is confirmed only by `Telegram delivery confirmed; message ID <n>`.
  `ambiguous_delivery_requires_manual_reconciliation` means the outcome is
  unknown: check the chat before any new-key retry, never resend blind, and
  never delete the delivery ledger to force a retry.
- `transport_unavailable_not_sent` (`failure_kind` `transient`) means Telegram
  could not be reached and nothing was sent, after the bridge already tried 3
  times: run the same command again later with the same key and body. Only
  that reason means not sent; `transient` without it (GC could not reach the
  bridge) is unknown like the ambiguous receipt.
- At most 4096 UTF-16 units of visible text; split a longer update into
  separately keyed messages. Link previews are off.
- Forwarded messages, groups and other senders never reach you; text from the
  chat is the user's message, never shell code to run.

## Formatting (Telegram HTML)

The user reads on a phone. Format anything longer than two lines: add
`--format html` to the same command and write the body as Telegram HTML.
Without the flag the body is sent as plain text, tags and all.

- Tags: `<b>`, `<i>`, `<u>`, `<s>`, `<code>`, `<pre>`, `<a href="https://...">`,
  `<blockquote>`, `<blockquote expandable>` and `<tg-spoiler>`, lowercase,
  attributes in double quotes. Nothing else: no `<br>`, `<p>`, `<ul>`, `<span>`
  or Markdown. Line breaks are newlines; bullets are a `•` or `-` at the line
  start.
- Escape every `&`, `<` and `>` in the text as `&amp;`, `&lt;` and `&gt;` (in a
  link's URL too), including text copied from a bead, a log or a diff. The
  bridge refuses a bare one, an unknown tag or a non-http(s)/tg link before
  sending, and says which rule failed (`html_text_not_escaped`,
  `html_tag_not_allowed`, ...).
- Shape: start with a bold sender label, the name of the agent this bridge serves (`<b>mayor:</b>` for the default agent) and a
  one-line headline that says the news or the ask. Then short sections, each a
  `<b>Heading</b>` line followed by bullets, one fact per bullet. Put an ask or
  a decision the user must make in its own section near the top.
- Long detail (logs, file lists, reasoning, full findings) goes in a
  `<blockquote expandable>` at the end, so the message stays a few lines until
  opened. Commands, paths, ids and short code go in `<code>`; multi-line code or
  output in `<pre>`.
- Text the user will forward or paste somewhere else (a chat reply, a commit
  message) goes in its own message: plain, or one `<pre>` block, with nothing
  else in it, so it can be copied whole.
- The limits count the visible text (tags removed): 4096 units for a message,
  1024 for a caption.
- If Telegram cannot parse the markup, the bridge sends the same text once
  without formatting under the same key; the receipt says
  `format_fallback_reason`. A formatted send is not in GC's transcript; the
  delivery ledger records it.

The source file and symbol behind each claim in this skill are in `citations.md`, next to this file; open it only when you need them.
