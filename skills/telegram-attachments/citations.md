# telegram-attachments citations

The source file and symbol behind the claims in SKILL.md, one row per claim.
The rows live only here; SKILL.md holds none.

```text
<!-- citations -->
| kind | path | symbol | note |
| pack | bridge/bridge.py | def infer_attachment_kind(path): | kind from extension |
| pack | bridge/bridge.py | 'photo': ('sendPhoto', 'photo', 10 * 1024 * 1024), | size caps |
| pack | bridge/bridge.py | caption_exceeds_1024_utf16_units | caption cap |
| pack | bridge/bridge.py | 'video_note': ('sendVideoNote', 'video_note', 50 * 1024 * 1024), | voice and video note kinds |
| pack | bridge/bridge.py | video_note_takes_no_caption | no caption on a video note |
| pack | bridge/bridge.py | attachment_path_unsupported_characters | no ; or , in the path |
| pack | bridge/bridge.py | + f'[{what} rejected: {reason}]', 4096) | text fallback |
| pack | bridge/OPERATIONS.md | **Albums.** | albums follow this section |
| pack | bridge/bridge.py | ALBUM_FILES = (2, 10) | 2-10 files |
| pack | bridge/bridge.py | given 2-10 times, the files go as one album captioned by the body | repeat --attachment |
| pack | bridge/OPERATIONS.md | The body is the caption of the first item only | caption on the first item |
| pack | bridge/OPERATIONS.md | Photos and videos may mix; documents group only with documents. | album grouping |
| pack | bridge/bridge.py | album_kind_not_groupable | no voice or video note in an album |
| pack | bridge/OPERATIONS.md | `--keyboard-file` with an album refuses as `invalid_keyboard` (P1): | no buttons in an album |
| pack | bridge/OPERATIONS.md | ## Topics | topic sends follow this section |
| pack | bridge/OPERATIONS.md | a decimal string as a multipart field of | files and albums carry the topic's thread |
| pack | bridge/OPERATIONS.md | an album's text fallback) | the text fallback stays in the topic |
| pack | bridge/OPERATIONS.md | Without either, a send lands in General as before. | no flag is the main chat |
| pack | bridge/bridge.py | base = self.state_dir / 'telegram-inbound' | inbound folder |
| pack | bridge/bridge.py | note = f'[attachment saved: {path} \| {kind} \| {size} bytes \| caption: {caption_display}]' | saved note |
| pack | bridge/bridge.py | INBOUND_MAX_BYTES = 50 * 1024 * 1024 | 50 MB inbound cap |
| pack | bridge/bridge.py | INBOUND_DOWNLOAD_ATTEMPTS = 3 | three fetch attempts |
| pack | README.md | Then run `gc reload`, and check that `gc config show` lists the telegram import. | pack command after import and reload |
| pack | bridge/OPERATIONS.md | The direct form works before pack reload | direct form before reload |
| pack | README.md | TELEGRAM_BRIDGE_SETTINGS | second bridge settings in the environment |
| pack | commands/reply-current/run.sh | exec python3 "$pack/bridge/bridge.py" --city-root "$city" reply-current "$@" | city-root form is the pack command's direct form |
<!-- /citations -->
```
