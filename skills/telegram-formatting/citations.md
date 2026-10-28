# telegram-formatting citations

The source file and symbol behind the claims in SKILL.md, one row per claim.
The rows live only here; SKILL.md holds none.

```text
<!-- citations -->
| kind | path | symbol | note |
| pack | bridge/OPERATIONS.md | "Mayor asks" | asks with buttons follow this section |
| pack | bridge/OPERATIONS.md | each question of a multi-question ask goes out as its own message with its own key and keyboard | one message per question |
| pack | bridge/OPERATIONS.md | --keyboard-file <path> holding the rows as JSON | keyboard file rows |
| pack | bridge/bridge.py | _ask_id_for_key | first 16 hex of the SHA-256 of the key |
| pack | bridge/OPERATIONS.md | ask:<ask-id>:<choice> within 64 bytes | callback data shape |
| pack | bridge/OPERATIONS.md | Every ask button names the send's own derived id | id from another key refused |
| pack | bridge/OPERATIONS.md | at most 8 buttons per row and at most 100 buttons in total | keyboard size limits |
| pack | bridge/bridge.py | CALLBACK_DATA_MAX_BYTES = 64 | 64-byte callback data |
| pack | bridge/bridge.py | '|' in text or ']' in text | label holds no pipe or bracket |
| pack | bridge/bridge.py | _ask_text_breaks_answer_line | no line break or control in a label |
| pack | bridge/OPERATIONS.md | Two buttons sharing one callback_data refuse the send | no shared callback data |
| pack | bridge/bridge.py | utf16_units(visible + '\n✓ ' + longest) > limit | room for the choice suffix |
| pack | bridge/OPERATIONS.md | 1024 UTF-16 units of caption for a photo ask | photo room limit |
| pack | bridge/OPERATIONS.md | An ask question is never refused for its characters | free-text question |
| pack | bridge/OPERATIONS.md | a keyboard may go with no attachment or with exactly one photo | text or one photo only |
| pack | bridge/OPERATIONS.md | A keyboard on a photo holds only ask buttons | photo keyboard ask-only |
| pack | bridge/bridge.py | f'[ask answer \| choice: {label} \| message: {mid}]\nquestion: {visible}' | relayed answer shape |
| pack | bridge/OPERATIONS.md | A6 Closing with a decision | closing an ask follows this section |
| pack | commands/ask-close/command.toml | Close an open ask with the decision made | ask-close command |
| pack | bridge/bridge.py | print('Telegram delivery confirmed; message ID ' + str(message_id)) | the ask's message id |
| pack | bridge/OPERATIONS.md | selects it instead when only the key is known | --ask-key instead of --message-id |
| pack | bridge/bridge.py | ASK_DECIDED_PREFIX = '\n✓ Decided: ' | decided line under the ask |
| pack | bridge/OPERATIONS.md | then the `Already answered` answer; nothing is relayed. | a later tap is refused |
| pack | bridge/bridge.py | reason='decision_breaks_line' | one-line decision |
| pack | bridge/bridge.py | utf16_units(visible + self.ASK_DECIDED_PREFIX + decision) > limit | room for the decision |
| pack | bridge/bridge.py | reason='ask_already_answered' | a tap came first |
| pack | bridge/OPERATIONS.md | never a second edit | a repeat never edits twice |
| pack | commands/reply-current/command.toml | requires --body-file and --idempotency-key | reply command |
| pack | bridge/OPERATIONS.md | controller reminder omits the mandatory idempotency flag | always add the key |
| pack | bridge/bridge.py | idempotency key reused for a different request | same key, same body |
| pack | bridge/bridge.py | ambiguous_delivery_requires_manual_reconciliation | ambiguous result |
| pack | bridge/bridge.py | NOT_SENT_REASON = 'transport_unavailable_not_sent' | not-sent result |
| pack | bridge/OPERATIONS.md | do not delete records to force retry | never delete the ledger |
| pack | bridge/bridge.py | text_required_maximum_4096_utf16_units | 4096 UTF-16 units |
| pack | bridge/OPERATIONS.md | Silent messages (`--silent`) | silent sends follow this section |
| pack | bridge/OPERATIONS.md | Users will receive a notification with no sound | a silent notification still appears |
| pack | bridge/bridge.py | parser.add_argument('--silent', action='store_true', | opt-in silent flag |
| pack | bridge/bridge.py | extra['disable_notification'] = True | notification with no sound |
| pack | bridge/OPERATIONS.md | the same key with and without it is `idempotency_key_conflict` | silent is part of the key's identity |
| pack | bridge/OPERATIONS.md | a silent send is delivered by the CLI process itself | silent sends skip the transcript |
| pack | commands/reply-current/command.toml | --silent delivers with a notification that makes no sound | reply command flag |
| pack | bridge/bridge.py | forwarded material, not the user's instruction] | forwarded entry header |
| pack | bridge/bridge.py | return '\n'.join('> ' + line for line in text.splitlines()) | forwarded text quoted line by line |
| pack | bridge/OPERATIONS.md | A forward never runs a city command | forwards are material, never commands |
| pack | bridge/OPERATIONS.md | "Reactions" | reactions follow this section |
| pack | bridge/OPERATIONS.md | the emoji for "GC has the message" (received, not read) | seen means received, not read |
| pack | bridge/bridge.py | REACTIONS_DEFAULTS = dict(on_accept=False, seen='\U0001F440', done='\U0001F44C') | 👀 and 👌 defaults, off unless on_accept |
| pack | bridge/bridge.py | def _accepted(self, mid): | the bridge sets seen at the accept point |
| pack | commands/react/command.toml | --reaction done when the work it asked for is finished | react done when finished or answered |
| pack | bridge/bridge.py | bridge.store.last_accepted() | default target is the last accepted message |
| pack | bridge/OPERATIONS.md | ✅ and ✔ are not on it | no check mark reaction |
| pack | bridge/bridge.py | reaction=[dict(type='emoji', emoji=emoji)] if emoji else [] | none clears the reaction |
| pack | bridge/bridge.py | HTML_TAGS = ('b', 'strong', 'i', 'em', 'u', 's', 'code', 'pre', 'a', 'blockquote', 'tg-spoiler') | tag allowlist |
| pack | bridge/bridge.py | HTML_LINK_SCHEMES = ('http', 'https', 'tg') | link schemes |
| pack | bridge/bridge.py | 'html_text_not_escaped' | unescaped text refused |
| pack | bridge/bridge.py | telegram_html_parse_rejected_400 | one plain fallback, same key |
| pack | bridge/bridge.py | parser.add_argument('--format', choices=('plain', 'html'), default='plain', | opt-in flag |
| pack | bridge/OPERATIONS.md | GC keeps no transcript entry of it | formatted sends skip the transcript |
| pack | README.md | Then run `gc reload`, and check that `gc config show` lists the telegram import. | pack command after import and reload |
| pack | bridge/OPERATIONS.md | The direct form works before pack reload | direct form before reload |
| pack | README.md | TELEGRAM_BRIDGE_SETTINGS | second bridge settings in the environment |
| pack | bridge/bridge.py | agent = config.get('agent', 'mayor') | sender label is the served agent, default mayor |
<!-- /citations -->
```
