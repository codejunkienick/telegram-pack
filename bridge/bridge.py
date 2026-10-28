#!/usr/bin/env python3
"""Private Telegram transport for GC 1.4.1; no agent or decision engine.

One process per city, configured only by that city's settings file
(settings.example.toml in this pack); nothing city-specific lives here."""
import argparse
import contextlib
import datetime
import fcntl
import getpass
import hashlib
import hmac
import importlib.util
from html import escape as html_escape
from html import unescape as html_unescape
import http.client
import http.server
import json
import math
import os
from pathlib import Path
import plistlib
import queue
import re
import secrets
import selectors
import shlex
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from urllib.parse import quote, urlsplit
import zoneinfo


PROBE_RETRY_BUDGET = 600
PROBE_RETRY_FIRST_DELAY = 5
PROBE_RETRY_MAX_DELAY = 60
CURL_CONNECT_TIMEOUT = 5
# One outbound Telegram send (a message, a file, a press answer): attempts that
# failed before any request byte left this host are tried again inside the
# send's reservation, SEND_ATTEMPTS in all with SEND_RETRY_DELAYS seconds
# between them, each limited to SEND_ATTEMPT_TIMEOUT. Every attempt and wait
# of the send fits in SEND_BUDGET seconds, below GC's 30 s wait on the publish
# callback; an attempt after the first starts only with SEND_MIN_ATTEMPT
# seconds left, so a short limit never cuts a request that already went out
# into an ambiguous one.
SEND_ATTEMPTS = 3
SEND_RETRY_DELAYS = (1, 3)
SEND_ATTEMPT_TIMEOUT = 20
SEND_BUDGET = 25
SEND_MIN_ATTEMPT = 6
# GC waits 30 s for the bridge's publish callback; the CLI's relay to GC waits
# longer, so it reads GC's verdict instead of timing out first.
GC_OUTBOUND_TIMEOUT = 35
# The receipt of a send whose every attempt failed before sending: delivered
# false, failure_kind transient. Its ledger row is not final.
NOT_SENT_REASON = 'transport_unavailable_not_sent'
# Reactions on the paired user's messages ([reactions]): none unless
# on_accept is set or an agent asks. Telegram's reaction list has no check
# mark, so "done" defaults to the OK hand.
REACTIONS_KEYS = {'on_accept', 'seen', 'done'}
REACTIONS_DEFAULTS = dict(on_accept=False, seen='\U0001F440', done='\U0001F44C')
REACTION_MAX_CHARS = 16
# The typing indicator ([typing]): none unless seconds > 0. Telegram shows a
# chat action for at most 5 s, so the worker repeats it every 4 s; it looks
# at the ledger every second for the send that ends the window.
TYPING_KEYS = {'seconds'}
TYPING_DEFAULTS = dict(seconds=0)
TYPING_MAX_SECONDS = 900
TYPING_INTERVAL = 4
TYPING_TICK = 1
TYPING_TIMEOUT = 5


def valid_reaction_emoji(value):
    """One reaction emoji as the settings and `react` accept it: a non-empty
    string of at most 16 characters, no whitespace, control character or ASCII
    letter or digit. Whether Telegram allows it is Telegram's 400 to say."""
    return (isinstance(value, str) and 0 < len(value) <= REACTION_MAX_CHARS
            and not any(c.isspace() or (c.isascii() and c.isalnum()) for c in value)
            and not Bridge._CONTROL_RE.search(value))
PACK_DIR = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True  # the pack directory is read-only under the unit
_transcriber_spec = importlib.util.spec_from_file_location(
    'voice_transcribe', PACK_DIR / 'transcriber' / 'voice_transcribe.py')
voice_transcribe = importlib.util.module_from_spec(_transcriber_spec)
_transcriber_spec.loader.exec_module(voice_transcribe)
SETTINGS_ENV = 'TELEGRAM_BRIDGE_SETTINGS'
SETTINGS_NAME = 'telegram-bridge.toml'
LAUNCHD_PATH = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
# macOS maps these leading paths onto /private themselves (/var is a link
# to /private/var, and the temporary directory lives under it); a runtime
# reached through exactly that mapping is still its own directory. Any
# other symlinked ancestor is a user-made link and is refused, on darwin
# as on Linux. Tests patch this table to their own temporary roots.
DARWIN_SYSTEM_ALIASES = {'/var': '/private/var', '/tmp': '/private/tmp', '/etc': '/private/etc'}


NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')


class TransportError(Exception):
    """Deliberately carries no remote body, URL, credential, or message text."""


class PollConflict(TransportError):
    pass


class ProbeRejected(TransportError):
    """Telegram answered, and the answer is a definite no: retrying cannot help."""


class TransportFailure(TransportError):
    """A classified transfer failure: curl's exit code (None when curl gave
    none) and the phase it failed in, never a URL, body or credential."""

    def __init__(self, message, exit_code=None, phase='unknown'):
        super().__init__(message)
        self.exit_code, self.phase = exit_code, phase


class RequestNotSent(TransportFailure):
    """No request byte left this host (DNS, connect or TLS handshake failed):
    the same request may be sent again without risking a duplicate."""


class RequestUnconfirmed(TransportFailure):
    """The request may have reached the server: never sent again."""


def _is_owner_only(st, kind):
    return kind(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077


def _is_owner_only_regular(st):
    return _is_owner_only(st, stat.S_ISREG)


def _is_owner_only_directory(st):
    return _is_owner_only(st, stat.S_ISDIR)


def private_text(path):
    path = Path(path).expanduser()
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    st = os.fstat(fd)
    if not _is_owner_only_regular(st):
        os.close(fd)
        raise TransportError('credential must be an owner-only regular file')
    with os.fdopen(fd) as stream:
        value = stream.read().strip()
    if not value or any(c.isspace() for c in value):
        raise TransportError('credential is empty or malformed')
    return value


def capability(path):
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return private_text(path)
    with os.fdopen(fd, 'w') as stream:
        stream.write(secrets.token_urlsafe(32))
        stream.flush()
        os.fsync(stream.fileno())
    return private_text(path)


# curl's exit codes for a failure before any request byte was sent, by phase.
CURL_NOT_SENT_EXITS = {6: 'dns', 7: 'connect', 35: 'tls'}
# curl's exit codes for a failure after the request may have gone out.
CURL_AFTER_SEND_EXITS = (28, 52, 56, 92)
# The last stdout line curl writes after the body, on success and failure alike.
_CURL_WRITE_OUT = '\n%{http_code} %{time_connect} %{time_appconnect} %{size_upload}'
_CURL_METRICS_RE = re.compile(r'([0-9]{3}) ([0-9]+\.[0-9]+) ([0-9]+\.[0-9]+) ([0-9]+)')


def classify_transfer(exit_code, http_code=0, time_connect=None, time_appconnect=None, size_upload=None, tls=True):
    """None for a transfer that completed below HTTP 500; else (not_sent, phase).

    Not sent (safe to send again): curl exit 6, 7 or 35, and exit 28 on an
    https URL whose TLS handshake never completed (time_appconnect 0) with
    nothing uploaded. Exit 6, 7 and 35 count only while the metrics show no
    completed handshake and no upload. Everything else may have reached the
    server: exit 28 after the handshake, 52, 56, 92, an HTTP 5xx, and every
    exit code not listed above or unreadable metrics (a duplicate is worse
    than a resend). The transport raises RequestUnconfirmed for a Python-side
    timeout and for an unreadable or oversized body after exit 0 itself."""
    if exit_code == 0:
        return (False, 'http-5xx') if http_code >= 500 else None
    shaken = bool(time_appconnect)
    uploaded = bool(size_upload)
    if exit_code in CURL_NOT_SENT_EXITS and not shaken and not uploaded:
        return True, CURL_NOT_SENT_EXITS[exit_code]
    if exit_code == 28 and tls and time_appconnect == 0 and size_upload == 0:
        # curl leaves time_connect at 0 while a TLS handshake stalls, so
        # 'connect' may also be a stalled handshake.
        return True, 'tls' if time_connect else 'connect'
    if exit_code in CURL_AFTER_SEND_EXITS or shaken or uploaded:
        return False, 'after-send'
    return False, 'unknown'


def _curl_metrics(line):
    match = _CURL_METRICS_RE.fullmatch(line)
    if not match:
        return None
    return dict(http_code=int(match.group(1)), time_connect=float(match.group(2)),
                time_appconnect=float(match.group(3)), size_upload=int(match.group(4)))


def _seconds(value):
    return str(value) if isinstance(value, int) else f'{value:.3f}'


def _curl(argv, config, url, timeout):
    """Run curl with its config on stdin; (status, parsed JSON body) for a
    completed transfer, a classified TransportFailure otherwise. Errors carry
    curl's exit code, HTTP code and timings only: never the URL, body or
    curl's own message."""
    try:
        result = subprocess.run(
            ['curl', '-q', '--silent', '--show-error', '--connect-timeout', str(CURL_CONNECT_TIMEOUT),
             '--max-time', _seconds(timeout), *argv, '--config', '-', '--write-out', _CURL_WRITE_OUT],
            input='\n'.join(config), capture_output=True, text=True, timeout=timeout + 2,
        )
    except subprocess.TimeoutExpired:
        raise RequestUnconfirmed('network request timed out; outcome unconfirmed') from None
    except (OSError, ValueError):
        raise RequestUnconfirmed('network request could not run; outcome unconfirmed') from None
    payload, _, last = result.stdout.rpartition('\n')
    metrics = _curl_metrics(last)
    code = result.returncode
    if code:
        not_sent, phase = classify_transfer(code, tls=url.startswith('https://'), **(metrics or {}))
        detail = (f"curl exit {code}, phase {phase}, http {metrics['http_code']}, "
                  f"appconnect {metrics['time_appconnect']:.3f}s, uploaded {metrics['size_upload']} B"
                  if metrics else f'curl exit {code}, phase {phase}, no transfer metrics')
        if not_sent:
            raise RequestNotSent(f'network request not sent ({detail})', code, phase)
        raise RequestUnconfirmed(f'network request failed; outcome unconfirmed ({detail})', code, phase)
    if metrics is None:
        raise RequestUnconfirmed('network response unconfirmed (curl exit 0, unreadable transfer metrics)', 0, 'after-send')
    phase = 'http-5xx' if metrics['http_code'] >= 500 else 'after-send'
    if len(payload) > 2_000_000:
        raise RequestUnconfirmed('response exceeds limit', 0, phase)
    try:
        return metrics['http_code'], json.loads(payload)
    except ValueError:
        raise RequestUnconfirmed(f"network response unconfirmed (curl exit 0, http {metrics['http_code']})",
                                 0, phase) from None


def http_json(url, body, timeout=20, headers=None, method='POST'):
    # curl receives credentials only on stdin, never in argv, logs, or errors.
    config = ['url = ' + json.dumps(url), 'header = "Content-Type: application/json"']
    for key, value in (headers or {}).items():
        config.append('header = ' + json.dumps(key + ': ' + value))
    if body is not None:
        config.append('data = ' + json.dumps(json.dumps(body)))
    return _curl(['--request', method], config, url, timeout)


def _curl_config_quote(value):
    # curl's config-file quoting only recognizes \\, \", \t, \n, \r, \v as escapes;
    # unlike JSON it has no \u form, so unicode bytes must pass through literally.
    value = (value.replace('\\', '\\\\').replace('"', '\\"').replace('\t', '\\t')
             .replace('\n', '\\n').replace('\r', '\\r').replace('\x0b', '\\v'))
    return '"' + value + '"'


def http_multipart(url, fields, file_field, file_path, timeout=20):
    return http_multipart_files(url, fields, [(file_field, file_path)], timeout)


def http_multipart_files(url, fields, files, timeout=20):
    """One multipart POST of `fields` and the (field, path) `files` in order:
    one file for send<Media>, file0..file9 for an album's sendMediaGroup."""
    # curl receives credentials only via the URL on stdin, never in argv, logs, or errors.
    # No Content-Type header is set: curl generates its own multipart/form-data boundary,
    # which sendPhoto/sendVideo/sendDocument require instead of application/json.
    config = ['url = ' + _curl_config_quote(url)]
    for key, value in fields.items():
        text = str(value)
        config.append('form-string = ' + _curl_config_quote(f'{key}={text}'))
    for file_field, file_path in files:
        config.append('form = ' + _curl_config_quote(f'{file_field}=@{file_path}'))
    return _curl(['--request', 'POST'], config, url, timeout)


def http_download(url, fd, max_bytes, timeout=60):
    # curl receives the URL only on stdin, never in argv, logs, or errors. The
    # response body streams straight into the caller's already-open fd (dup'd
    # onto curl's own stdout) rather than through a path curl would look up
    # itself, so a symlink swapped in at that name after the caller opened it
    # can never be followed; max-filesize aborts a transfer that exceeds the
    # caller's cap even if the server misreports size. The %{stderr} marker
    # moves the write-out status code to stderr so it never lands in the body.
    config = ['url = ' + _curl_config_quote(url), 'max-filesize = ' + str(int(max_bytes))]
    try:
        result = subprocess.run(
            ['curl', '-q', '--silent', '--show-error', '--connect-timeout', '5', '--max-time', str(timeout),
             '--request', 'GET', '--config', '-', '--write-out', '%{stderr}%{http_code}'],
            input='\n'.join(config), stdout=fd, stderr=subprocess.PIPE, text=True, timeout=timeout + 2,
        )
        if result.returncode:
            raise TransportError('network request failed; outcome unconfirmed')
        lines = result.stderr.strip().splitlines()
        return int(lines[-1]) if lines else 0
    except (subprocess.TimeoutExpired, ValueError, OSError):
        raise TransportError('network response unconfirmed') from None


def infer_attachment_kind(path):
    suffix = path.suffix.lower()
    if suffix in ('.png', '.jpg', '.jpeg'):
        return 'photo'
    if suffix in ('.webm', '.mp4'):
        return 'video'
    return 'document'


def utf16_units(text):
    return len(text.encode('utf-16-le')) // 2


def utc_offset_label(moment):
    """'+09' for a whole-hour offset, '+05:30' otherwise, 'UTC' at zero."""
    offset = moment.utcoffset() or datetime.timedelta(0)
    minutes = int(offset.total_seconds()) // 60
    if not minutes:
        return 'UTC'
    sign, minutes = ('+' if minutes > 0 else '-'), abs(minutes)
    return f'{sign}{minutes // 60:02d}' + (f':{minutes % 60:02d}' if minutes % 60 else '')


# Telegram HTML parse_mode, opt-in per message (format='html'). Only these tags
# go out; anything else is refused before Telegram is contacted, so a typo can
# never turn into a 400 that loses the message or into markup the user cannot read.
HTML_TAGS = ('b', 'strong', 'i', 'em', 'u', 's', 'code', 'pre', 'a', 'blockquote', 'tg-spoiler')
HTML_LINK_SCHEMES = ('http', 'https', 'tg')
# The raw markup may be longer than the visible text Telegram counts; this only
# bounds what a caller can hand the validator.
HTML_SOURCE_MAX_UNITS = 16384
_HTML_TOKEN_RE = re.compile(r'<[^<>]*>|&[^&;<>\s]*;?|[<>]')
_HTML_OPEN_RE = re.compile(r'<([a-z][a-z-]*)((?:\s+[a-z]+(?:="[^"<>]*")?)*)\s*>\Z')
_HTML_CLOSE_RE = re.compile(r'</([a-z][a-z-]*)\s*>\Z')
_HTML_ATTR_RE = re.compile(r'\s+([a-z]+)(?:="([^"<>]*)")?')
_HTML_ENTITY_RE = re.compile(r'&(?:lt|gt|amp|quot|#[0-9]{1,7}|#x[0-9a-fA-F]{1,6});\Z')
_HTML_CODE_CLASS_RE = re.compile(r'language-[A-Za-z0-9_+-]{1,32}\Z')


def _html_attributes(tag, raw, parent):
    """None when the tag's attributes are allowed, else the refusal reason."""
    attrs = {}
    for name, value in _HTML_ATTR_RE.findall(raw):
        if name in attrs:
            return 'html_attribute_not_allowed'
        attrs[name] = value
    if tag == 'a':
        if set(attrs) != {'href'} or not attrs['href']:
            return 'html_attribute_not_allowed'
        try:
            scheme = urlsplit(html_unescape(attrs['href'])).scheme.lower()
        except ValueError:
            # Not a URL urlsplit can read (an unclosed IPv6 bracket, a host
            # that NFKC turns into URL syntax): it has no allowed scheme.
            return 'html_link_scheme_not_allowed'
        return None if scheme in HTML_LINK_SCHEMES else 'html_link_scheme_not_allowed'
    if tag == 'code' and attrs:
        if set(attrs) != {'class'} or parent != 'pre' or not _HTML_CODE_CLASS_RE.match(attrs['class']):
            return 'html_attribute_not_allowed'
        return None
    if tag == 'blockquote' and attrs:
        return None if set(attrs) == {'expandable'} and attrs['expandable'] == '' else 'html_attribute_not_allowed'
    return 'html_attribute_not_allowed' if attrs else None


def html_text(value):
    """A value as Telegram HTML text: &, < and > escaped, nothing else touched."""
    return html_escape(str(value), quote=False)


def html_visible_text(text):
    """Validate Telegram HTML; return (visible text, None) or (None, reason).

    The visible text is what Telegram counts against its limits and what the
    plain fallback sends: tags stripped, the four named and numeric entities
    decoded. Text outside tags must have &, < and > escaped by the caller."""
    if utf16_units(text) > HTML_SOURCE_MAX_UNITS:
        return None, 'html_source_too_long'
    stack, visible, position = [], [], 0
    for match in _HTML_TOKEN_RE.finditer(text):
        visible.append(text[position:match.start()])
        position = match.end()
        token = match.group(0)
        if token in ('<', '>'):
            return None, 'html_text_not_escaped'
        if token.startswith('&'):
            if not _HTML_ENTITY_RE.match(token):
                return None, 'html_text_not_escaped'
            if token.startswith('&#'):
                # html.unescape quietly maps these to U+FFFD; refuse them instead.
                point = int(token[3:-1], 16) if token[2] in 'xX' else int(token[2:-1])
                if point == 0 or 0xD800 <= point <= 0xDFFF or point > 0x10FFFF:
                    return None, 'html_text_not_escaped'
            visible.append(html_unescape(token))
            continue
        close = _HTML_CLOSE_RE.match(token)
        if close:
            if not stack or stack[-1] != close.group(1):
                return None, 'html_tags_unbalanced'
            stack.pop()
            continue
        opening = _HTML_OPEN_RE.match(token)
        if not opening:
            return None, 'html_tag_not_allowed'
        tag, parent = opening.group(1), stack[-1] if stack else None
        if tag not in HTML_TAGS:
            return None, 'html_tag_not_allowed'
        if (parent == 'code' or (parent == 'pre' and tag != 'code') or (tag in ('pre', 'blockquote', 'a') and tag in stack)
                or (tag == 'pre' and 'code' in stack)):
            return None, 'html_nesting_not_allowed'
        reason = _html_attributes(tag, opening.group(2), parent)
        if reason:
            return None, reason
        stack.append(tag)
    if stack:
        return None, 'html_tags_unbalanced'
    visible.append(text[position:])
    return ''.join(visible), None


def resendable(receipt):
    """A saved receipt that is not final: every attempt of that send failed
    before any request byte left this host. Only the bridge writes it (no
    receipt saved before it existed matches), and the same key with the same
    request sends again."""
    return (isinstance(receipt, dict) and receipt.get('delivered') is False
            and receipt.get('failure_kind') == 'transient'
            and isinstance(receipt.get('metadata'), dict) and receipt['metadata'].get('reason') == NOT_SENT_REASON)


class Store:
    def __init__(self, path, initial_offset):
        self.path = str(path)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS cursor (id INTEGER PRIMARY KEY, offset INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO cursor VALUES (1, ?)', (initial_offset,))
            db.execute('CREATE TABLE IF NOT EXISTS deliveries (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, receipt TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS asks (ask_id TEXT PRIMARY KEY, question TEXT NOT NULL, '
                       'format TEXT NOT NULL, options TEXT NOT NULL, message_id INTEGER, '
                       'answered INTEGER NOT NULL DEFAULT 0, answer TEXT, '
                       'update_id INTEGER, relayed INTEGER NOT NULL DEFAULT 0, '
                       "kind TEXT NOT NULL DEFAULT 'text', decided INTEGER NOT NULL DEFAULT 0)")
            # CREATE TABLE IF NOT EXISTS never adds a column to an existing
            # ledger: add decided once, so every recorded ask reads
            # decided=0 and keeps its meaning. The INSERT above holds the
            # write lock, so a second process opening the ledger waits here.
            if 'decided' not in [row[1] for row in db.execute('PRAGMA table_info(asks)')]:
                db.execute('ALTER TABLE asks ADD COLUMN decided INTEGER NOT NULL DEFAULT 0')
            # The paired user's newest message GC durably accepted: one row,
            # the target of `react` without --message-id.
            db.execute('CREATE TABLE IF NOT EXISTS accepted (id INTEGER PRIMARY KEY CHECK (id = 1), '
                       'message_id INTEGER NOT NULL, accepted_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS pins (slot TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 1, '
                       'message_id INTEGER, revision INTEGER NOT NULL DEFAULT 0, fingerprint TEXT)')
            # Topics of the paired chat (OPERATIONS.md, "Topics"): each
            # settings name's thread id, created by `topics` or recorded by
            # hand, and the attempt its create key topic:<attempt>:<name> uses.
            db.execute('CREATE TABLE IF NOT EXISTS topics (name TEXT PRIMARY KEY, thread_id INTEGER NOT NULL UNIQUE, '
                       'source TEXT NOT NULL, recorded_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS topic_attempts (name TEXT PRIMARY KEY, attempt INTEGER NOT NULL)')

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def offset(self):
        with self.connect() as db:
            return db.execute('SELECT offset FROM cursor WHERE id=1').fetchone()[0]

    def advance(self, offset):
        with self.connect() as db:
            db.execute('UPDATE cursor SET offset=max(offset, ?) WHERE id=1', (offset,))

    def reserve(self, key, fingerprint, pending, retry_undelivered=False):
        """None when the caller now holds the key and may send (a new key, or
        one whose saved receipt is the not-final not-sent one); else the saved
        receipt. One transaction, so two callers never both get None.
        retry_undelivered also retakes a saved receipt that is not delivered
        (rejected or ambiguous): only for a request that is idempotent in
        effect (a pin or an unpin), made under its slot lock."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT fingerprint, receipt FROM deliveries WHERE key=?', (key,)).fetchone()
            if existing:
                if existing[0] != fingerprint:
                    raise TransportError('idempotency key reused for a different request')
                saved = json.loads(existing[1])
                if not resendable(saved) and not (retry_undelivered and saved.get('delivered') is not True):
                    return saved
                db.execute('UPDATE deliveries SET receipt=? WHERE key=?', (json.dumps(pending), key))
                return None
            db.execute('INSERT INTO deliveries VALUES (?, ?, ?)', (key, fingerprint, json.dumps(pending)))
        return None

    def saved_receipt(self, key):
        """The receipt saved under key, or None when the key was never reserved."""
        with self.connect() as db:
            row = db.execute('SELECT receipt FROM deliveries WHERE key=?', (key,)).fetchone()
        return None if row is None else json.loads(row[0])

    def finish(self, key, receipt):
        with self.connect() as db:
            db.execute('UPDATE deliveries SET receipt=? WHERE key=?', (json.dumps(receipt), key))

    def record_ask(self, ask_id, question, fmt, options, kind='text'):
        """Remember one mayor ask button group: the first record wins, so a
        repeated send of the same ask never resets its answered flag. Kind is
        'text' or 'photo': the question is the message text or the photo caption."""
        if kind not in ('text', 'photo'):
            raise ValueError('ask kind must be text or photo')
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO asks(ask_id, question, format, options, kind)'
                       ' VALUES (?,?,?,?,?)',
                       (ask_id, question, fmt, json.dumps(options), kind))

    def get_ask(self, ask_id):
        with self.connect() as db:
            row = db.execute('SELECT question, format, options, message_id, answered, answer, update_id, relayed, kind,'
                             ' decided FROM asks WHERE ask_id=?', (ask_id,)).fetchone()
        if row is None:
            return None
        question, fmt, options, message_id, answered, answer, update_id, relayed, kind, decided = row
        return dict(ask_id=ask_id, question=question, format=fmt, options=json.loads(options),
                    message_id=message_id, answered=bool(answered), answer=answer,
                    update_id=update_id, relayed=bool(relayed), kind=kind or 'text', decided=bool(decided))

    def get_ask_by_message(self, message_id):
        """The ask whose send confirmed this message id, or None."""
        with self.connect() as db:
            row = db.execute('SELECT ask_id FROM asks WHERE message_id=?', (message_id,)).fetchone()
        return None if row is None else self.get_ask(row[0])

    def set_ask_message(self, ask_ids, message_id):
        with self.connect() as db:
            db.executemany('UPDATE asks SET message_id=? WHERE ask_id=? AND message_id IS NULL',
                           [(message_id, ask_id) for ask_id in ask_ids])

    def mark_ask_answered(self, ask_id, answer, update_id):
        """In one ledger write mark the ask answered with the choice and update
        id; True when this write won, False when the ask was already answered
        (a close from the CLI process won after the tap read the ask)."""
        with self.connect() as db:
            return db.execute('UPDATE asks SET answered=1, answer=?, update_id=? WHERE ask_id=? AND answered=0',
                              (answer, update_id, ask_id)).rowcount == 1

    def close_ask(self, ask_id, decision):
        """In one ledger write mark the ask answered and decided with the
        decision, and relayed (nothing goes to GC: the mayor made it); True
        when this write won, False when a tap or another close came first.
        The guard is the only serialisation of a tap (the daemon) against a
        close (the CLI): they are separate processes."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return db.execute('UPDATE asks SET answered=1, answer=?, decided=1, relayed=1'
                              ' WHERE ask_id=? AND answered=0', (decision, ask_id)).rowcount == 1

    def mark_ask_relayed(self, ask_id):
        """Record the ask as relayed once GC confirms the answer entry."""
        with self.connect() as db:
            db.execute('UPDATE asks SET relayed=1 WHERE ask_id=?', (ask_id,))

    def record_accepted(self, message_id, accepted_at):
        """Remember the last accepted message; a replayed older update never
        moves it back."""
        with self.connect() as db:
            db.execute('INSERT INTO accepted VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET '
                       'message_id=excluded.message_id, accepted_at=excluded.accepted_at '
                       'WHERE excluded.message_id >= accepted.message_id', (message_id, accepted_at))

    def last_accepted(self):
        """The last accepted message's id, or None when none is recorded."""
        with self.connect() as db:
            row = db.execute('SELECT message_id FROM accepted WHERE id=1').fetchone()
        return row[0] if row else None

    def agent_reacted(self, message_id):
        """True once a `react` call reserved a reaction for the message: the
        accept reaction then never runs over it."""
        prefix = f'reaction:{int(message_id)}:'
        with self.connect() as db:
            row = db.execute('SELECT 1 FROM deliveries WHERE substr(key, 1, ?) = ? AND key != ? LIMIT 1',
                             (len(prefix), prefix, prefix + 'accept')).fetchone()
        return row is not None

    PIN_FIELDS =('generation', 'message_id', 'revision', 'fingerprint')

    def pin_state(self, slot):
        """One pinned-message slot: its generation (from 1), live message id
        (None when there is none), revision and the fingerprint of the last
        applied body and format (None when unknown)."""
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO pins(slot) VALUES (?)', (slot,))
            row = db.execute('SELECT ' + ', '.join(self.PIN_FIELDS) + ' FROM pins WHERE slot=?', (slot,)).fetchone()
        return dict(zip(self.PIN_FIELDS, row), slot=slot)

    def set_pin(self, slot, **fields):
        if not fields or set(fields) - set(self.PIN_FIELDS):
            raise ValueError('unknown pin field')
        with self.connect() as db:
            db.execute('UPDATE pins SET ' + ', '.join(f'{name}=?' for name in fields) + ' WHERE slot=?',
                       (*fields.values(), slot))

    TOPIC_SOURCES = ('created', 'recorded')

    def record_topic(self, name, thread_id, source):
        """Record a topic's thread id in one transaction. Returns None, or the
        refusal: topic_already_recorded when the name has a row,
        thread_id_already_recorded when another row holds the thread id."""
        if source not in self.TOPIC_SOURCES:
            raise ValueError('topic source must be created or recorded')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM topics WHERE name=?', (name,)).fetchone():
                return 'topic_already_recorded'
            if db.execute('SELECT 1 FROM topics WHERE thread_id=?', (thread_id,)).fetchone():
                return 'thread_id_already_recorded'
            db.execute('INSERT INTO topics VALUES (?, ?, ?, ?)', (name, thread_id, source,
                       datetime.datetime.now(datetime.timezone.utc).isoformat()))
        return None

    def topic_rows(self):
        """Every recorded topic, in the order recorded: dicts of name,
        thread_id, source and recorded_at."""
        with self.connect() as db:
            rows = db.execute('SELECT name, thread_id, source, recorded_at FROM topics ORDER BY rowid').fetchall()
        return [dict(zip(('name', 'thread_id', 'source', 'recorded_at'), row)) for row in rows]

    def topic_attempt(self, name):
        """The attempt a topic's create key uses: 1 until --retry raises it."""
        with self.connect() as db:
            row = db.execute('SELECT attempt FROM topic_attempts WHERE name=?', (name,)).fetchone()
        return row[0] if row else 1

    def raise_topic_attempt(self, name):
        """Raise a topic's attempt by one in one write; returns the new one."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO topic_attempts VALUES (?, 2) ON CONFLICT(name) DO UPDATE SET attempt=attempt+1',
                       (name,))
            return db.execute('SELECT attempt FROM topic_attempts WHERE name=?', (name,)).fetchone()[0]

    def deliveries_mark(self):
        """The highest rowid of the deliveries table, 0 when it is empty: rows
        are never deleted, so every new key gets a higher one."""
        with self.connect() as db:
            return db.execute('SELECT coalesce(max(rowid), 0) FROM deliveries').fetchone()[0]

    def sent_since(self, mark):
        """True once the deliveries table holds a key newer than `mark` other
        than an accept reaction: a send, edit, ask, pin, album or reaction from
        the daemon or a CLI process, which share this ledger."""
        with self.connect() as db:
            row = db.execute("SELECT 1 FROM deliveries WHERE rowid > ? AND key NOT GLOB 'reaction:[0-9]*:accept'"
                             ' LIMIT 1', (mark,)).fetchone()
        return row is not None


def check_bridge_config(config, *, settings=False):
    """The one validation of a bridge config's identity: the bot, user and
    chat ids are positive integers in a private pairing, the city and agent
    names match NAME_RE, an account is present, the timezone is known and
    the GC URL is plain HTTP on 127.0.0.1 without a path or user. Returns
    (city, account, agent, local_tz, base). With settings=True every refusal
    is a SettingsError naming its key; otherwise a TransportError."""
    def fail(bridge_message, settings_message):
        raise SettingsError(settings_message) if settings else TransportError(bridge_message)

    for key in ('bot_id', 'user_id', 'chat_id'):
        value = config.get(key)
        if type(value) is not int or not 1 <= value <= 2 ** 63 - 1:
            fail('positive numeric bot/user/private-chat IDs required',
                 f'settings: bot.{key} must be an integer from 1 to 9223372036854775807')
    if config['chat_id'] != config['user_id']:
        fail('private chat ID must match authorized user ID',
             'settings: bot.chat_id must equal bot.user_id (a private chat with the paired user)')
    city = config.get('city')
    if not isinstance(city, str) or not NAME_RE.fullmatch(city):
        fail('city name required', 'settings: city is missing or malformed')
    account = config.get('account_id', config.get('bot_username'))
    if not account:
        fail('account_id required', 'settings: bot.account_id is missing or malformed')
    agent = config.get('agent', 'mayor')
    if not isinstance(agent, str) or not NAME_RE.fullmatch(agent):
        fail('agent name required', 'settings: agent is missing or malformed')
    try:
        local_tz = zoneinfo.ZoneInfo(config.get('timezone') or 'UTC')
    except (ValueError, TypeError, zoneinfo.ZoneInfoNotFoundError):
        fail('timezone is not a known time zone', 'settings: timezone is not a known time zone')
    base_value = config.get('gc_base_url', 'http://127.0.0.1:8372')
    base = base_value.rstrip('/') if isinstance(base_value, str) else ''
    try:
        parsed = urlsplit(base)
        bad_gc_url = (not base or parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                      or parsed.path or parsed.username)
    except ValueError:
        bad_gc_url = True
    if bad_gc_url:
        fail('GC URL must be plain HTTP on 127.0.0.1 without a path',
             'settings: gc_base_url must be plain HTTP on 127.0.0.1 without a path or user')
    return city, account, agent, local_tz, base


def _callback_handler(bridge):
    class Handler(http.server.BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, *_):
            pass

        def do_POST(self):
            if (not hmac.compare_digest(self.path, '/' + bridge.secret + '/publish')
                or self.headers.get('X-GC-Request') not in ('1', 'true')):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get('Content-Length', '-1'))
                if not 0 < length <= 65536:
                    raise ValueError()
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise ValueError()
                if 'attachment' in request or 'attachments' in request:
                    result = bridge.receipt(reason='attachment_not_accepted_over_callback')
                else:
                    result = bridge.publish(request)
                payload = json.dumps(result).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (ValueError, UnicodeError):
                self.send_error(400)
            except Exception:
                # Never expose raw exceptions (may contain payload or secrets).
                self.send_error(500, 'Delivery unconfirmed')
    return Handler


class Bridge:
    ATTACHMENT_KINDS = {
        'photo': ('sendPhoto', 'photo', 10 * 1024 * 1024),
        'video': ('sendVideo', 'video', 50 * 1024 * 1024),
        'document': ('sendDocument', 'document', 50 * 1024 * 1024),
        'voice': ('sendVoice', 'voice', 50 * 1024 * 1024),
        'video_note': ('sendVideoNote', 'video_note', 50 * 1024 * 1024),
    }
    # sendVideoNote has no caption: the body must be empty for these kinds.
    CAPTIONLESS_KINDS = frozenset({'video_note'})
    # sendMediaGroup: 2-10 items; photos and videos mix, documents group only
    # with documents, and voice and video notes never group.
    ALBUM_FILES = (2, 10)
    ALBUM_KINDS = frozenset({'photo', 'video', 'document'})
    # A health report older than HEALTH_MAX_AGE no longer proves the agent is quota-blocked, so the exhaustion notice stays quiet.
    HEALTH_MAX_AGE = datetime.timedelta(minutes=10)
    HEALTH_CLOCK_SKEW = datetime.timedelta(minutes=1)

    # Inbound media relay: kinds Telegram can attach to a private message that
    # this bridge downloads and relays, in the order checked (a message never
    # carries more than one of these). Voice/audio rarely carry file_name, so
    # each kind gets its own generic fallback rather than one shared default.
    INBOUND_KINDS = ('document', 'video', 'voice', 'audio')
    INBOUND_DEFAULT_NAMES = {'photo': 'photo.jpg', 'video': 'video.mp4', 'voice': 'voice.ogg',
                              'audio': 'audio.mp3', 'document': 'document.bin'}
    INBOUND_MAX_BYTES = 50 * 1024 * 1024
    TRANSCRIBE_KINDS = ('voice', 'audio')
    TRANSCRIPT_MAX_CHARS = 4000
    TRANSCRIPT_MAX_BYTES = 64 * 1024
    # The adapter's own failure codes ({"error": <code>}), each relayed as one
    # fixed phrase; any other code is 'invalid output'.
    TRANSCRIBER_ERRORS = {
        'timeout': 'timeout',
        'queue_failed': 'transcriber queue unavailable',
        'decode_failed': 'audio could not be decoded',
        'whisper_missing': 'transcriber missing (whisper-cli not found)',
        'model_missing': 'transcriber missing (model not found)',
        'whisper_failed': 'whisper-cli failed',
        'fence_failed': 'transcriber could not be confined to its configured CPUs',
    }
    PENDING_SUFFIX = '-transcript.pending'
    PENDING_RELAY_ATTEMPTS = 3
    PENDING_MAX_BYTES = 64 * 1024
    PENDING_BACKOFF_SECONDS = (5, 30)  # waits after the 1st and 2nd failed relay
    _PENDING_NAME_RE = re.compile(r'^([0-9]{1,18})-transcript\.pending$')
    _AUDIO_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$')
    _CONTROL_RE = re.compile(r'[\x00-\x1f\x7f-\x9f\u2028\u2029]+')
    INBOUND_DOWNLOAD_ATTEMPTS = 3
    _FILENAME_UNSAFE_RE = re.compile(r'[^A-Za-z0-9._-]+')
    # Any of these marks a message the user forwarded: Bot API 7.0's
    # forward_origin, or the fields it replaced.
    FORWARD_KEYS = ('forward_origin', 'forward_from', 'forward_from_chat', 'forward_from_message_id',
                    'forward_signature', 'forward_sender_name', 'forward_date')
    FORWARD_FIELD_MAX = 80
    _FORWARD_DELIMITERS = str.maketrans({'[': '(', ']': ')', '|': '/'})

    def __init__(self, config, store, token, callback_secret, port=8930, request=http_json,
                 request_multipart=http_multipart, request_download=None, state_dir=None, transcribe=None,
                 sleep=None, monotonic=None, request_media_group=http_multipart_files, typing_wait=None):
        self.config, self.store, self.token = config, store, token
        # The send retry's wait and clock (tests inject both, so none sleeps).
        self.sleep, self.monotonic = sleep or time.sleep, monotonic or time.monotonic
        self.transcribe = transcribe
        self.transcribe_queue = queue.Queue()
        self.transcribe_worker = None
        self.transcribe_worker_lock = threading.Lock()
        # The settings loader validated [reactions]; without it, the defaults.
        self.reactions = dict(REACTIONS_DEFAULTS, **(config.get('reactions') or {}))
        self.reaction_queue = queue.Queue()
        self.reaction_worker = None
        self.reaction_worker_lock = threading.Lock()
        # The typing window ([typing]): None, or dict(end, mark, due, failed)
        # under typing_lock; the worker waits on typing_wake between ticks
        # (tests inject typing_wait, so none sleeps).
        self.typing_seconds = dict(TYPING_DEFAULTS, **(config.get('typing') or {}))['seconds']
        self.typing_window = None
        self.typing_lock = threading.Lock()
        self.typing_wake = threading.Event()
        self.typing_wait = typing_wait or self.typing_wake.wait
        self.typing_worker = None
        self.secret, self.port, self.request = callback_secret, port, request
        self.request_multipart = request_multipart
        self.request_media_group = request_media_group
        self.request_download = request_download or http_download
        # The bridge's own directory: ledger, offset, inbound files, heartbeat.
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.protected_paths = tuple(config.get('protected_paths', ()) or ())
        self.city, self.account, self.agent, self.local_tz, self.base = check_bridge_config(config)
        self.display_name = config.get('user_display_name') or 'User'
        self.unit = config.get('unit')
        self.menu = tuple(config.get('menu') or ())
        # The city's own commands ([[command]]), by name in file order; the
        # settings loader has validated every entry.
        self.commands = {command['name']: command for command in config.get('commands') or ()}
        # The city's topics of the paired chat ([[topic]]), each {name, agent}
        # in file order; their thread ids live in the ledger.
        self.topic_settings = tuple(config.get('topics') or ())
        # The gc agent this bridge serves (check_bridge_config validated it):
        # inbound must be routed to it, and the quota-exhaustion notice
        # watches its session.
        self.conversation = dict(scope_id=self.city, provider='telegram', account_id=self.account,
                                 conversation_id=str(config['chat_id']), kind='dm')
        self.publish_lock = threading.Lock()
        self.stop = threading.Event()
        self.last_heartbeat = None

    def telegram(self, method, body, timeout=20):
        status, data = self.request('https://api.telegram.org/bot' + self.token + '/' + method, body, timeout)
        if not isinstance(data, dict):
            raise TransportError('Telegram response unconfirmed')
        return status, data

    def telegram_multipart(self, method, fields, file_field, file_path, timeout=20):
        status, data = self.request_multipart('https://api.telegram.org/bot' + self.token + '/' + method,
                                               fields, file_field, file_path, timeout)
        if not isinstance(data, dict):
            raise TransportError('Telegram response unconfirmed')
        return status, data

    def telegram_media_group(self, fields, files, timeout=20):
        status, data = self.request_media_group('https://api.telegram.org/bot' + self.token + '/sendMediaGroup',
                                                fields, files, timeout)
        if not isinstance(data, dict):
            raise TransportError('Telegram response unconfirmed')
        return status, data

    def _send(self, method, attempt_once):
        """One outbound Telegram send under its reservation and publish_lock.

        `attempt_once(timeout)` makes one request. Returns its (status, data)
        when Telegram answered below HTTP 500 (a 4xx is the caller's definite
        rejection). An attempt that failed before any request byte left this
        host (RequestNotSent) is tried again after SEND_RETRY_DELAYS, at most
        SEND_ATTEMPTS in all within SEND_BUDGET; when none got out, the last
        RequestNotSent propagates. Any other failure, an HTTP 5xx included,
        may have reached Telegram: it propagates at once and is never retried.
        Every failed attempt logs one line: method, attempt, curl exit, phase
        and milliseconds, never text, URL, token or chat."""
        started = self.monotonic()
        for attempt in range(1, SEND_ATTEMPTS + 1):
            begun = self.monotonic()
            timeout = min(SEND_ATTEMPT_TIMEOUT, SEND_BUDGET - (begun - started))
            try:
                status, data = attempt_once(timeout)
                verdict = classify_transfer(0, http_code=status)
                if verdict:
                    raise RequestUnconfirmed(f'Telegram answered HTTP {status}; outcome unconfirmed', 0, verdict[1])
                return status, data
            except RequestNotSent as error:
                self._log_attempt(method, attempt, 'not sent', error, begun)
                if attempt == SEND_ATTEMPTS:
                    raise
                delay = SEND_RETRY_DELAYS[attempt - 1]
                if SEND_BUDGET - (self.monotonic() - started) - delay < SEND_MIN_ATTEMPT:
                    raise
                self.sleep(delay)
            except TransportError as error:
                self._log_attempt(method, attempt, 'unconfirmed', error, begun)
                raise

    def _log_attempt(self, method, attempt, outcome, error, begun):
        code = getattr(error, 'exit_code', None)
        phase = getattr(error, 'phase', 'unknown')
        ms = max(0, round((self.monotonic() - begun) * 1000))
        self._log(f'telegram {method} attempt {attempt}/{SEND_ATTEMPTS} {outcome}: '
                  f"curl exit {'-' if code is None else code}, phase {phase}, {ms} ms")

    def gc(self, path, body, method='POST', timeout=20):
        headers = {'X-GC-Request': '1'}
        token_path = self.config.get('gc_api_token_path')
        if token_path:
            headers['Authorization'] = 'Bearer ' + private_text(token_path)
        status, result = self.request(self.base + '/v0/city/' + quote(self.city, safe='') + '/extmsg/' + path,
                                      body, timeout, headers=headers, method=method)
        if not 200 <= status < 300 or not isinstance(result, dict):
            raise TransportError('GC request rejected or unconfirmed')
        return result

    def probe(self):
        status, data = self.telegram('getMe', {})
        bot = data.get('result', {})
        if status == 200 and data.get('ok') is True and (bot.get('id') != self.config['bot_id'] or bot.get('is_bot') is not True):
            raise ProbeRejected('Telegram bot identity mismatch or probe failed')
        if status != 200 or data.get('ok') is not True:
            raise TransportError('Telegram bot identity mismatch or probe failed')
        status, data = self.telegram('getWebhookInfo', {})
        if status != 200 or data.get('ok') is not True or not isinstance(data.get('result'), dict):
            raise TransportError('Telegram webhook probe failed')
        if data['result'].get('url'):
            raise ProbeRejected('existing Telegram webhook must be resolved before polling')

    def set_commands(self):
        """Set the settings' menu as the paired chat's command menu and confirm it by reading it back:
        the [[menu]] entries, then the [[command]] entries shown in the menu, in file order.

        Uses no Store, lock, webhook or getUpdates call, so it is safe while the
        unit polls; running it again sets the same list again.
        """
        entries = list(self.menu) + [(c['name'], c['description']) for c in self.commands.values() if c['menu']]
        commands = [dict(command=name, description=text) for name, text in entries]
        scope = dict(type='chat', chat_id=self.config['chat_id'])
        status, data = self.telegram('setMyCommands', dict(commands=commands, scope=scope))
        if status != 200 or data.get('ok') is not True or data.get('result') is not True:
            raise TransportError(f'Telegram did not accept the command menu (HTTP {status})')
        status, data = self.telegram('getMyCommands', dict(scope=scope))
        if status != 200 or data.get('ok') is not True:
            raise TransportError(f'Telegram command menu read-back failed (HTTP {status})')
        if data.get('result') != commands:
            raise TransportError('Telegram command menu read-back differs from the list that was set')
        return commands

    def probe_with_retry(self, budget=PROBE_RETRY_BUDGET, wait=None, monotonic=time.monotonic, log=None):
        """Probe until it passes, riding out a short network outage.

        Returns False if the bridge was told to stop while waiting. A definite
        rejection (identity mismatch, existing webhook) propagates at once; a
        transport failure is retried with a capped backoff until the budget is
        spent, then the last error propagates so systemd restarts the process.
        """
        wait = wait or self.stop.wait
        log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        started, delay, attempt = monotonic(), PROBE_RETRY_FIRST_DELAY, 0
        while True:
            attempt += 1
            try:
                self.probe()
                if attempt > 1:
                    log(f'Telegram probe succeeded on attempt {attempt}')
                return True
            except ProbeRejected:
                raise
            except TransportError as error:
                if monotonic() - started + delay > budget:
                    raise
                log(f'Telegram probe attempt {attempt} failed ({error}); retrying in {delay:g}s')
                if wait(delay):
                    return False
                delay = min(delay * 2, PROBE_RETRY_MAX_DELAY)

    def register(self):
        # SupportsAttachments stays False until GC's handling of it is
        # confirmed. Flip only after confirming GC tolerates it correctly.
        return self.gc('adapters', dict(provider='telegram', account_id=self.account, name='city-private-telegram',
            callback_url=f'http://127.0.0.1:{self.port}/{self.secret}',
            capabilities=dict(SupportsChildConversations=False, SupportsAttachments=False, MaxMessageLength=4096)))

    def _largest_photo_size(self, sizes):
        candidates = [s for s in sizes if isinstance(s, dict) and isinstance(s.get('file_id'), str) and s['file_id']]
        if not candidates:
            return None
        def area(s):
            w, h = s.get('width'), s.get('height')
            return (w if isinstance(w, int) else 0) * (h if isinstance(h, int) else 0)
        return max(candidates, key=lambda s: (area(s), s.get('file_size') if isinstance(s.get('file_size'), int) else 0))

    def _extract_attachment(self, message):
        """Return (kind, file_id, known_size_or_None, original_name_or_None) for the
        first supported media on this message, or None. A message carries at
        most one of these kinds; photo is checked first since it is the most
        common inbound attachment and has no top-level file_size of its own."""
        photo = message.get('photo')
        if isinstance(photo, list) and photo:
            size = self._largest_photo_size(photo)
            if size is not None:
                known_size = size.get('file_size') if isinstance(size.get('file_size'), int) else None
                return 'photo', size['file_id'], known_size, None
        for kind in self.INBOUND_KINDS:
            media = message.get(kind)
            if isinstance(media, dict) and isinstance(media.get('file_id'), str) and media['file_id']:
                known_size = media.get('file_size') if isinstance(media.get('file_size'), int) else None
                name = media.get('file_name') if isinstance(media.get('file_name'), str) else None
                return kind, media['file_id'], known_size, name
        return None

    @classmethod
    def _sanitize_filename(cls, name, fallback, limit=120):
        # Keep only the final path component (drops any '../' traversal or
        # embedded separator), replace anything outside a conservative
        # ASCII-safe set, then strip a leading run of dots/underscores --
        # done last, since collapsing an unsafe run to '_' can otherwise
        # expose a '.' that was not leading in the original name (no dotfiles).
        candidate = ''
        if isinstance(name, str):
            candidate = name.strip().replace('\\', '/').rsplit('/', 1)[-1]
            candidate = cls._FILENAME_UNSAFE_RE.sub('_', candidate).lstrip('._')
        return (candidate or fallback)[:limit]

    @staticmethod
    def _verify_contained(path, root_resolved, what):
        """Refuse a symlinked path outright, then confirm its realpath still
        falls under root_resolved. Called both when a directory is first
        created and again right before it is written into, so a symlink
        swapped in during that window (or a pre-existing one) can never send
        inbound attachment writes outside the inbound tree."""
        if path.is_symlink():
            raise TransportError(f'refusing symlinked {what}: {path}')
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            raise TransportError(f'{what} escapes inbound root: {path}')
        return resolved

    def _require_contained_dir(self, path, root_resolved, what):
        self._verify_contained(path, root_resolved, what)
        if path.exists() and not path.is_dir():
            raise TransportError(f'{what} is not a directory: {path}')
        path.mkdir(parents=True, exist_ok=True)
        resolved = self._verify_contained(path, root_resolved, what)
        os.chmod(path, 0o700)
        return path, resolved

    def _inbound_root(self):
        if self.state_dir is None:
            raise TransportError('state directory required for inbound attachment storage')
        root_resolved = self.state_dir.resolve(strict=False)
        base = self.state_dir / 'telegram-inbound'
        return self._require_contained_dir(base, root_resolved, 'inbound root')

    def _inbound_dir(self, now=None):
        base, base_resolved = self._inbound_root()
        now = now or datetime.datetime.now(datetime.timezone.utc)
        day_dir = base / now.strftime('%Y%m%d')
        day_dir, _ = self._require_contained_dir(day_dir, base_resolved, 'inbound date directory')
        return day_dir

    def _inbound_dest_path(self, kind, message_id, original_name):
        fallback = self.INBOUND_DEFAULT_NAMES.get(kind, 'file.bin')
        name = self._sanitize_filename(original_name, fallback)
        return self._inbound_dir() / f'{message_id}-{name}'

    def _telegram_get_file(self, file_id):
        status, data = self.telegram('getFile', {'file_id': file_id})
        result = data.get('result')
        if status != 200 or data.get('ok') is not True or not isinstance(result, dict):
            raise TransportError('Telegram getFile failed')
        file_path = result.get('file_path')
        if not isinstance(file_path, str) or not file_path:
            raise TransportError('Telegram getFile returned no path')
        remote_size = result.get('file_size')
        return file_path, remote_size if isinstance(remote_size, int) else None

    @staticmethod
    def _open_pinned_dir(path, label, open_error=None):
        # open_error is the message for an OSError while opening or pinning
        # the directory; None re-raises that OSError unchanged, for callers
        # whose original code let it propagate. Either way a descriptor
        # opened before the failure is closed, never leaked.
        dfd = None
        try:
            dfd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            pinned, named = os.fstat(dfd), os.lstat(path)
        except OSError as error:
            if dfd is not None:
                os.close(dfd)
            if open_error is None:
                raise
            raise TransportError(open_error) from error
        if (pinned.st_dev, pinned.st_ino) != (named.st_dev, named.st_ino):
            os.close(dfd)
            raise TransportError(f'{label} changed while opening')
        return dfd

    @staticmethod
    def _atomic_write_pinned(dfd, dest_name, prefix, write_fn, fsync_dir=False, create_error=None):
        tmp_name = f'.{prefix}-{secrets.token_hex(16)}.tmp'
        fd = None
        try:
            # An unpredictable name opened with O_EXCL|O_NOFOLLOW refuses a
            # pre-placed symlink or file.
            try:
                fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            except OSError as error:
                if create_error is None:
                    raise
                raise TransportError(create_error) from error
            result = write_fn(fd)
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(tmp_name, dest_name, src_dir_fd=dfd, dst_dir_fd=dfd)
            if fsync_dir:
                os.fsync(dfd)
            return result
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp_name, dir_fd=dfd)
            except OSError:
                pass  # renamed into place, or never created

    def _download_to(self, remote_file_path, dest, root_resolved):
        """Download into dest and return the byte size. After the containment
        check the date directory is opened once and pinned by descriptor; the
        temp create, the rename into place and the cleanup are all relative to
        that descriptor, so swapping the directory's path for a symlink at any
        later moment cannot redirect a write outside the inbound tree."""
        date_dir = dest.parent
        self._verify_contained(date_dir, root_resolved, 'inbound date directory')
        url = 'https://api.telegram.org/file/bot' + self.token + '/' + remote_file_path
        dfd = self._open_pinned_dir(date_dir, 'inbound date directory',
                                    open_error='inbound temp file could not be created safely')
        try:
            def write(fd):
                # Every response byte flows through this one fd
                # (request_download dup's it onto curl's stdout).
                status = self.request_download(url, fd, self.INBOUND_MAX_BYTES)
                # A 200 with an empty body is a valid empty attachment.
                if status != 200:
                    raise TransportError('Telegram file download rejected or unconfirmed')
                size = os.fstat(fd).st_size
                os.fchmod(fd, 0o600)
                return size
            return self._atomic_write_pinned(dfd, dest.name, 'inbound', write,
                                             create_error='inbound temp file could not be created safely')
        except OSError as error:
            raise TransportError('inbound attachment write failed') from error
        finally:
            os.close(dfd)

    def _fetch_inbound_attachment(self, kind, file_id, known_size, message_id, original_name):
        """Download one inbound attachment, bounded to a few attempts so a
        persistent transport failure degrades to a relay note instead of
        blocking inbound delivery forever. Returns (path, size, failure) where
        failure is None on success, 'oversize' (skipped, never downloaded) or
        'fetch_failed' (download never succeeded)."""
        if isinstance(known_size, int) and known_size > self.INBOUND_MAX_BYTES:
            return None, known_size, 'oversize'
        _, root_resolved = self._inbound_root()
        dest = self._inbound_dest_path(kind, message_id, original_name)
        for _ in range(self.INBOUND_DOWNLOAD_ATTEMPTS):
            try:
                file_path, remote_size = self._telegram_get_file(file_id)
                size = remote_size if remote_size is not None else known_size
                if isinstance(size, int) and size > self.INBOUND_MAX_BYTES:
                    return None, size, 'oversize'
                return dest, self._download_to(file_path, dest, root_resolved), None
            except TransportError:
                continue
        return None, None, 'fetch_failed'

    # ---- forwarded messages --------------------------------------------------
    # A message the paired user forwards is material they pass on, never their
    # own instruction: it is relayed under a one-line header built only from
    # Telegram's forward fields, with its text quoted line by line after it.

    @classmethod
    def _forward_source(cls, message):
        """{'origin', 'sent'} of a forwarded message, cleaned for the header;
        "unknown" for a missing or malformed field, never an error."""
        def text(value):
            return value.strip() if isinstance(value, str) else ''

        def handle(name, username):
            username = text(username)
            if name and username:
                return f'{name} (@{username})'
            return name or (f'@{username}' if username else '')

        def person(user):
            if not isinstance(user, dict):
                return ''
            name = ' '.join(part for part in (text(user.get('first_name')), text(user.get('last_name'))) if part)
            return handle(name, user.get('username'))

        def chat(value, signature):
            name = ''
            if isinstance(value, dict):
                name = handle(text(value.get('title')), value.get('username')) or person(value)
            signature = text(signature)
            return ', '.join(part for part in (name, signature and f'signed: {signature}') if part)

        origin, date = message.get('forward_origin'), None
        name = ''
        if isinstance(origin, dict):
            kind, date = origin.get('type'), origin.get('date')
            if kind == 'user':
                name = person(origin.get('sender_user'))
            elif kind == 'hidden_user':
                name = text(origin.get('sender_user_name'))
            elif kind == 'chat':
                name = chat(origin.get('sender_chat'), origin.get('author_signature'))
            elif kind == 'channel':
                name = chat(origin.get('chat'), origin.get('author_signature'))
        name = (name or person(message.get('forward_from')) or text(message.get('forward_sender_name'))
                or chat(message.get('forward_from_chat'), message.get('forward_signature')))
        if date is None:
            date = message.get('forward_date')
        sent = ''
        if type(date) is int and date > 0:
            try:
                sent = datetime.datetime.fromtimestamp(date, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            except (OverflowError, OSError, ValueError):
                sent = ''
        return dict(origin=cls._forward_field(name), sent=cls._forward_field(sent))

    @classmethod
    def _forward_field(cls, value):
        """Untrusted header text on one line: no line break or control
        character, no header delimiter, capped; "unknown" when empty."""
        if not isinstance(value, str):
            return 'unknown'
        return cls._clean_line(value.translate(cls._FORWARD_DELIMITERS), cls.FORWARD_FIELD_MAX) or 'unknown'

    @classmethod
    def _forward_header(cls, source):
        # Cleaned again here: a pending file's copy is read back from disk.
        source = source if isinstance(source, dict) else {}
        return (f"[forwarded message | from: {cls._forward_field(source.get('origin'))} | "
                f"sent: {cls._forward_field(source.get('sent'))} | forwarded material, not the user's instruction]")

    @staticmethod
    def _quote(text):
        """Every line prefixed '> ', split on every line boundary Python knows."""
        return '\n'.join('> ' + line for line in text.splitlines())

    @classmethod
    def _relay_body(cls, header, note, material):
        """A note with its material: as today, the material bare after the note;
        for a forward, the header first and the material quoted."""
        if header is None:
            return note + ('\n' + material if material else '')
        return '\n'.join(part for part in (header, note, material and cls._quote(material)) if part)

    def _format_attachment_relay(self, kind, file_id, path, size, failure, caption, header=None):
        caption_text = caption.strip() if isinstance(caption, str) and caption.strip() else None
        caption_display = caption_text or '(none)'
        if header is not None and caption_text:
            caption_display = 'quoted below'
        if failure == 'oversize':
            note = (f'[attachment skipped: exceeds {self.INBOUND_MAX_BYTES} byte limit ({size} bytes) | '
                    f'{kind} | file_id: {file_id} | caption: {caption_display}]')
        elif failure == 'fetch_failed':
            note = (f'[attachment unavailable: could not fetch after {self.INBOUND_DOWNLOAD_ATTEMPTS} attempts | '
                    f'{kind} | file_id: {file_id} | caption: {caption_display}]')
        else:
            note = f'[attachment saved: {path} | {kind} | {size} bytes | caption: {caption_display}]'
        return self._relay_body(header, note, caption_text)

    @staticmethod
    def _format_duration(message, kind):
        media = message.get(kind)
        seconds = media.get('duration') if isinstance(media, dict) else None
        if type(seconds) is not int or seconds < 0:
            return '?:??'
        return f'{seconds // 60}:{seconds % 60:02d}'

    def _kill_group(self, proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass

    # Seconds of audio assumed when Telegram gave no duration.
    TRANSCRIBE_UNKNOWN_SECONDS = 600
    # Margin over the transcriber's own run limit (its decode step plus the
    # scaled whisper limit), so the transcriber normally stops itself first.
    TRANSCRIBE_GRACE = 30
    # The line the adapter prints when its turn in the host-wide queue starts.
    TRANSCRIBE_RUNNING_LINE = voice_transcribe.RUNNING_LINE.encode()
    # With gpu on, a GPU run may take this share of the run limit (at least
    # this many seconds) before the transcriber moves the note to the CPU.
    TRANSCRIBE_GPU_SHARE = voice_transcribe.GPU_LIMIT_SHARE
    TRANSCRIBE_GPU_MIN = voice_transcribe.GPU_MIN_LIMIT
    # The adapter's "device" line: which device ran the note and why:
    # gpu / gpu: <reason> / cpu: <reason> for a reason of 1-32 lower-case
    # letters, digits or hyphens from a GPU check.
    TRANSCRIBE_DEVICES = frozenset({'gpu', 'cpu: vram-low', 'cpu: gpu-failed', 'cpu: gpu-off'})
    TRANSCRIBE_DEVICE_RE = re.compile(r'(?:gpu|cpu): [a-z0-9-]{1,32}')

    @classmethod
    def _valid_transcribe_device(cls, value):
        return isinstance(value, str) and (value in cls.TRANSCRIBE_DEVICES
                                           or cls.TRANSCRIBE_DEVICE_RE.fullmatch(value) is not None)

    def _transcribe_timeout(self, audio_seconds):
        """Seconds one run may take once its turn started: the settings' base
        timeout plus its allowance per minute of audio, so a long note gets the
        time it needs. Waiting in the queue does not count."""
        cfg = self.transcribe
        if type(audio_seconds) is not int or audio_seconds < 0:
            audio_seconds = self.TRANSCRIBE_UNKNOWN_SECONDS
        extra = math.ceil(cfg.get('timeout_per_minute', 0) * audio_seconds / 60)
        return cfg['timeout'] + extra

    def _transcribe_gpu_allowance(self, audio_seconds):
        """Extra seconds of a run with gpu on: a GPU run that hangs or fails
        still leaves the CPU retry its whole run limit."""
        if not (self.transcribe.get('gpu') or self.transcribe.get('method') in ('auto', 'gpu')):
            return 0
        return max(self.TRANSCRIBE_GPU_MIN, math.ceil(self._transcribe_timeout(audio_seconds) / self.TRANSCRIBE_GPU_SHARE))

    @staticmethod
    def _duration_seconds(duration):
        match = re.fullmatch(r'([0-9]+):([0-5][0-9])', duration or '')
        return int(match.group(1)) * 60 + int(match.group(2)) if match else None

    def _run_transcriber(self, path, audio_seconds=None):
        """Transcribe one saved voice note in a throwaway subprocess. Returns
        (text, None, stats) on success or (None, category, stats) where category is one of a
        fixed set of phrases: nothing the child printed reaches the relayed
        text. Child output is read through a hard byte cap and the process
        group is killed on overrun or timeout. stats holds only exit code, byte
        counts and duration; child output is never logged or relayed. The
        child first waits for its turn in the host-wide transcription queue
        with no time limit; the run limit starts at its "running" line. With
        gpu on the child may use the GPU (device auto) and picks it itself;
        stats['device'] is its device line when it gave a valid one. Never
        raises."""
        cfg = self.transcribe
        python, script = cfg['python'], cfg['script']
        device = {'cpu': 'cpu', 'auto': 'check', 'gpu': 'gpu'}.get(cfg.get('method'),
                                                                  'auto' if cfg.get('gpu') else 'cpu')
        stats = dict(exit_code=None, stdout_bytes=0, stderr_bytes=0, seconds=0.0, device=None)
        if shutil.which('ffmpeg') is None:
            return None, 'transcriber missing (ffmpeg not on PATH)', stats
        if not os.access(python, os.X_OK) or not os.path.isfile(script):
            return None, 'transcriber missing (adapter not installed)', stats
        env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR')}
        if device == 'cpu':
            env['CUDA_VISIBLE_DEVICES'] = ''  # CPU only: the GPU may belong to other work
        env.update(cfg['env'])  # the transcriber's paths and limits from the settings
        try:
            proc = subprocess.Popen([python, script, str(path), cfg['model'], device],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=env, start_new_session=True)
        except OSError:
            return None, 'transcriber missing (could not start)', stats
        out, err_bytes, failure = bytearray(), 0, None
        started = time.monotonic()
        deadline = None  # set when the child's turn in the queue starts
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ, 'out')
        sel.register(proc.stderr, selectors.EVENT_READ, 'err')
        try:
            while sel.get_map() and failure is None:
                if deadline is None and self.TRANSCRIBE_RUNNING_LINE + b'\n' in out:
                    deadline = (time.monotonic() + self._transcribe_timeout(audio_seconds)
                                + self._transcribe_gpu_allowance(audio_seconds)
                                + self.transcribe['timeout'] + self.TRANSCRIBE_GRACE)
                remaining = 1.0 if deadline is None else deadline - time.monotonic()
                if remaining <= 0:
                    failure = 'timeout'
                    break
                for key, _ in sel.select(min(remaining, 1.0)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        sel.unregister(key.fileobj)
                    elif key.data == 'out':
                        out += chunk
                        if len(out) > self.TRANSCRIPT_MAX_BYTES:
                            failure = 'output too large'
                            break
                    else:
                        err_bytes += len(chunk)  # counted, never kept: it may carry credentials
            if failure is None:
                try:
                    proc.wait(timeout=max(deadline - time.monotonic(), 0.1) if deadline is not None else 5)
                except subprocess.TimeoutExpired:
                    failure = 'timeout'
        finally:
            sel.close()
            if failure is not None or proc.poll() is None:
                self._kill_group(proc)
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()
        stats.update(exit_code=proc.returncode, stdout_bytes=len(out), stderr_bytes=err_bytes,
                     seconds=round(time.monotonic() - started, 1))
        if failure is not None:
            return None, failure, stats
        if proc.returncode != 0:
            return None, 'transcriber exited with an error', stats
        try:
            result = json.loads(bytes(out).decode('utf-8', 'replace').strip().splitlines()[-1])
            if not isinstance(result, dict):
                return None, 'invalid output', stats
            if 'device' in result:
                if not self._valid_transcribe_device(result['device']):
                    return None, 'invalid output', stats
                stats['device'] = result['device']
            if 'error' in result:
                return None, self.TRANSCRIBER_ERRORS.get(result['error'], 'invalid output'), stats
            text = result['text']
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return None, 'invalid output', stats
        if not isinstance(text, str):
            return None, 'invalid output', stats
        text = self._clean_line(text, self.TRANSCRIPT_MAX_CHARS)
        return (text, None, stats) if text else (None, 'no speech detected', stats)

    @classmethod
    def _clean_line(cls, value, limit):
        value = ' '.join(cls._CONTROL_RE.sub(' ', value).split())
        return value if len(value) <= limit else value[:limit - 1] + '\u2026'

    def _format_voice_pending(self, kind, duration, path, caption, header=None):
        caption_text = caption.strip() if isinstance(caption, str) and caption.strip() else None
        note = f'[attachment saved: {path} | {kind} {duration} | transcribing]'
        return self._relay_body(header, note, caption_text)

    def _format_voice_transcript(self, kind, duration, mid, path, transcript, reason, header=None):
        body = transcript if reason is None else f'transcription failed: {reason}'
        if header is None:
            return f'[{kind} {duration} transcript (msg {mid}): {body}] (audio: {path})'
        if reason is not None:
            return self._relay_body(header, f'[{kind} {duration} transcript (msg {mid}): {body}] (audio: {path})', None)
        return self._relay_body(header, f'[{kind} {duration} transcript (msg {mid}): quoted below] (audio: {path})',
                                transcript)

    # ---- durable transcript queue -------------------------------------------
    # One <message_id>-transcript.pending JSON file per queued voice note sits
    # next to its audio in the date directory. Every read, write and delete goes
    # through a descriptor pinned to that directory (same fence as the download),
    # so a swapped symlink can never redirect it. The file names the audio by
    # basename only; the path is always rebuilt from the pinned directory.

    def _open_day_dir(self, day_dir):
        """Verify containment and return a descriptor pinned to the date directory."""
        _, root_resolved = self._inbound_root()
        self._verify_contained(day_dir, root_resolved, 'inbound date directory')
        return self._open_pinned_dir(day_dir, 'inbound date directory',
                                     open_error='inbound date directory could not be opened safely')

    def _pending_write(self, day_dir, mid, state):
        dfd = self._open_day_dir(day_dir)
        try:
            def write(fd):
                os.write(fd, json.dumps(state).encode())
                os.fchmod(fd, 0o600)
            self._atomic_write_pinned(dfd, f'{mid}{self.PENDING_SUFFIX}', 'pending', write)
        except OSError as error:
            raise TransportError('pending transcript file write failed') from error
        finally:
            os.close(dfd)

    def _pending_read(self, day_dir, mid):
        """Return the parsed pending state, or None when the file is absent."""
        dfd = self._open_day_dir(day_dir)
        try:
            try:
                fd = os.open(f'{mid}{self.PENDING_SUFFIX}', os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=dfd)
            except FileNotFoundError:
                return None
            except OSError as error:
                raise TransportError('pending transcript file refused (not a plain file)') from error
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise TransportError('pending transcript file refused (not a plain file)')
                raw = os.read(fd, self.PENDING_MAX_BYTES + 1)
                if len(raw) > self.PENDING_MAX_BYTES:
                    raise TransportError('pending transcript file is too large')
            finally:
                os.close(fd)
        finally:
            os.close(dfd)
        try:
            state = json.loads(raw.decode('utf-8'))
        except ValueError as error:
            raise TransportError('pending transcript file is not valid JSON') from error
        if (not isinstance(state, dict) or not isinstance(state.get('audio_name'), str)
                or not self._AUDIO_NAME_RE.match(state['audio_name'])
                or type(state.get('attempts')) is not int or state.get('kind') not in self.TRANSCRIBE_KINDS
                or not isinstance(state.get('duration'), str)):
            raise TransportError('pending transcript file has an unexpected shape')
        return state

    def _pending_delete(self, day_dir, mid):
        dfd = self._open_day_dir(day_dir)
        try:
            os.unlink(f'{mid}{self.PENDING_SUFFIX}', dir_fd=dfd)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise TransportError('pending transcript file could not be removed') from error
        finally:
            os.close(dfd)

    def _enqueue_transcription(self, job):
        with self.transcribe_worker_lock:
            if self.transcribe_worker is None:
                self.transcribe_worker = threading.Thread(target=self._transcribe_loop, daemon=True,
                                                          name='voice-transcriber')
                self.transcribe_worker.start()
        self.transcribe_queue.put(job)

    def _transcribe_loop(self):
        while True:
            job = self.transcribe_queue.get()
            try:
                self._process_transcription(*job)
            except Exception:  # noqa: BLE001 - the worker must outlive any single note
                print('voice transcript job failed', file=sys.stderr, flush=True)
            finally:
                self.transcribe_queue.task_done()

    def _process_transcription(self, mid, day_dir):
        state = self._pending_read(day_dir, mid)
        if state is None or state.get('gave_up') or not state.get('note_sent'):
            return  # already delivered, abandoned, or its first note is still to be redelivered
        path = day_dir / state['audio_name']
        transcript, reason, stats = self._run_transcriber(path, self._duration_seconds(state['duration']))
        # One line per note: the device and why, never the text.
        device = f"; device {stats['device']}" if stats.get('device') else ''
        if reason is not None:
            print(f"voice transcription failed (msg {mid}): {reason}; exit {stats['exit_code']}; "
                  f"stdout {stats['stdout_bytes']} B; stderr {stats['stderr_bytes']} B; {stats['seconds']} s{device}",
                  file=sys.stderr, flush=True)
        else:
            print(f"voice transcription done (msg {mid}): {stats['seconds']} s{device}", file=sys.stderr, flush=True)
        # A file written before forwards were relayed has no "forwarded" key and
        # delivers unmarked; a malformed one still gets the header.
        header = self._forward_header(state['forwarded']) if 'forwarded' in state else None
        text = self._format_voice_transcript(state['kind'], state['duration'], mid, path, transcript, reason, header)
        # The provider id is the idempotency key: a retry after a relay whose
        # acknowledgement was lost lands on the same GC entry, never a second one.
        while True:
            try:
                self._relay_to_gc(f'{mid}-transcript', text)
                self._pending_delete(day_dir, mid)
                return
            except TransportError:
                state['attempts'] += 1
                if state['attempts'] >= self.PENDING_RELAY_ATTEMPTS:
                    state['gave_up'] = True
                    self._pending_write(day_dir, mid, state)
                    print(f'voice transcript relay gave up (msg {mid}): relay failed after '
                          f"{state['attempts']} attempts", file=sys.stderr, flush=True)
                    return
                self._pending_write(day_dir, mid, state)
                backoff = self.PENDING_BACKOFF_SECONDS[min(state['attempts'] - 1, len(self.PENDING_BACKOFF_SECONDS) - 1)]
                if self.stop.wait(backoff):
                    return  # shutting down: the file stays for the next start

    def reconcile_pending(self):
        """Re-enqueue pending transcripts left by a previous run. Same
        containment rule as the reaper: the state directory and the inbound
        root must be real directories, and each date directory a real,
        contained directory, before anything is read. Returns the count queued."""
        if self.state_dir is None or not self.transcribe:
            return 0
        root_resolved = self.state_dir.resolve(strict=False)
        chain = self.state_dir / 'telegram-inbound'
        try:
            for entry, what in ((self.state_dir, 'state directory'), (chain, 'inbound root')):
                if not stat.S_ISDIR(os.lstat(entry).st_mode):
                    return 0
                self._verify_contained(entry, root_resolved, what)
        except (OSError, TransportError):
            return 0
        queued = 0
        for day in sorted(os.listdir(chain)):
            day_dir = chain / day
            try:
                if not stat.S_ISDIR(os.lstat(day_dir).st_mode):
                    continue
                dfd = self._open_day_dir(day_dir)
            except (OSError, TransportError):
                continue
            try:
                names = os.listdir(dfd)
            finally:
                os.close(dfd)
            for name in sorted(names):
                match = self._PENDING_NAME_RE.match(name)
                if not match:
                    continue
                mid = int(match.group(1))
                try:
                    state = self._pending_read(day_dir, mid)
                except TransportError:
                    print(f'voice pending file refused (msg {mid})', file=sys.stderr, flush=True)
                    continue
                if state is None or state.get('gave_up') or not state.get('note_sent'):
                    continue
                self._enqueue_transcription((mid, day_dir))
                queued += 1
        return queued

    def _relay_to_gc(self, provider_message_id, text, reply=None):
        normalized = dict(provider_message_id=str(provider_message_id), conversation=self.conversation,
            actor=dict(id=str(self.config['user_id']), display_name=self.display_name, is_bot=False), text=text,
            received_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        if reply is not None:
            normalized['reply_to_message_id'] = str(reply)
        result = self.gc('inbound', {'message': normalized})
        # GC can return HTTP 200 without a transcript when routing is absent or
        # hydration is pending. Only a matching durable entry acknowledges input.
        entry = result.get('TranscriptEntry')
        if (result.get('TargetAgentName') != self.agent or result.get('TargetSessionID')
            or not isinstance(entry, dict)
            or not isinstance(entry.get('ID'), str) or not entry['ID']
            or type(entry.get('Sequence')) is not int or entry['Sequence'] <= 0
            or entry.get('Kind') != 'inbound'
            or entry.get('Conversation') != self.conversation
            or entry.get('ProviderMessageID') != str(provider_message_id)
            or entry.get('Actor') != normalized['actor']):
            raise TransportError('GC has not confirmed a matching durable inbound entry routed to the agent')

    @staticmethod
    def _update_id(update):
        uid = update.get('update_id')
        if type(uid) is not int or uid < 0:
            raise TransportError('Telegram update has invalid ID')
        return uid

    @staticmethod
    def _message_id(message):
        mid = message.get('message_id')
        if type(mid) is not int or mid <= 0:
            raise TransportError('authorized inbound message has invalid ID')
        return mid

    def inbound(self, update):
        if 'callback_query' in update:
            self.inbound_callback(self._update_id(update), update['callback_query'])
            return
        message = update.get('message')
        if not isinstance(message, dict):
            return
        chat, sender = message.get('chat', {}), message.get('from', {})
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return
        if (chat.get('type') != 'private' or type(chat.get('id')) is not int or chat['id'] != self.config['chat_id']
            or type(sender.get('id')) is not int or sender['id'] != self.config['user_id'] or sender.get('is_bot') is not False):
            return
        # Forwarded content is not an instruction authored by the paired user:
        # it never runs a command or /start, and reaches the agent only quoted
        # under a forwarded header.
        forward = self._forward_source(message) if any(key in message for key in self.FORWARD_KEYS) else None
        header = self._forward_header(forward) if forward is not None else None

        attachment = self._extract_attachment(message)
        text = message.get('text')
        caption = message.get('caption')
        found = self._match_command(text) if forward is None and attachment is None and isinstance(text, str) else None
        if found is not None:
            # A city command goes to its handler, never to GC.
            command, args = found
            uid, mid = self._update_id(update), self._message_id(message)
            self.run_command(command, uid, dict(
                kind='command', command=command['name'], args=args, update_id=uid, message_id=mid,
                callback_data=None, callback_message_id=None, received_at=self._received_at(),
                user_display_name=self.display_name))
            return
        if attachment is None and (not isinstance(text, str) or not text.strip()
                                   or (forward is None and text.startswith('/start'))):
            return

        mid = self._message_id(message)

        transcribe_job = None
        if attachment is not None:
            kind, file_id, known_size, original_name = attachment
            # The download happens before the GC call below so a durably
            # accepted inbound entry always has its file already on disk.
            path, size, failure = self._fetch_inbound_attachment(kind, file_id, known_size, mid, original_name)
            if self.transcribe and failure is None and kind in self.TRANSCRIBE_KINDS:
                # The note goes out now; the transcript follows as a second
                # message from the worker so slow speech-to-text never holds
                # the inbound cursor.
                duration = self._format_duration(message, kind)
                state = dict(version=1, kind=kind, duration=duration, message_id=mid, provider_id=f'{mid}-transcript',
                             audio_name=path.name, attempts=0, note_sent=False, gave_up=False,
                             created_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
                if forward is not None:
                    state['forwarded'] = forward
                try:
                    # Persisted before the first note goes out, so a restart at
                    # any later point can still deliver the transcript.
                    if self._pending_read(path.parent, mid) is None:
                        self._pending_write(path.parent, mid, state)
                    relay_text = self._format_voice_pending(kind, duration, path, caption, header)
                    transcribe_job = (mid, path.parent, state)
                except TransportError:
                    print(f'voice transcript queue unavailable (msg {mid}); relaying without transcription',
                          file=sys.stderr, flush=True)
                    relay_text = self._format_attachment_relay(kind, file_id, path, size, failure, caption, header)
            else:
                relay_text = self._format_attachment_relay(kind, file_id, path, size, failure, caption, header)
        else:
            relay_text = text if header is None else self._relay_body(header, None, text)

        reply = message.get('reply_to_message', {}).get('message_id')
        if type(reply) is not int or reply <= 0:
            reply = None
        self._relay_to_gc(str(mid), relay_text, reply)
        self._accepted(mid)
        # The message above is now durably queued for the mayor but it may be
        # quota-blocked and unable to answer it any time soon; tell the user once
        # per exhaustion episode rather than leaving the user wondering why the
        # bridge went silent.
        self.notify_quota_exhausted()
        if transcribe_job is not None:
            mid, day_dir, state = transcribe_job
            state['note_sent'] = True
            try:
                self._pending_write(day_dir, mid, state)
            except TransportError:
                # The marker is what lets a restart find this job; without it the
                # follow-up would be lost. Retain the cursor so the update is
                # reprocessed: the first note re-relays under the same provider
                # id (GC dedups it) and the marker is written again.
                print(f'voice transcript queue not updated (msg {mid}); cursor retained', file=sys.stderr, flush=True)
                raise
            self._enqueue_transcription((mid, day_dir))

    # ---- reactions -------------------------------------------------------------
    # setMessageReaction replaces the message's whole reaction list (a bot sets
    # at most one), so a repeat can never stack a second reaction.

    def _accepted(self, mid):
        """The accept point: GC durably accepted message `mid` from the paired
        user. Record it as the last accepted message and, with on_accept,
        queue the seen reaction for the worker, and with [typing] open or
        extend the typing window. Never raises: the inbound cursor never
        waits on, or is retained by, a reaction or a chat action."""
        try:
            self.store.record_accepted(mid, datetime.datetime.now(datetime.timezone.utc).isoformat())
        except Exception:  # noqa: BLE001 - the relay already succeeded
            self._log(f'accepted message not recorded (msg {mid})')
        if self.reactions['on_accept']:
            try:
                self._enqueue_reaction(mid)
            except Exception:  # noqa: BLE001 - e.g. no thread could start; the relay already succeeded
                self._log(f'reaction not set (msg {mid}): reaction_worker_unavailable')
        if self.typing_seconds:
            self._open_typing()

    def _enqueue_reaction(self, mid):
        with self.reaction_worker_lock:
            if self.reaction_worker is None:
                worker = threading.Thread(target=self._reaction_loop, daemon=True, name='accept-reaction')
                worker.start()
                self.reaction_worker = worker
        self.reaction_queue.put(mid)

    def _reaction_loop(self):
        while True:
            mid = self.reaction_queue.get()
            try:
                receipt = self.set_reaction(mid, self.reactions['seen'], f'reaction:{mid}:accept', once=True)
                if receipt is not None and not receipt.get('delivered'):
                    self._log(f"reaction not set (msg {mid}): {receipt['metadata']['reason']}")
            except Exception:  # noqa: BLE001 - the worker must outlive any single reaction
                self._log(f'reaction not set (msg {mid}): reaction_failed')
            finally:
                self.reaction_queue.task_done()

    @contextlib.contextmanager
    def _reaction_lock(self):
        """An flock beside the ledger that the daemon's accept worker and every
        `react` process hold from their check to Telegram's answer, so the two
        never interleave: a seen still in flight finishes before a done goes."""
        fd = os.open(self.store.path + '.reactions.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def set_reaction(self, message_id, emoji, key, once=False):
        """Set one emoji reaction (None clears) on a message of the paired chat
        under its own ledger key. With `once` (the accept reaction) a not-sent
        attempt is saved as final, so a replayed update never sets it later,
        and nothing is sent once a `react` call reserved one for the message:
        None then. setMessageReaction replaces the whole list, so a late seen
        would erase a done."""
        body = dict(chat_id=self.config['chat_id'], message_id=int(message_id),
                    reaction=[dict(type='emoji', emoji=emoji)] if emoji else [])

        def deliver(pending):
            receipt = pending
            try:
                status, data = self._send('setMessageReaction',
                                          lambda timeout: self.telegram('setMessageReaction', body, timeout))
                if status == 200 and data.get('ok') is True and data.get('result') is True:
                    receipt = self.receipt(True, message_id=str(message_id))
                elif data.get('ok') is False and 400 <= data.get('error_code', 0) < 500:
                    receipt = self.receipt(reason='telegram_rejected_not_sent_' + str(data['error_code']))
            except RequestNotSent:
                receipt = self.receipt(reason=NOT_SENT_REASON) if once else self.not_sent()
            except TransportError:
                pass
            return receipt
        with self._reaction_lock():
            if once and self.store.agent_reacted(message_id):
                return None
            return self._reserve_and_deliver(key, body, deliver)

    # ---- typing ([typing]) -----------------------------------------------------
    # sendChatAction delivers nothing to the chat history, so it is the one
    # Telegram call outside the ledger: no key, no row, one attempt, never
    # retried. One worker (started by run) sends it; the poll loop only opens
    # the window, and nothing here takes publish_lock.

    def _open_typing(self):
        """Open the typing window for an accepted message, or extend the open
        one to `seconds` from now. A window remembers the ledger's newest
        delivery row when it began; one that already ended, at its end time
        or by a send, before the worker looked, begins again with a new mark
        and its own failure log. Never raises."""
        with self.typing_lock:
            window = self.typing_window
        now = self.monotonic()
        try:
            if window is not None and now < window['end'] and not self.store.sent_since(window['mark']):
                mark, failed = window['mark'], window['failed']
            else:
                mark, failed = self.store.deliveries_mark(), False
        except Exception:  # noqa: BLE001 - the relay already succeeded
            self._log('typing not shown: ledger_unavailable')
            return
        with self.typing_lock:
            self.typing_window = dict(end=now + self.typing_seconds, mark=mark, due=now, failed=failed)
        self.typing_wake.set()

    def _start_typing(self):
        """Start the typing worker; one that cannot start only logs."""
        try:
            worker = threading.Thread(target=self._typing_loop, daemon=True, name='typing')
            worker.start()
            self.typing_worker = worker
        except Exception:  # noqa: BLE001 - typing is cosmetic; the bridge runs without it
            self._log('typing not shown: typing_worker_unavailable')

    def _typing_loop(self):
        """The typing worker: a tick at least every TYPING_TICK seconds, and at
        once when a message is accepted, until bridge.stop ends the window."""
        while not self.stop.is_set():
            self.typing_wake.clear()
            try:
                self._typing_tick()
            except Exception:  # noqa: BLE001 - the worker must outlive any single tick
                with self.typing_lock:
                    window = self.typing_window
                if window is not None:
                    self._end_typing(window, 'typing_failed')
            self.typing_wait(TYPING_TICK)
        with self.typing_lock:
            self.typing_window = None

    def _typing_tick(self):
        """One look at the typing window: end it at its end time or once the
        ledger holds a send newer than its mark, else send the chat action
        when one is due."""
        with self.typing_lock:
            window = self.typing_window
        if window is None:
            return
        now = self.monotonic()
        if now >= window['end']:
            return self._end_typing(window)
        try:
            sent = self.store.sent_since(window['mark'])
        except Exception:  # noqa: BLE001 - a send it cannot see must not leave typing on
            return self._end_typing(window, 'ledger_unavailable')
        if sent:
            return self._end_typing(window)
        with self.typing_lock:
            due = now >= window['due']
            if due:
                window['due'] = now + TYPING_INTERVAL
        if due:
            self._send_typing(window)

    def _send_typing(self, window):
        """One sendChatAction typing to the paired chat, a single attempt of at
        most TYPING_TIMEOUT seconds. A definite rejection ends the window (the
        next would be refused alike); any other failure leaves it to the next
        due time."""
        body = dict(chat_id=self.config['chat_id'], action='typing')
        try:
            status, data = self.telegram('sendChatAction', body, timeout=TYPING_TIMEOUT)
        except TransportError:
            return self._typing_failed(window, 'transport_unavailable')
        if status == 200 and data.get('ok') is True and data.get('result') is True:
            return
        code = data.get('error_code')
        if data.get('ok') is False and type(code) is int and 400 <= code < 500:
            return self._end_typing(window, f'telegram_rejected_{code}')
        self._typing_failed(window, 'telegram_unconfirmed')

    def _typing_failed(self, window, reason):
        """Log a failure once per window: its reason, never text."""
        with self.typing_lock:
            logged, window['failed'] = window['failed'], True
        if not logged:
            self._log(f'typing not shown: {reason}')

    def _end_typing(self, window, reason=None):
        if reason is not None:
            self._typing_failed(window, reason)
        with self.typing_lock:
            if self.typing_window is window:
                self.typing_window = None

    # ---- city commands ([[command]]) -----------------------------------------
    # A registered slash command or a button press under a registered
    # callback prefix goes to the city's handler, never to GC. The handler gets
    # one JSON object and returns {"actions": [...]}; the bridge validates the
    # whole answer before it acts, then sends and edits through the delivery
    # ledger under command:<update_id>:<index> and answers a callback under
    # command:<update_id>:answer. Nothing the handler prints reaches a log.

    COMMAND_PATH = '/usr/local/bin:/usr/bin:/bin'
    COMMAND_OUTPUT_MAX_BYTES = 64 * 1024
    COMMAND_MAX_ACTIONS = 32
    COMMAND_KEYBOARD_MAX_BUTTONS = 100
    COMMAND_KEYBOARD_MAX_PER_ROW = 8
    COMMAND_BUTTON_TEXT_MAX = 256
    COMMAND_ANSWER_MAX = 200
    CALLBACK_DATA_MAX_BYTES = 64
    # The bridge-owned callback prefix for mayor ask buttons
    # (callback_data ask:<ask-id>:<choice>). No [[command]] callback_prefix
    # may equal or overlap it; no handler output may use it.
    ASK_CALLBACK_PREFIX = 'ask:'
    ASK_ALREADY_ANSWERED = 'Already answered'
    # The line a close (ask-close) appends to the ask's text or caption.
    ASK_DECIDED_PREFIX = '\n✓ Decided: '
    _COMMAND_ACTION_KEYS = {
        'send': ({'type', 'text'}, {'format', 'keyboard'}),
        'edit': ({'type', 'message_id', 'text'}, {'format', 'keyboard'}),
        'answer_callback': ({'type'}, {'text', 'alert'}),
    }
    _COMMAND_TEXT_RE = re.compile(r'/([a-z0-9_]{1,32})(?:@([A-Za-z0-9_]{1,64}))?(?: (.*))?\Z', re.DOTALL)

    class _HandlerFailure(Exception):
        """A handler answer the bridge will not act on; args[0] is a fixed reason code."""

    def _match_command(self, text):
        """(command, args) for a registered /<name> or /<name>@<this bot>, alone
        or followed by a space and arguments; None for anything else."""
        match = self._COMMAND_TEXT_RE.match(text)
        if not match or match.group(1) not in self.commands:
            return None
        if match.group(2) is not None and match.group(2).lower() != str(self.account).lower():
            return None  # addressed to another bot
        return self.commands[match.group(1)], match.group(3) or ''

    def _callback_command(self, data):
        for command in self.commands.values():
            if data.startswith(command['callback_prefix']):
                return command  # prefixes never start one another, so at most one matches
        return None

    @classmethod
    def _ask_choice(cls, data):
        """(ask_id, choice) of bridge-owned callback_data ask:<ask-id>:<choice>,
        or None when it is not that shape. The choice may itself hold colons."""
        if not isinstance(data, str) or not data.startswith(cls.ASK_CALLBACK_PREFIX):
            return None
        ask_id, sep, choice = data[len(cls.ASK_CALLBACK_PREFIX):].partition(':')
        if not sep or not ask_id or not choice:
            return None
        return ask_id, choice

    @staticmethod
    def _ask_id_for_key(key):
        """The ask id of one keyboard send: the first 16 hex characters of the
        SHA-256 of its idempotency key, so one ask belongs to one logical send
        and reusing an ask id under another key is refused, never rebound."""
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    @staticmethod
    def _ask_text_breaks_answer_line(text):
        """True when a button label would split or blur the relayed answer.

        The answer's first line separates its fields with ' | ' inside
        '[...]': a label holding a line break or control character would
        make it ambiguous. Refuse when splitlines would split it or any
        character has Unicode category Cc, Zl or Zp; Cf (emoji ZWJ)
        stays valid. The visible question is free text and is never
        checked here: it rides verbatim as the last field, after a
        newline, so nothing in it can blur a field."""
        return (text.splitlines() != [text]
                or any(unicodedata.category(ch) in ('Cc', 'Zl', 'Zp') for ch in text))

    def _handler_environment(self, name):
        return dict(PATH=self.COMMAND_PATH, HOME=os.environ.get('HOME') or str(Path.home()),
                    LANG=os.environ.get('LANG') or 'C.UTF-8', TZ=self.config.get('timezone') or 'UTC',
                    TELEGRAM_BRIDGE_COMMAND=name)

    def _run_executable_handler(self, command, data):
        """The handler's stdout bytes. argv is the handler alone (no shell),
        stdin the JSON, cwd its directory, the environment fixed; its process
        group is killed at the timeout or on oversize output. Its stderr is
        discarded: handler output never reaches a log."""
        handler = command['handler']
        try:
            proc = subprocess.Popen([handler], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, cwd=os.path.dirname(handler),
                                    env=self._handler_environment(command['name']), start_new_session=True)
        except OSError:
            raise self._HandlerFailure('handler_not_started') from None
        deadline = time.monotonic() + command['timeout']
        out, failure, pending = bytearray(), None, memoryview(data)
        sel = selectors.DefaultSelector()
        try:
            os.set_blocking(proc.stdin.fileno(), False)
            sel.register(proc.stdin, selectors.EVENT_WRITE, 'in')
            sel.register(proc.stdout, selectors.EVENT_READ, 'out')
            while sel.get_map() and failure is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = 'timeout'
                    break
                for key, _ in sel.select(remaining):
                    if key.data == 'in':
                        try:
                            pending = pending[os.write(key.fd, pending):]
                        except BlockingIOError:
                            continue
                        except OSError:
                            pending = pending[:0]  # it closed its stdin; its exit status decides
                        if not pending:
                            sel.unregister(proc.stdin)
                            proc.stdin.close()
                        continue
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        sel.unregister(proc.stdout)
                    else:
                        out += chunk
                        if len(out) > self.COMMAND_OUTPUT_MAX_BYTES:
                            failure = 'output_too_large'
                            break
            if failure is None:
                try:
                    proc.wait(timeout=max(deadline - time.monotonic(), 0.01))
                except subprocess.TimeoutExpired:
                    failure = 'timeout'
        finally:
            sel.close()
            if failure is not None or proc.poll() is None:
                self._kill_group(proc)
            proc.wait()
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
        if failure is not None:
            raise self._HandlerFailure(failure)
        if proc.returncode != 0:
            raise self._HandlerFailure('exit_status')
        return bytes(out)

    def _post_url_handler(self, command, data, connection=None):
        """The body of one POST of the JSON to a loopback handler URL, through
        http.client: no proxy from the environment, no redirect followed, the
        same timeout over the whole exchange, the read capped at 64 KiB."""
        parsed = urlsplit(command['url'])
        deadline = time.monotonic() + command['timeout']

        def remaining():
            left = deadline - time.monotonic()
            if left <= 0:
                raise self._HandlerFailure('timeout')
            return left

        conn = (connection or http.client.HTTPConnection)(parsed.hostname, parsed.port, timeout=command['timeout'])
        try:
            conn.connect()
            sock = conn.sock
            # Each blocking step gets only what is left of the one deadline:
            # writing the request, the status line and headers, each read.
            sock.settimeout(remaining())
            conn.request('POST', parsed.path, body=data, headers={
                'Content-Type': 'application/json', 'Accept': 'application/json', 'Connection': 'close'})
            sock.settimeout(remaining())
            response = conn.getresponse()
            if response.status != 200:
                raise self._HandlerFailure('http_status')
            body = bytearray()
            # The response closes its socket once the whole body is read.
            while not response.isclosed():
                sock.settimeout(remaining())
                chunk = response.read1(65536)
                if not chunk:
                    break
                body += chunk
                if len(body) > self.COMMAND_OUTPUT_MAX_BYTES:
                    raise self._HandlerFailure('output_too_large')
            return bytes(body)
        except TimeoutError:
            raise self._HandlerFailure('timeout') from None
        except (OSError, http.client.HTTPException):
            raise self._HandlerFailure('handler_unreachable') from None
        finally:
            conn.close()

    @staticmethod
    def _strict_object(pairs):
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError('duplicate key')
        return dict(pairs)

    @staticmethod
    def _no_constant(_name):
        raise ValueError('NaN or Infinity')

    def _command_text(self, action):
        """(text, plain) of a send or edit: plain is the visible text for html,
        else None. The limits are those of publish()."""
        text, fmt = action['text'], action.get('format', 'plain')
        if fmt not in ('plain', 'html') or not isinstance(text, str):
            raise self._HandlerFailure('invalid_text')
        plain = None
        if fmt == 'html':
            plain, error = html_visible_text(text)
            if error:
                raise self._HandlerFailure('invalid_html')
        visible = text if plain is None else plain
        if not visible.strip() or utf16_units(visible) > 4096:
            raise self._HandlerFailure('invalid_text')
        return text, plain

    def _command_keyboard(self, keyboard):
        if (not isinstance(keyboard, list) or not keyboard
                or any(not isinstance(row, list) or not 1 <= len(row) <= self.COMMAND_KEYBOARD_MAX_PER_ROW
                       for row in keyboard)
                or sum(len(row) for row in keyboard) > self.COMMAND_KEYBOARD_MAX_BUTTONS):
            raise self._HandlerFailure('invalid_keyboard')
        rows = []
        for row in keyboard:
            buttons = []
            for button in row:
                if not isinstance(button, dict) or set(button) != {'text', 'callback_data'}:
                    raise self._HandlerFailure('invalid_keyboard')
                text, data = button['text'], button['callback_data']
                if (not isinstance(text, str) or not text.strip() or len(text) > self.COMMAND_BUTTON_TEXT_MAX
                        or not isinstance(data, str) or not 1 <= len(data.encode()) <= self.CALLBACK_DATA_MAX_BYTES):
                    raise self._HandlerFailure('invalid_keyboard')
                if self._callback_command(data) is None:
                    raise self._HandlerFailure('foreign_callback_data')
                buttons.append(dict(text=text, callback_data=data))
            rows.append(buttons)
        return rows

    def _publish_keyboard(self, keyboard):
        """(rows, None) for a valid publish() keyboard, (None, reason) otherwise.

        The same row, button-count, text and size limits as a handler keyboard
        apply. Every button must name a registered command prefix, except the
        bridge-owned ask: buttons, whose ask:<ask-id>:<choice> shape is checked
        here; handler output still refuses those as foreign. Reasons match the
        handler failure codes, and reach publish() callers as receipts."""
        if (not isinstance(keyboard, list) or not keyboard
                or any(not isinstance(row, list) or not 1 <= len(row) <= self.COMMAND_KEYBOARD_MAX_PER_ROW
                       for row in keyboard)
                or sum(len(row) for row in keyboard) > self.COMMAND_KEYBOARD_MAX_BUTTONS):
            return None, 'invalid_keyboard'
        rows, seen = [], set()
        for row in keyboard:
            buttons = []
            for button in row:
                if not isinstance(button, dict) or set(button) != {'text', 'callback_data'}:
                    return None, 'invalid_keyboard'
                text, data = button['text'], button['callback_data']
                if (not isinstance(text, str) or not text.strip() or len(text) > self.COMMAND_BUTTON_TEXT_MAX
                        or not isinstance(data, str) or not 1 <= len(data.encode()) <= self.CALLBACK_DATA_MAX_BYTES):
                    return None, 'invalid_keyboard'
                if data in seen:
                    # Two buttons sharing callback_data would answer the same:
                    # inbound_ask matches the first, so the second label could
                    # never be relayed. Refuse rather than mislabel an answer.
                    return None, 'invalid_keyboard'
                seen.add(data)
                if data.startswith(self.ASK_CALLBACK_PREFIX):
                    if self._ask_choice(data) is None:
                        return None, 'invalid_keyboard'
                    if ('|' in text or ']' in text
                            or self._ask_text_breaks_answer_line(text)):
                        # The relayed answer's first line separates its
                        # fields with ' | ' inside '[...]': a label holding
                        # '|', ']', a line break or a control character
                        # would make that line ambiguous.
                        return None, 'invalid_keyboard'
                elif self._callback_command(data) is None:
                    return None, 'foreign_callback_data'
                buttons.append(dict(text=text, callback_data=data))
            rows.append(buttons)
        return rows, None

    def parse_handler_output(self, raw, kind):
        """The validated action list of one handler answer, or _HandlerFailure.
        Exactly {"actions": [...]}, strict JSON (no duplicate keys, no NaN),
        at most 32 actions; answer_callback only for a callback, once."""
        if len(raw) > self.COMMAND_OUTPUT_MAX_BYTES:
            raise self._HandlerFailure('output_too_large')
        try:
            answer = json.loads(raw.decode('utf-8'), object_pairs_hook=self._strict_object,
                                parse_constant=self._no_constant)
        except (UnicodeError, ValueError, RecursionError):
            raise self._HandlerFailure('malformed_json') from None
        try:
            # Strict JSON may still escape a lone UTF-16 surrogate ("\ud83d"),
            # which no text, limit count or callback_data can encode.
            json.dumps(answer, ensure_ascii=False).encode('utf-8')
        except UnicodeError:
            raise self._HandlerFailure('invalid_unicode') from None
        except RecursionError:
            raise self._HandlerFailure('malformed_json') from None
        if not isinstance(answer, dict) or set(answer) != {'actions'} or not isinstance(answer['actions'], list):
            raise self._HandlerFailure('not_an_actions_object')
        if len(answer['actions']) > self.COMMAND_MAX_ACTIONS:
            raise self._HandlerFailure('too_many_actions')
        actions, answered = [], False
        for action in answer['actions']:
            if (not isinstance(action, dict) or not isinstance(action.get('type'), str)
                    or action['type'] not in self._COMMAND_ACTION_KEYS):
                raise self._HandlerFailure('unknown_action_type')
            required, optional = self._COMMAND_ACTION_KEYS[action['type']]
            if not required <= set(action) or set(action) - required - optional:
                raise self._HandlerFailure('unknown_action_key')
            if action['type'] == 'answer_callback':
                text, alert = action.get('text', ''), action.get('alert', False)
                if (kind != 'callback' or answered or not isinstance(text, str) or len(text) > self.COMMAND_ANSWER_MAX
                        or type(alert) is not bool):
                    raise self._HandlerFailure('invalid_answer')
                answered = True
                actions.append(dict(type='answer_callback', text=text, alert=alert))
                continue
            text, plain = self._command_text(action)
            parsed = dict(type=action['type'], text=text, format=action.get('format', 'plain'))
            if action['type'] == 'edit':
                if type(action['message_id']) is not int or action['message_id'] <= 0:
                    raise self._HandlerFailure('invalid_message_id')
                parsed['message_id'] = action['message_id']
            if 'keyboard' in action:
                parsed['keyboard'] = self._command_keyboard(action['keyboard'])
            parsed['plain'] = plain
            actions.append(parsed)
        return actions

    def _reserve_and_deliver(self, key, identity, deliver_fn, retry_undelivered=False):
        """The one ledger reservation path for every send: reserve the key
        durably under publish_lock before Telegram is contacted, return a
        saved receipt instead of sending again, and finish with the result
        of deliver_fn(pending). retry_undelivered: see Store.reserve."""
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        with self.publish_lock:
            pending = self.receipt(reason='ambiguous_delivery_requires_manual_reconciliation')
            try:
                cached = self.store.reserve(key, fingerprint, pending, retry_undelivered)
            except TransportError:
                return self.receipt(reason='idempotency_key_conflict')
            if cached:
                return cached
            result = deliver_fn(pending)
            self.store.finish(key, result)
            return result

    def _command_message(self, key, action):
        """Send or edit one message under its ledger key: reserved before
        Telegram is contacted, a saved receipt returned instead of a second
        send, an unconfirmed send left ambiguous."""
        extra = {}
        if 'keyboard' in action:
            extra['reply_markup'] = {'inline_keyboard': action['keyboard']}
        method = 'sendMessage'
        if action['type'] == 'edit':
            method, extra['message_id'] = 'editMessageText', action['message_id']
        identity = {k: v for k, v in action.items() if k != 'plain'}
        return self._reserve_and_deliver(key, identity, lambda pending: self._deliver_text(
            action['text'], None, pending, action['plain'], method=method, extra=extra))

    def _answer_callback(self, update_id, query_id, text='', alert=False, key=None):
        """Answer one callback query once, under command:<update_id>:answer for
        city commands and ask:<update_id>:answer for mayor asks."""
        body = dict(callback_query_id=query_id)
        if text:
            body['text'] = text
        if alert:
            body['show_alert'] = True
        key = key or f'command:{update_id}:answer'
        return self._reserve_and_deliver(key, body, lambda pending: self._deliver_true_call(
            'answerCallbackQuery', body, pending))

    def _deliver_true_call(self, method, body, pending, not_found=None):
        """One Telegram call whose success answers `"result": true` (a
        callback answer, a pin, an unpin): delivered on that, rejected on a
        4xx, not sent when no byte left, else the pending receipt.
        `not_found` = (description fragment, reason) names one 400."""
        receipt = pending
        try:
            status, data = self._send(method, lambda timeout: self.telegram(method, body, timeout))
            if status == 200 and data.get('ok') is True and data.get('result') is True:
                receipt = self.receipt(True)
            elif (not_found and data.get('ok') is False and data.get('error_code') == 400
                  and not_found[0] in str(data.get('description', '')).lower()):
                receipt = self.receipt(reason=not_found[1])
            elif data.get('ok') is False and 400 <= data.get('error_code', 0) < 500:
                receipt = self.receipt(reason='telegram_rejected_not_sent_' + str(data['error_code']))
        except RequestNotSent:
            receipt = self.not_sent()
        except TransportError:
            pass
        return receipt

    @staticmethod
    def _log(line):
        print(line, file=sys.stderr, flush=True)

    def run_command(self, command, update_id, payload, query_id=None):
        """Run the handler for one command or callback and act on its answer.
        A handler failure sends the fixed notice instead; a pending callback is
        always answered (the handler's answer, else empty) before returning."""
        name = command['name']
        data = json.dumps(payload).encode()  # ASCII: a lone surrogate in chat text cannot break it
        try:
            raw = (self._post_url_handler(command, data) if command.get('url')
                   else self._run_executable_handler(command, data))
            actions = self.parse_handler_output(raw, payload['kind'])
        except self._HandlerFailure as failure:
            self._log(f'command {name} failed (update {update_id}): {failure.args[0]}')
            receipt = self._command_message(f'command:{update_id}:notice', dict(
                type='send', text=f'command {name} failed', format='plain', plain=None))
            if receipt.get('delivered') is not True:
                self._log(f'command {name} (update {update_id}): failure notice not delivered: '
                          f"{receipt.get('metadata', {}).get('reason')}")
            if query_id is not None:
                self._answer_callback(update_id, query_id)
            return
        answered = False
        for index, action in enumerate(actions):
            if action['type'] == 'answer_callback':
                receipt = self._answer_callback(update_id, query_id, action['text'], action['alert'])
                answered = True
            else:
                receipt = self._command_message(f'command:{update_id}:{index}', action)
            if receipt.get('delivered') is not True:
                self._log(f"command {name} (update {update_id}): action {index} {action['type']} not delivered: "
                          f"{receipt.get('metadata', {}).get('reason')}")
        if query_id is not None and not answered:
            self._answer_callback(update_id, query_id)
        self._log(f'command {name} handled (update {update_id}): {len(actions)} action(s)')

    def _received_at(self):
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def inbound_callback(self, update_id, query):
        """One callback_query: an authorized press under a registered prefix
        runs its handler; any other is answered empty, logged and dropped."""
        if not isinstance(query, dict) or not isinstance(query.get('id'), str) or not query['id']:
            self._log(f'callback dropped (update {update_id}): malformed')
            return
        sender, message = query.get('from'), query.get('message')
        chat = message.get('chat') if isinstance(message, dict) else None
        data = query.get('data')
        command = None
        if (not isinstance(sender, dict) or type(sender.get('id')) is not int or sender['id'] != self.config['user_id']
                or sender.get('is_bot') is not False or not isinstance(chat, dict) or type(chat.get('id')) is not int
                or chat['id'] != self.config['chat_id']):
            reason = 'not_authorized'
        elif not isinstance(data, str) or not data:
            reason = 'no_registered_prefix'
        elif data.startswith(self.ASK_CALLBACK_PREFIX):
            self.inbound_ask(update_id, query, data)
            return
        elif (command := self._callback_command(data)) is None:
            reason = 'no_registered_prefix'
        else:
            reason = None
        if command is None:
            self._log(f'callback dropped (update {update_id}): {reason}')
            self._answer_callback(update_id, query['id'])
            return
        mid = message.get('message_id')
        payload = dict(kind='callback', command=command['name'], args=None, update_id=update_id, message_id=None,
                       callback_data=data, callback_message_id=mid if type(mid) is int and mid > 0 else None,
                       received_at=self._received_at(), user_display_name=self.display_name)
        self.run_command(command, update_id, payload, query_id=query['id'])

    def inbound_ask(self, update_id, query, data):
        """One tap on a mayor ask button: record the answer, relay it once,
        then edit the ask message to show the choice.

        P1 A keyboard goes with no attachment or with exactly one photo; any
        other attachment kind (or more than one) refuses the send as
        invalid_keyboard. D1's text-only rule is widened to one photo.
        A1 A valid press names an existing ask, the message the ask was sent
        as, an authorised user and one of the ask's options; anything else
        keeps the drop-and-answer-empty behaviour and never reaches the mayor.
        A2 On the first valid press, in order: (1) answer the callback once
        (key ask:<update_id>:answer); (2) in one ledger write mark the ask
        answered with the choice and the update id; (3) relay exactly one
        durable inbound entry naming the question, the option and the original
        message id, with provider message id ask-<ask-id>, and record the ask
        as relayed once GC confirms it; (4) only then attempt the edit that
        appends "\\n✓ <label>" to the text or caption and removes the buttons,
        under the per-ask key ask:<ask-id>:edit (editMessageText for a text
        ask, editMessageCaption for a photo ask). The offset advances once the
        relay is confirmed; the edit outcome never holds it, un-answers or
        relays again.
        A3 When GC does not confirm the relay the offset is retained and the
        retry relays once under the same provider id, records relayed, then
        edits; the callback is answered no second time and the edit happens
        at most once. A4 A later press on an answered and relayed ask runs
        the pending edit once before the already-answered answer (a no-op
        when the edit already ran, under the per-ask key); nothing is
        relayed again. A5 The send-time refusals (derived ask id, no shared
        callback_data, room for the choice suffix within 4096 units of text
        or 1024 units of photo caption, no ambiguous label) stay, with the
        photo caption limit for photo asks."""
        answer_key = f'ask:{update_id}:answer'
        message = query.get('message')
        mid = message.get('message_id') if isinstance(message, dict) else None
        parsed = self._ask_choice(data)
        if (type(mid) is not int or mid <= 0 or parsed is None
                or len(data.encode()) > self.CALLBACK_DATA_MAX_BYTES):
            self._log(f'callback dropped (update {update_id}): malformed')
            self._answer_callback(update_id, query['id'], key=answer_key)
            return
        ask_id, _ = parsed
        ask = self.store.get_ask(ask_id)
        if ask is None:
            self._log(f'callback dropped (update {update_id}): unknown_ask')
            self._answer_callback(update_id, query['id'], key=answer_key)
            return
        # The press must name the message the ask was sent as: a forged
        # message id would otherwise edit and relay the wrong message while
        # marking the real ask answered. A send whose receipt is still
        # ambiguous has no recorded message yet and cannot be checked.
        if ask['message_id'] is not None and mid != ask['message_id']:
            self._log(f'callback dropped (update {update_id}): message_mismatch')
            self._answer_callback(update_id, query['id'], key=answer_key)
            return
        if ask['answered']:
            if not ask.get('relayed'):
                # A3: the first press answered but GC never confirmed the
                # relay, so the offset was retained. The retry relays once
                # under the same provider id, records relayed, then edits.
                # The callback answer is saved under the same update key, so
                # it sends no second time.
                self._answer_callback(update_id, query['id'], key=answer_key)
                self._relay_ask_answer(ask_id, ask, mid, update_id)
                return
            # An answered and relayed press runs the pending edit step
            # once before the already-answered answer: after a restart
            # between the relay record and the edit, any later authorised
            # press (same or different update id) completes the edit. A
            # no-op when it already ran, under the per-ask key.
            self._edit_ask(ask_id, ask, mid, update_id)
            self._answer_callback(update_id, query['id'], text=self.ASK_ALREADY_ANSWERED, key=answer_key)
            return
        label = next((option['text'] for option in ask['options']
                      if option.get('callback_data') == data), None)
        if label is None:
            self._log(f'callback dropped (update {update_id}): unknown_choice')
            self._answer_callback(update_id, query['id'], key=answer_key)
            return
        self._answer_callback(update_id, query['id'], key=answer_key)
        if not self.store.mark_ask_answered(ask_id, label, update_id):
            # A6: a close (ask-close, the CLI process) won after the read
            # above; taps are serialised in this one poller, so only a close
            # can. Its row is decided and relayed: nothing relays, and the
            # edit step shows the decision (a saved receipt when the close's
            # edit already ran). This callback was answered empty above.
            self._edit_ask(ask_id, self.store.get_ask(ask_id), mid, update_id)
            return
        ask = self.store.get_ask(ask_id)
        self._relay_ask_answer(ask_id, ask, mid, update_id)

    def _relay_ask_answer(self, ask_id, ask, mid, update_id):
        """Relay one recorded ask answer, record it relayed, then edit once.

        The relay uses provider message id ask-<ask-id> so an update id equal
        to an earlier relayed Telegram message id can never deduplicate it.
        GC confirmation records relayed; a GC failure raises and retains the
        offset for the A3 retry. The edit is its own step (_edit_ask): it runs
        at most once under the per-ask key ask:<ask-id>:edit and never holds
        the offset or relays again. The answer is two parts: a first line
        '[ask answer | choice: <label> | message: <id>]', then a newline and
        'question: ' with the visible question verbatim as the last field,
        so nothing in a free-text question can be confused with a field."""
        question, fmt = ask['question'], ask['format']
        label = ask['answer']
        if fmt == 'html':
            visible, _ = html_visible_text(question)
        else:
            visible = question
        self._relay_to_gc(f'ask-{ask_id}',
                          f'[ask answer | choice: {label} | message: {mid}]\nquestion: {visible}')
        self.store.mark_ask_relayed(ask_id)
        self._edit_ask(ask_id, ask, mid, update_id)

    def _edit_ask(self, ask_id, ask, mid, update_id=None):
        """Run one answered ask's edit step once: append "\\n✓ <label>" (a
        tap) or "\\n✓ Decided: <decision>" (a close) to the text or caption
        and remove the buttons, under the per-ask key ask:<ask-id>:edit
        (editMessageText for a text ask, editMessageCaption for a photo ask).
        The suffix and identity come from the row, so a close's edit and a
        later tap's edit are one request. A press whose edit key already
        holds a receipt (confirmed, rejected or pending) never edits again;
        the edit outcome never holds the offset, un-answers or relays again.
        Both edits carry an empty inline keyboard. Returns the edit receipt;
        a tap (update_id set) logs an unconfirmed edit."""
        question, fmt = ask['question'], ask['format']
        label = ask['answer']
        kind = ask.get('kind') or 'text'
        prefix = self.ASK_DECIDED_PREFIX if ask.get('decided') else '\n✓ '
        if fmt == 'html':
            visible, _ = html_visible_text(question)
            edit_text = question + prefix + html_text(label)
            plain_edit = visible + prefix + label
        else:
            edit_text, plain_edit = question + prefix + label, None
        if kind == 'photo':
            edit = self._reserve_and_deliver(f'ask:{ask_id}:edit',
                                             dict(type='edit', message_id=mid, text=edit_text, format=fmt),
                                             lambda pending: self._deliver_caption_edit(
                                                 edit_text, pending, plain_edit, mid))
        else:
            edit = self._reserve_and_deliver(f'ask:{ask_id}:edit',
                                             dict(type='edit', message_id=mid, text=edit_text, format=fmt),
                                             lambda pending: self._deliver_text(
                                                 edit_text, None, pending, plain_edit,
                                                 method='editMessageText',
                                                 extra={'message_id': mid,
                                                        'reply_markup': {'inline_keyboard': []}}))
        if update_id is not None and edit.get('delivered') is not True:
            self._log(f'ask edit not confirmed (update {update_id})')
        return edit

    # The refusals of close_ask before its ledger write; any other reason it
    # returns is the edit's, after the ask was closed.
    ASK_CLOSE_REFUSALS = ('ask_not_found', 'ask_message_unknown', 'ask_already_answered', 'ask_already_decided',
                          'decision_required', 'decision_breaks_line', 'decision_exceeds_room')

    def close_ask(self, decision, message_id=None, ask_key=None):
        """A6: close one open ask with the decision the mayor made, selected
        by its sent message id or by its send's idempotency key (exactly
        one). One guarded ledger write marks it answered, decided and
        relayed (nothing goes to GC), then the edit step removes the buttons
        and appends "\\n✓ Decided: <decision>". A later tap gets only the
        already-answered answer. The same decision again is the idempotent
        repeat (the saved edit receipt; a not-sent edit sends again).
        Returns the edit receipt, or a refusal receipt before any write."""
        if (message_id is None) == (ask_key is None):
            raise ValueError('close_ask takes exactly one of message_id and ask_key')
        if message_id is not None:
            ask = self.store.get_ask_by_message(message_id)
        else:
            ask = self.store.get_ask(self._ask_id_for_key(ask_key))
        if ask is None:
            return self.receipt(reason='ask_not_found')
        if ask['message_id'] is None:
            # The ask's send never confirmed a message: nothing to edit.
            return self.receipt(reason='ask_message_unknown')
        if ask['answered']:
            return self._closed_ask(ask, decision)
        if not isinstance(decision, str) or not decision.strip():
            return self.receipt(reason='decision_required')
        if self._ask_text_breaks_answer_line(decision):
            return self.receipt(reason='decision_breaks_line')
        visible = html_visible_text(ask['question'])[0] if ask['format'] == 'html' else ask['question']
        limit = 1024 if ask.get('kind') == 'photo' else 4096
        if utf16_units(visible + self.ASK_DECIDED_PREFIX + decision) > limit:
            return self.receipt(reason='decision_exceeds_room')
        if not self.store.close_ask(ask['ask_id'], decision):
            # A tap (or another close) won between the read and the write.
            return self._closed_ask(self.store.get_ask(ask['ask_id']), decision)
        ask = self.store.get_ask(ask['ask_id'])
        return self._edit_ask(ask['ask_id'], ask, ask['message_id'])

    def _closed_ask(self, ask, decision):
        """close_ask on an ask already answered: a tap's choice refuses, another
        decision refuses, the same decision runs the edit step again (a
        saved receipt, never a second edit)."""
        if not ask['decided']:
            return self.receipt(reason='ask_already_answered', answer=ask['answer'])
        if ask['answer'] != decision:
            return self.receipt(reason='ask_already_decided', answer=ask['answer'])
        return self._edit_ask(ask['ask_id'], ask, ask['message_id'])

    def _deliver_caption_edit(self, caption, pending, plain=None, message_id=None):
        """Edit a photo ask caption; mirrors _deliver_text with editMessageCaption.

        `plain` is the visible caption for an HTML ask, retried once without
        parse_mode when Telegram rejects the markup. A 4xx rejection is final;
        an unconfirmed send returns pending and never holds the offset. The
        edit carries an empty inline keyboard so the answered ask's buttons
        are removed."""
        body = dict(chat_id=self.config['chat_id'], message_id=message_id, caption=caption,
                    reply_markup={'inline_keyboard': []})
        if plain is not None:
            body['parse_mode'] = 'HTML'
        try:
            status, data = self._send('editMessageCaption',
                                      lambda timeout: self.telegram('editMessageCaption', body, timeout))
            if self._confirmed(status, data):
                return self.receipt(True, message_id=str(data['result']['message_id']))
            if plain is not None and self._html_rejected(data):
                receipt = self._deliver_caption_edit(plain, pending, None, message_id)
                if receipt is not pending:
                    receipt['format_fallback_reason'] = 'telegram_html_parse_rejected_400'
                return receipt
            if data.get('ok') is False and 400 <= data.get('error_code', 0) < 500:
                return self.receipt(reason='telegram_rejected_not_sent_' + str(data['error_code']))
            return pending
        except RequestNotSent:
            return self.not_sent()
        except TransportError:
            return pending

    def drain(self, updates):
        for update in updates:
            uid = self._update_id(update)
            if uid < self.store.offset():
                continue
            self.inbound(update)  # errors retain offset; GC dedups provider_message_id
            self.store.advance(uid + 1)

    def receipt(self, delivered=False, reason='', failure_kind='permanent', **extra):
        result = dict(conversation=self.conversation, delivered=delivered, **extra)
        if not delivered:
            result.update(failure_kind=failure_kind, metadata={'reason': reason})
        return result

    def not_sent(self):
        """Every attempt failed before sending: definitely not delivered, and
        the same key with the same request sends again (resendable)."""
        return self.receipt(reason=NOT_SENT_REASON, failure_kind='transient')

    def _attachment_error(self, attachment, text, visible):
        if not isinstance(attachment, dict):
            return 'attachment_must_be_an_object'
        kind = attachment.get('kind')
        if kind not in self.ATTACHMENT_KINDS:
            return 'attachment_kind_unsupported'
        path = attachment.get('path')
        if not isinstance(path, str) or not path:
            return 'attachment_path_required'
        if not path.startswith('/'):
            return 'attachment_path_must_be_absolute'
        if ';' in path or ',' in path:
            # curl's --form treats ';'/',' in an @file path as the start of a type=/
            # filename= directive; reject rather than risk silent reinterpretation.
            return 'attachment_path_unsupported_characters'
        real_path = os.path.realpath(path)
        protected = getattr(self, 'protected_paths', None)
        if protected is None:
            protected = self.config.get('protected_paths', ()) or ()
        if any(real_path == os.path.realpath(candidate) for candidate in protected):
            return 'attachment_path_is_a_bridge_credential'
        state_dir = getattr(self, 'state_dir', None)
        if state_dir is not None and Path(real_path).name.startswith('telegram-bridge.sqlite3'):
            state_real = os.path.realpath(state_dir)
            if real_path == state_real or real_path.startswith(state_real + os.sep):
                return 'attachment_path_is_a_bridge_credential'
        try:
            file_path = Path(path)
            if not file_path.is_file():
                return 'attachment_file_missing_or_not_regular'
            if not os.access(file_path, os.R_OK):
                return 'attachment_file_not_readable'
            size = file_path.stat().st_size
        except (OSError, ValueError):
            return 'attachment_file_unreadable'
        if size > self.ATTACHMENT_KINDS[kind][2]:
            return 'attachment_exceeds_size_limit'
        if kind in self.CAPTIONLESS_KINDS:
            # The raw body, not the visible text: markup that renders to nothing
            # or a lone newline is still text the caller meant to send.
            return 'video_note_takes_no_caption' if text else None
        if utf16_units(visible) > 1024:
            # Telegram's media caption cap (1024 UTF-16 units) is a quarter of
            # sendMessage's 4096. Splitting one publish() into a caption send plus a
            # follow-up text send would need two receipts under one idempotency key,
            # breaking the single ambiguous-receipt reconciliation contract below.
            # Reject instead; a caller with long text sends a short caption here and
            # the full text as a separate publish() under its own idempotency key.
            return 'caption_exceeds_1024_utf16_units'
        return None

    # Telegram's 400 for markup it cannot turn into entities. Only this answer
    # earns the one plain re-send: any other 4xx is a definitive rejection that
    # plain text would not change.
    @staticmethod
    def _html_rejected(data):
        return (data.get('ok') is False and data.get('error_code') == 400
                and "can't parse entities" in str(data.get('description', '')).lower())

    def _confirmed(self, status, data):
        sent = data.get('result', {})
        return (status == 200 and data.get('ok') is True and type(sent.get('message_id')) is int
                and sent.get('chat', {}).get('id') == self.config['chat_id'])

    def _deliver_text(self, text, reply, pending, plain=None, method='sendMessage', extra=None, fallback_reason=None):
        """Send text; `plain` is set for HTML, and is the one fallback body
        sent (under the same key, same reservation) when Telegram cannot parse
        the markup. The first send was then definitively not delivered, so
        the fallback can never duplicate the message. `method` and `extra`
        (a keyboard, an edited message's id) serve the city commands; `extra`
        also carries a silent send's disable_notification, so the fallback
        is silent too."""
        body = dict(chat_id=self.config['chat_id'], text=text, link_preview_options={'is_disabled': True})
        body.update(extra or {})
        if plain is not None:
            body['parse_mode'] = 'HTML'
        if reply:
            body['reply_parameters'] = {'message_id': int(reply)}
        try:
            status, data = self._send(method, lambda timeout: self.telegram(method, body, timeout))
            if self._confirmed(status, data):
                if fallback_reason is not None:
                    return self.receipt(True, message_id=str(data['result']['message_id']),
                                        fallback_reason=fallback_reason)
                return self.receipt(True, message_id=str(data['result']['message_id']))
            if plain is not None and self._html_rejected(data):
                receipt = self._deliver_text(plain, reply, pending, method=method, extra=extra)
                if receipt is not pending:
                    receipt['format_fallback_reason'] = 'telegram_html_parse_rejected_400'
                return receipt
            if data.get('ok') is False and 400 <= data.get('error_code', 0) < 500:
                return self.receipt(reason='telegram_rejected_not_sent_' + str(data['error_code']))
            return pending
        except RequestNotSent:
            return self.not_sent()
        except TransportError:
            return pending

    def _deliver_attachment(self, attachment, text, reply, pending, plain=None, extra=None):
        """Send one attachment; `extra` carries the keyboard reply_markup for a
        photo ask and a silent send's disable_notification, which both
        fallbacks keep. The HTML parse-rejection fallback re-sends the same
        file once without markup, keeping the keyboard. A keyboard send never
        falls back to a text message: a rejected photo ask fails instead, so
        the ask is never recorded as sent without its buttons."""
        method, field, _ = self.ATTACHMENT_KINDS[attachment['kind']]
        fields = {'chat_id': str(self.config['chat_id'])}
        if attachment['kind'] not in self.CAPTIONLESS_KINDS:
            fields['caption'] = text
            if plain is not None:
                fields['parse_mode'] = 'HTML'
        if reply:
            fields['reply_parameters'] = json.dumps({'message_id': int(reply)})
        if extra and 'reply_markup' in extra:
            fields['reply_markup'] = json.dumps(extra['reply_markup'])
        if extra and extra.get('disable_notification') is True:
            fields['disable_notification'] = 'true'
        try:
            status, data = self._send(method, lambda timeout: self.telegram_multipart(
                method, fields, field, attachment['path'], timeout))
            if self._confirmed(status, data):
                return self.receipt(True, message_id=str(data['result']['message_id']))
            if plain is not None and self._html_rejected(data):
                # Same file, same caption text without markup, once, keeping
                # the keyboard for a photo ask.
                receipt = self._deliver_attachment(attachment, plain, reply, pending, extra=extra)
                if receipt is not pending:
                    receipt['format_fallback_reason'] = 'telegram_html_parse_rejected_400'
                return receipt
            if data.get('ok') is False and 400 <= data.get('error_code', 0) < 500:
                if extra and 'reply_markup' in extra:
                    # A photo ask never falls back to a text message: the
                    # buttons would be lost and a text copy would duplicate
                    # the ask. Fail instead with no delivered copy.
                    return self.receipt(reason='telegram_rejected_not_sent_' + str(data['error_code']))
                # A stuck-report must not silently vanish: fall back to a text
                # message carrying the same content plus the rejection reason.
                # The fallback is always plain: the markup is unproven here.
                reason = 'telegram_rejected_not_sent_' + str(data['error_code'])
                # `extra` holds no keyboard here, only a silent send's flag.
                return self._fallback_to_text(text if plain is None else plain, reply, reason, pending, extra=extra)
            return pending
        except RequestNotSent:
            return self.not_sent()
        except TransportError:
            return pending

    def _fallback_to_text(self, text, reply, reason, pending, extra=None, what='attachment'):
        fallback_text = self._truncate_utf16((text + '\n' if text.strip() else '')
                                             + f'[{what} rejected: {reason}]', 4096)
        return self._deliver_text(fallback_text, reply, pending, extra=extra, fallback_reason=reason)

    def _album_error(self, attachments, text, visible):
        """The first reason an album refuses, before anything is reserved or
        sent: its size, its kinds' grouping, then every file's own checks."""
        low, high = self.ALBUM_FILES
        if not isinstance(attachments, list) or not low <= len(attachments) <= high:
            return 'album_needs_2_to_10_files'
        kinds = [item.get('kind') if isinstance(item, dict) else None for item in attachments]
        if any(kind in self.ATTACHMENT_KINDS and kind not in self.ALBUM_KINDS for kind in kinds):
            return 'album_kind_not_groupable'
        if 'document' in kinds and any(kind in ('photo', 'video') for kind in kinds):
            return 'album_mixes_documents'
        for item in attachments:
            error = self._attachment_error(item, text, visible)
            if error:
                return error
        return None

    def _deliver_album(self, attachments, text, reply, pending, plain=None, extra=None):
        """Send 2-10 files as one sendMediaGroup album, the body as the first
        item's caption (none when empty). Confirmed only by one Message per
        file, each in the paired chat; the receipt names the first message
        and all of them in order. The HTML parse-rejection fallback re-sends
        the album once with the plain caption; any other 4xx falls back to
        one plain text message, like a rejected attachment. `extra` carries
        a silent send's disable_notification."""
        media, files = [], []
        for index, item in enumerate(attachments):
            entry = {'type': item['kind'], 'media': f'attach://file{index}'}
            if index == 0 and text:
                entry['caption'] = text
                if plain is not None:
                    entry['parse_mode'] = 'HTML'
            media.append(entry)
            files.append((f'file{index}', item['path']))
        fields = {'chat_id': str(self.config['chat_id']), 'media': json.dumps(media)}
        if reply:
            fields['reply_parameters'] = json.dumps({'message_id': int(reply)})
        if extra and extra.get('disable_notification') is True:
            fields['disable_notification'] = 'true'
        try:
            status, data = self._send('sendMediaGroup', lambda timeout: self.telegram_media_group(
                fields, files, timeout))
            # One Message per file, each passing _confirmed's own checks.
            sent = data.get('result')
            if (isinstance(sent, list) and len(sent) == len(files)
                    and all(isinstance(message, dict) and self._confirmed(status, dict(data, result=message))
                            for message in sent)):
                ids = [str(message['message_id']) for message in sent]
                return self.receipt(True, message_id=ids[0], message_ids=ids)
            if plain is not None and self._html_rejected(data):
                receipt = self._deliver_album(attachments, plain, reply, pending, extra=extra)
                if receipt is not pending:
                    receipt['format_fallback_reason'] = 'telegram_html_parse_rejected_400'
                return receipt
            if data.get('ok') is False and 400 <= data.get('error_code', 0) < 500:
                reason = 'telegram_rejected_not_sent_' + str(data['error_code'])
                return self._fallback_to_text(text if plain is None else plain, reply, reason, pending,
                                              extra=extra, what='album')
            return pending
        except RequestNotSent:
            return self.not_sent()
        except TransportError:
            return pending

    def publish(self, request):
        if request.get('conversation') != self.conversation:
            return self.receipt(reason='recipient_not_authorized')
        key, text = request.get('idempotency_key'), request.get('text')
        if not isinstance(key, str) or not key.strip() or len(key) > 256:
            return self.receipt(reason='stable_idempotency_key_required')
        # Plain is the default and carries no 'format' key into the
        # fingerprint, so every existing caller and ledger row is unchanged;
        # 'html' is part of the identity, so one key with another format is a
        # conflict exactly like another body.
        fmt = request.get('format', 'plain')
        if fmt == 'plain':
            request = {k: v for k, v in request.items() if k != 'format'}
        elif fmt != 'html':
            return self.receipt(reason='format_unsupported')
        # Silent likewise joins the fingerprint only when true: false is the
        # same as absent and is dropped, so a send without it is unchanged
        # and the same key with and without silent is a conflict.
        silent = request.get('silent', False)
        if silent is False:
            request = {k: v for k, v in request.items() if k != 'silent'}
        elif silent is not True:
            return self.receipt(reason='silent_must_be_boolean')
        if not isinstance(text, str):
            return self.receipt(reason='text_required_maximum_4096_utf16_units')
        plain = None
        if fmt == 'html':
            plain, error = html_visible_text(text)
            if error:
                return self.receipt(reason=error)
        visible = text if plain is None else plain
        attachment = request.get('attachment')
        # An album (2-10 files, sendMediaGroup) is the field attachments; one
        # file keeps the field attachment, so its request is unchanged.
        attachments = request.get('attachments')
        if attachment is not None and attachments is not None:
            return self.receipt(reason='attachment_and_attachments_together')
        # A captionless kind is the one send with an empty body; a body given
        # with it is refused by _attachment_error below. An album may go
        # without a caption too.
        captionless = isinstance(attachment, dict) and attachment.get('kind') in self.CAPTIONLESS_KINDS
        # Telegram counts its limits on the visible text after parsing, so an
        # HTML body is bounded by that (and its source by HTML_SOURCE_MAX_UNITS)
        # and refused whole when over, never cut inside a tag.
        if (not visible.strip() and not captionless and attachments is None) or utf16_units(visible) > 4096:
            return self.receipt(reason='text_required_maximum_4096_utf16_units')
        reply = request.get('reply_to_message_id', '')
        if reply and (not isinstance(reply, str) or not reply.isdecimal() or int(reply) <= 0):
            return self.receipt(reason='invalid_reply_message_id')
        if attachments is not None:
            # sendMediaGroup takes no reply_markup (P1: no keyboard with more
            # than one attachment).
            if 'keyboard' in request:
                return self.receipt(reason='invalid_keyboard')
            error = self._album_error(attachments, text, visible)
            if error:
                return self.receipt(reason=error)
        rows = None
        if 'keyboard' in request:
            # Absent means byte-identical behaviour to today: the field joins
            # the idempotency fingerprint like format does, so the same key
            # with another keyboard is idempotency_key_conflict.
            rows, error = self._publish_keyboard(request['keyboard'])
            if error:
                if (error == 'foreign_callback_data'
                        and isinstance(attachment, dict)
                        and attachment.get('kind') == 'photo'):
                    return self.receipt(reason='invalid_keyboard')
                return self.receipt(reason=error)
            request = dict(request, keyboard=rows)
        ask_ids = [ask_id for row in rows or () for ask_id, _ in
                   (self._ask_choice(button['callback_data']) or (None, None) for button in row)
                   if ask_id is not None]
        ask_ids = list(dict.fromkeys(ask_ids))
        # P1: a keyboard may go with no attachment or with exactly one photo;
        # any other attachment kind (or more than one) refuses the send as
        # invalid_keyboard before anything is recorded or sent. A keyboard on
        # a photo holds only ask: buttons: a handler edit of that photo would
        # go through editMessageText, which Telegram rejects for media.
        if rows is not None and attachment is not None:
            if not (isinstance(attachment, dict) and attachment.get('kind') == 'photo'):
                return self.receipt(reason='invalid_keyboard')
            if any(self._ask_choice(button['callback_data']) is None
                   for row in rows for button in row):
                return self.receipt(reason='invalid_keyboard')
        if attachment is not None:
            error = self._attachment_error(attachment, text, visible)
            if error:
                return self.receipt(reason=error)
        # Every ask button names the send's own derived id: a valid shape
        # under another key (or another ask's id) is refused before anything
        # is recorded, so a reused ask id can never silently rebind the
        # first question, options and message to a second send.
        if any(ask_id != self._ask_id_for_key(key) for ask_id in ask_ids):
            return self.receipt(reason='invalid_keyboard')
        if ask_ids:
            # The question is free text and is never refused for its
            # characters: it rides verbatim as the last field of the
            # relayed answer, after a newline, so nothing in it can be
            # confused with a field. Only the room rule below applies.
            # Answering appends '\n✓ <label>' to the question (the caption for
            # a photo ask), so a question already at the limit would make
            # every edit overlong and the ask unanswerable. Refuse while the
            # longest choice still fits nowhere, rather than send buttons no
            # tap can complete: 1024 units of caption for a photo ask, 4096
            # units of text otherwise.
            longest = max((button['text'] for row in rows for button in row
                           if (self._ask_choice(button['callback_data']) or (None,))[0] in ask_ids),
                          default='')
            limit = 1024 if attachment is not None else 4096
            if utf16_units(visible + '\n✓ ' + longest) > limit:
                return self.receipt(reason='invalid_keyboard')
        def deliver(pending):
            if rows is not None:
                # Recorded before Telegram is contacted, so a press can only
                # ever resolve against a sent ask; the sent message id follows
                # once the send is confirmed. A photo ask records kind photo
                # with its caption as the question.
                kind = 'photo' if attachment is not None else 'text'
                for ask_id in ask_ids:
                    self.store.record_ask(ask_id, text, fmt, [button for row in rows for button in row
                                                             if (self._ask_choice(button['callback_data']) or (None,))[0] == ask_id],
                                          kind)
            extra = {}
            if rows is not None:
                extra['reply_markup'] = {'inline_keyboard': rows}
            if silent:
                extra['disable_notification'] = True
            if attachments is not None:
                receipt = self._deliver_album(attachments, text, reply, pending, plain, extra=extra or None)
            elif attachment is not None:
                receipt = self._deliver_attachment(attachment, text, reply, pending, plain, extra=extra or None)
            else:
                receipt = self._deliver_text(text, reply, pending, plain, extra=extra or None)
            if rows is not None and receipt.get('delivered') and receipt.get('message_id'):
                self.store.set_ask_message(ask_ids, int(receipt['message_id']))
            return receipt
        return self._reserve_and_deliver(key, request, deliver)

    # ---- pinned messages: one bot message per slot, edited in place ----
    PIN_SLOT_RE = re.compile(r'[a-z0-9_-]{1,32}')

    @contextlib.contextmanager
    def _pin_lock(self, slot):
        """Hold <state_dir>/telegram-pin-<slot>.lock (owner-only, never the bot
        lock) for one whole update, so two callers never both send. The
        ledger lives in the state directory, so its parent is that."""
        fd = os.open(Path(self.store.path).parent / f'telegram-pin-{slot}.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the lock; the file is never unlinked

    def _pin_call(self, key, identity, deliver_fn, retry_undelivered=False):
        """_reserve_and_deliver, plus whether Telegram was contacted (False
        for a saved receipt)."""
        called = []

        def deliver(pending):
            called.append(True)
            return deliver_fn(pending)
        return self._reserve_and_deliver(key, identity, deliver, retry_undelivered), bool(called)

    def pin(self, slot, text, fmt='plain'):
        """Show `text` in the slot's one pinned message (OPERATIONS.md "Pinned
        messages"). With no live message, a new generation: send the body
        silently, exactly once under pin:<slot>:g<generation>:message, record
        its id, then pin it under pin:<slot>:g<generation>:pin. A pin not yet
        delivered (or since undone by an --unpin call whose answer was lost)
        is attempted again before anything else. The same body and
        format as last applied makes no call; another body edits the live
        message under pin:<slot>:g<generation>:r<revision>, a new revision
        each time. "message is not modified" counts as applied; "message to
        edit not found" (the user deleted it) ends the generation, and the
        next one sends and pins in this same run; so does a pin's 400
        "message to pin not found".

        Returns a receipt: delivered with message_id, revision and changed
        (False when nothing was sent), else the failing step's receipt with
        its reason and step (send, pin or edit)."""
        if not isinstance(slot, str) or not self.PIN_SLOT_RE.fullmatch(slot):
            return self.receipt(reason='pin_slot_invalid')
        if fmt not in ('plain', 'html'):
            return self.receipt(reason='format_unsupported')
        if not isinstance(text, str):
            return self.receipt(reason='text_required_maximum_4096_utf16_units')
        plain = None
        if fmt == 'html':
            plain, error = html_visible_text(text)
            if error:
                return self.receipt(reason=error)
        visible = text if plain is None else plain
        if not visible.strip() or utf16_units(visible) > 4096:
            return self.receipt(reason='text_required_maximum_4096_utf16_units')
        applied = hashlib.sha256(json.dumps(dict(text=text, format=fmt), sort_keys=True).encode()).hexdigest()
        changed = False
        ended = None  # (receipt, step) of the last deleted-message answer
        with self._pin_lock(slot):
            for _ in range(2):  # a deleted message ends at most one generation per run
                state = self.store.pin_state(slot)
                generation, mid = state['generation'], state['message_id']
                prefix = f'pin:{slot}:g{generation}'
                if mid is None:
                    # The identity is the slot's generation, not the body: a
                    # rerun after a crash finds this generation's saved
                    # receipt whatever it now shows, so one generation never
                    # sends a second message. An ambiguous send stays so.
                    sent, fresh = self._pin_call(
                        prefix + ':message', dict(type='pin_message', slot=slot, generation=generation),
                        lambda pending: self._deliver_text(text, None, pending, plain,
                                                           extra={'disable_notification': True}))
                    if sent.get('delivered') is not True:
                        if (sent.get('metadata') or {}).get('reason', '').startswith('telegram_rejected_not_sent_'):
                            # Definitely not sent: the next update starts afresh.
                            self.store.set_pin(slot, generation=generation + 1)
                        return dict(sent, step='send')
                    mid = int(sent['message_id'])
                    # A saved receipt's body is unknown here: the edit below
                    # then brings the message to this body.
                    self.store.set_pin(slot, message_id=mid, revision=0, fingerprint=applied if fresh else None)
                    state = self.store.pin_state(slot)
                    changed = changed or fresh
                pinned, called = self._pin_call(
                    prefix + ':pin', dict(type='pin', message_id=mid),
                    lambda pending: self._deliver_pin_toggle(
                        prefix + ':unpin', 'pinChatMessage',
                        dict(chat_id=self.config['chat_id'], message_id=mid, disable_notification=True), pending,
                        not_found=('message to pin not found', 'telegram_message_to_pin_not_found')),
                    retry_undelivered=True)
                changed = changed or called
                if (pinned.get('metadata') or {}).get('reason') == 'telegram_message_to_pin_not_found':
                    # The user deleted the message before its pin landed.
                    self.store.set_pin(slot, generation=generation + 1, message_id=None, revision=0,
                                       fingerprint=None)
                    ended = pinned, 'pin'
                    continue
                if pinned.get('delivered') is not True:
                    return dict(pinned, step='pin', message_id=str(mid))
                if state['fingerprint'] == applied:
                    return self.receipt(True, message_id=str(mid), revision=state['revision'], changed=changed)
                revision = state['revision'] + 1
                # Recorded before the edit and unknown until it applies, so a
                # crash or an unconfirmed edit is edited again next time.
                self.store.set_pin(slot, revision=revision, fingerprint=None)
                edit, _ = self._pin_call(prefix + f':r{revision}',
                                         dict(type='edit', message_id=mid, text=text, format=fmt),
                                         lambda pending: self._deliver_pin_edit(text, plain, mid, pending))
                if edit.get('delivered') is True:
                    self.store.set_pin(slot, fingerprint=applied)
                    return self.receipt(True, message_id=str(mid), revision=revision, changed=True)
                if (edit.get('metadata') or {}).get('reason') != 'telegram_message_to_edit_not_found':
                    return dict(edit, step='edit', message_id=str(mid))
                self.store.set_pin(slot, generation=generation + 1, message_id=None, revision=0, fingerprint=None)
                ended = edit, 'edit'
                changed = True
        # Two generations' messages went missing within this one run.
        return dict(ended[0], step=ended[1])

    def unpin(self, slot):
        """Unpin the slot's live message under pin:<slot>:g<generation>:unpin;
        the slot then has no live message and the next update is a new
        generation. With no live message, a generation whose send never
        confirmed (ambiguous, or not sent) is ended instead, so the next
        update sends a new message: the operator's reconciliation once the
        chat was checked. Returns a delivered receipt (message_id None when
        nothing was pinned, ended_generation when one was ended) or the
        unpin's receipt."""
        if not isinstance(slot, str) or not self.PIN_SLOT_RE.fullmatch(slot):
            return self.receipt(reason='pin_slot_invalid')
        with self._pin_lock(slot):
            state = self.store.pin_state(slot)
            generation, mid = state['generation'], state['message_id']
            if mid is None:
                sent = self.store.saved_receipt(f'pin:{slot}:g{generation}:message')
                if sent is not None and sent.get('delivered') is not True:
                    self.store.set_pin(slot, generation=generation + 1, revision=0, fingerprint=None)
                    return self.receipt(True, message_id=None, ended_generation=generation)
                return self.receipt(True, message_id=None)
            prefix = f'pin:{slot}:g{generation}'
            unpinned, _ = self._pin_call(
                prefix + ':unpin', dict(type='unpin', message_id=mid),
                lambda pending: self._deliver_pin_toggle(
                    prefix + ':pin', 'unpinChatMessage',
                    dict(chat_id=self.config['chat_id'], message_id=mid), pending),
                retry_undelivered=True)
            if unpinned.get('delivered') is not True:
                return dict(unpinned, step='unpin', message_id=str(mid))
            self.store.set_pin(slot, generation=generation + 1, message_id=None, revision=0, fingerprint=None)
            return self.receipt(True, message_id=str(mid))

    def _deliver_pin_toggle(self, opposite_key, method, body, pending, not_found=None):
        """A pin or an unpin of the live message. Before the call the
        generation's opposite row (the unpin's for a pin, the pin's for an
        unpin) is marked not applied: its effect is no longer known once this
        call may land, so that row is attempted again by its next request (a
        pin update re-pins after an unpin whose answer was lost, and an
        unpin after a re-pin unpins again)."""
        superseded = 'superseded_by_pin' if method == 'pinChatMessage' else 'superseded_by_unpin'
        self.store.finish(opposite_key, self.receipt(reason=superseded))
        return self._deliver_true_call(method, body, pending, not_found=not_found)

    def _deliver_pin_edit(self, text, plain, message_id, pending):
        """Edit a pinned message; mirrors _deliver_text with editMessageText,
        and reads Telegram's two 400s an in-place edit expects: "message is
        not modified" is applied, "message to edit not found" ends the
        slot's generation. `plain` is the HTML fallback body."""
        body = dict(chat_id=self.config['chat_id'], message_id=message_id, text=text,
                    link_preview_options={'is_disabled': True})
        if plain is not None:
            body['parse_mode'] = 'HTML'
        try:
            status, data = self._send('editMessageText', lambda timeout: self.telegram('editMessageText', body, timeout))
            if self._confirmed(status, data):
                return self.receipt(True, message_id=str(data['result']['message_id']))
            description = str(data.get('description', '')).lower()
            if data.get('ok') is False and data.get('error_code') == 400:
                if 'message is not modified' in description:
                    return self.receipt(True, message_id=str(message_id), not_modified=True)
                if 'message to edit not found' in description:
                    return self.receipt(reason='telegram_message_to_edit_not_found')
            if plain is not None and self._html_rejected(data):
                receipt = self._deliver_pin_edit(plain, None, message_id, pending)
                if receipt is not pending:
                    receipt['format_fallback_reason'] = 'telegram_html_parse_rejected_400'
                return receipt
            if data.get('ok') is False and 400 <= data.get('error_code', 0) < 500:
                return self.receipt(reason='telegram_rejected_not_sent_' + str(data['error_code']))
            return pending
        except RequestNotSent:
            return self.not_sent()
        except TransportError:
            return pending

    # ---- topics of the paired private chat (OPERATIONS.md, "Topics") ----
    _THREAD_ID_RE = re.compile(r'[1-9][0-9]{0,18}')
    TOPIC_USERS_WARNING = 'WARN users may create and delete topics in this chat (BotFather setting)'

    def topic_conversation(self, thread_id):
        """The GC conversation of one topic: a thread whose parent is the
        paired chat. GC compares all six fields, so it is built the same way
        every time."""
        chat = str(self.config['chat_id'])
        return dict(scope_id=self.city, provider='telegram', account_id=self.account,
                    conversation_id=f'{chat}:topic:{thread_id}', parent_conversation_id=chat, kind='thread')

    def _recorded_topics(self):
        """The settings' topics that have a ledger row, in file order, each
        {name, agent, thread_id, conversation}. The ledger is read on every
        call: the CLI may record a topic while the daemon runs."""
        if not self.topic_settings or self.store is None:
            return []
        rows = {row['name']: row['thread_id'] for row in self.store.topic_rows()}
        return [dict(topic, thread_id=rows[topic['name']], conversation=self.topic_conversation(rows[topic['name']]))
                for topic in self.topic_settings if topic['name'] in rows]

    def topic_by_thread(self, thread_id):
        """The configured, recorded topic with this thread id, or None."""
        return next((topic for topic in self._recorded_topics() if topic['thread_id'] == thread_id), None)

    def topic_by_name(self, name):
        """The configured, recorded topic with this name, or None."""
        return next((topic for topic in self._recorded_topics() if topic['name'] == name), None)

    def conversation_for_id(self, conversation_id):
        """The ref of a conversation id this bridge serves: the paired chat's
        own ref for <chat_id>, a configured, recorded topic's ref for
        <chat_id>:topic:<thread_id>, else None."""
        chat = str(self.config['chat_id'])
        if conversation_id == chat:
            return self.conversation
        prefix = chat + ':topic:'
        if isinstance(conversation_id, str) and conversation_id.startswith(prefix):
            thread = conversation_id[len(prefix):]
            if self._THREAD_ID_RE.fullmatch(thread):
                topic = self.topic_by_thread(int(thread))
                return topic['conversation'] if topic else None
        return None

    @contextlib.contextmanager
    def _topics_lock(self):
        """Hold <state_dir>/telegram-topics.lock (owner-only, never the bot
        lock) for one whole `topics` run, so two runs never both create."""
        fd = os.open(Path(self.store.path).parent / 'telegram-topics.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the lock; the file is never unlinked

    def _topic_key(self, name):
        return f'topic:{self.store.topic_attempt(name)}:{name}'

    @staticmethod
    def _topic_created(receipt):
        """The thread id a delivered create receipt carries, else None."""
        if isinstance(receipt, dict) and receipt.get('delivered') is True:
            thread_id = receipt.get('message_thread_id')
            if type(thread_id) is int and thread_id > 0:
                return thread_id
        return None

    def _topic_mode(self):
        """getMe before any create: True when users may create topics too.
        Raises when topic mode is off, so nothing is created."""
        status, data = self.telegram('getMe', {})
        bot = data.get('result')
        if status != 200 or data.get('ok') is not True or not isinstance(bot, dict):
            raise TransportError(f'Telegram getMe failed (HTTP {status}); no topic created')
        if bot.get('id') != self.config['bot_id'] or bot.get('is_bot') is not True:
            raise TransportError('Telegram bot identity mismatch; no topic created')
        if bot.get('has_topics_enabled') is not True:
            raise TransportError('topic mode is off for this bot; switch it on with BotFather')
        return bot.get('allows_users_to_create_topics') is True

    def create_topic(self, name, attempt):
        """Create one topic in the paired chat, exactly once under
        topic:<attempt>:<name>; its ledger row is written in the same
        deliver step. Returns the receipt (message_thread_id when delivered)."""
        identity = dict(type='create_topic', name=name, attempt=attempt)
        return self._reserve_and_deliver(f'topic:{attempt}:{name}', identity,
                                         lambda pending: self._deliver_create_topic(name, pending))

    def _deliver_create_topic(self, name, pending):
        """createForumTopic: confirmed only by HTTP 200 with a ForumTopic of
        this name and a thread id > 0; rejected on a 4xx, not sent when no
        byte left, else the pending (ambiguous) receipt."""
        body = dict(chat_id=self.config['chat_id'], name=name)
        try:
            status, data = self._send('createForumTopic', lambda timeout: self.telegram('createForumTopic', body,
                                                                                           timeout))
        except RequestNotSent:
            return self.not_sent()
        except TransportError:
            return pending
        topic = data.get('result')
        thread_id = topic.get('message_thread_id') if isinstance(topic, dict) else None
        if (status == 200 and data.get('ok') is True and type(thread_id) is int and thread_id > 0
                and topic.get('name') == name):
            self.store.record_topic(name, thread_id, 'created')
            return self.receipt(True, message_thread_id=thread_id)
        if data.get('ok') is False and 400 <= data.get('error_code', 0) < 500:
            return self.receipt(reason='telegram_rejected_not_sent_' + str(data['error_code']))
        return pending

    def topics(self, record=None, thread_id=None, retry=None):
        """Create each settings topic without a ledger row once, in file order
        (OPERATIONS.md, "Topics"), under the topics lock; never getUpdates, so
        safe while the unit polls. `record` (with `thread_id`) records a
        topic created by hand and makes no Telegram call; `retry` raises a
        topic's attempt after an ambiguous or rejected create and creates it
        again. A refusal raises TransportError before any Telegram call.
        Returns (lines, complete): the report, and whether every settings
        topic has a thread id."""
        names = [topic['name'] for topic in self.topic_settings]
        with self._topics_lock():
            done = {}  # name -> how this run gave it its row
            rows = {row['name']: row for row in self.store.topic_rows()}
            # A confirmed create whose row is missing (a crash between the
            # two) gets its row from the receipt; nothing is sent.
            for name in names:
                found = None if name in rows else self._topic_created(self.store.saved_receipt(self._topic_key(name)))
                if found and self.store.record_topic(name, found, 'created') is None:
                    done[name] = 'created'
            rows = {row['name']: row for row in self.store.topic_rows()}
            for flag, name in (('--record', record), ('--retry', retry)):
                if name is None:
                    continue
                if name not in names:
                    raise TransportError(f'topics {flag}: no [[topic]] named {name!r} in the settings')
                if name in rows:
                    raise TransportError(f'topics {flag}: topic {name} already has thread {rows[name]["thread_id"]}')
            if record is not None:
                if self.store.record_topic(record, thread_id, 'recorded') is not None:
                    holder = next((row['name'] for row in rows.values() if row['thread_id'] == thread_id), '?')
                    raise TransportError(f'topics --record: thread {thread_id} is already recorded for topic {holder}')
                done[record] = 'recorded by hand'
                rows = {row['name']: row for row in self.store.topic_rows()}
            if retry is not None:
                saved = self.store.saved_receipt(self._topic_key(retry))
                if saved is None:
                    raise TransportError(f'topics --retry: topic {retry} has no create to retry; '
                                         'run topics without --retry')
                if resendable(saved):
                    raise TransportError(f'topics --retry: the create of topic {retry} was not sent; '
                                         'send again: run topics without --retry')
                if saved.get('delivered') is True:
                    raise TransportError(f'topics --retry: topic {retry} was created; run topics without --retry')
            # A plain run creates every topic without a row whose create was
            # never tried or was not sent; --record makes no Telegram call.
            create = []
            if record is None:
                for name in names:
                    saved = self.store.saved_receipt(self._topic_key(name))
                    if name not in rows and (name == retry or saved is None or resendable(saved)):
                        create.append(name)
            lines = []
            if create and self._topic_mode():
                lines.append(self.TOPIC_USERS_WARNING)
            if retry is not None:
                self.store.raise_topic_attempt(retry)
            unreachable = False
            for name in create:
                if unreachable:
                    continue
                receipt = self.create_topic(name, self.store.topic_attempt(name))
                if receipt.get('delivered') is True:
                    done[name] = 'created'
                unreachable = resendable(receipt)
            rows = {row['name']: row for row in self.store.topic_rows()}
            for topic in self.topic_settings:
                name = topic['name']
                if name in rows:
                    thread = rows[name]['thread_id']
                    ref = self.topic_conversation(thread)
                    lines.append(f"topic {name}: thread {thread} ({done.get(name, 'recorded earlier')})")
                    lines.append(f"bind: gc extmsg bind --provider telegram --account-id {self.account} "
                                 f"--conversation-id {ref['conversation_id']} "
                                 f"--parent-conversation-id {ref['parent_conversation_id']} "
                                 f"--kind thread --agent {topic['agent']} --json")
                else:
                    reason, step = self._topic_not_created(name, unreachable)
                    lines.append(f'topic {name}: not created ({reason})' + (f'; {step}' if step else ''))
            if not names:
                lines.append('no [[topic]] tables in the settings')
            for name, row in rows.items():
                if name not in names:
                    lines.append(f"topic {name}: thread {row['thread_id']} "
                                 '(not in settings; its messages reach the agent as not configured)')
            return lines, all(name in rows for name in names)

    def _topic_not_created(self, name, unreachable):
        """Why a settings topic has no row, and what the operator does next:
        (reason, next step or None)."""
        saved = self.store.saved_receipt(self._topic_key(name))
        command, quoted = 'gc telegram topics', shlex.quote(name)
        if saved is None:
            if unreachable:
                return 'not attempted: Telegram could not be reached', f'run {command} again'
            return 'not attempted', f'run {command} to create it'
        found = self._topic_created(saved)
        if found:
            return f'created as thread {found}, which the ledger holds for another topic', None
        reason = (saved.get('metadata') or {}).get('reason') or 'delivery not confirmed'
        if resendable(saved):
            return reason, f'run {command} again to send it'
        if reason.startswith('telegram_rejected_not_sent_'):
            return reason, f'fix the cause, then run {command} --retry {quoted}'
        return reason, (f'check the chat: if topic {name} is there, write in it, take the thread id from the '
                        f'"[topic: thread <id> | not configured" line the agent receives and run {command} '
                        f'--record {quoted} --thread-id <id>; if it is not, run {command} --retry {quoted}')

    # Poll heartbeat for a city's health check: proof that getUpdates is
    # still succeeding, written at most once a minute. It judges health
    # by its age and pid, so a failed write only costs one stderr line.
    HEARTBEAT_INTERVAL = 60

    def heartbeat_path(self):
        return Path(self.store.path).parent / 'telegram-bridge-heartbeat.json'

    def record_poll_ok(self, monotonic=time.monotonic, now=None):
        tick = monotonic()
        if self.last_heartbeat is not None and tick - self.last_heartbeat < self.HEARTBEAT_INTERVAL:
            return
        self.last_heartbeat = tick
        when = now or datetime.datetime.now(datetime.timezone.utc)
        try:
            self._write_heartbeat(self.heartbeat_path(), json.dumps(dict(last_poll_ok_at=when.strftime('%Y-%m-%dT%H:%M:%SZ'), pid=os.getpid())))
        except (OSError, TransportError):
            print('poll heartbeat not saved', file=sys.stderr, flush=True)

    @staticmethod
    def _write_heartbeat(path, text):
        """The heartbeat's one writer, the same fence as _pending_write:
        the runtime directory is pinned by an
        O_NOFOLLOW descriptor; the temp file has an unpredictable name created
        with O_EXCL|O_NOFOLLOW in it, is written whole and fsynced, then
        renamed over the heartbeat there and the directory fsynced, so neither
        a planted temp link nor a swapped directory can send the write outside
        the runtime. The bridge never reads the heartbeat back.

        On Linux and WSL the runtime must be its own canonical path (no
        symlink anywhere in it). On darwin the same rule holds except for
        the system's own leading aliases in DARWIN_SYSTEM_ALIASES (/var,
        /tmp or /etc mapped to /private/...), which is how the temporary
        directory is reached there; any other symlinked ancestor, and a
        runtime directory that is itself a link, are refused. The pinned
        descriptor keeps every later step in the directory that was
        opened."""
        runtime = path.parent
        if voice_transcribe.host_platform() == 'darwin':
            if runtime.is_symlink():
                raise TransportError('heartbeat directory is not its own canonical path')
            resolved = os.path.realpath(runtime)
            if resolved != str(runtime):
                runtime_str = str(runtime)
                tolerated = any(
                    (runtime_str == alias or runtime_str.startswith(alias + '/'))
                    and resolved == target + runtime_str[len(alias):]
                    for alias, target in DARWIN_SYSTEM_ALIASES.items())
                if not tolerated:
                    raise TransportError('heartbeat directory is not its own canonical path')
        elif os.path.realpath(runtime) != str(runtime):
            raise TransportError('heartbeat directory is not its own canonical path')
        dfd = Bridge._open_pinned_dir(runtime, 'heartbeat directory')
        try:
            def write(fd):
                data = memoryview(text.encode())
                while data:
                    data = data[os.write(fd, data):]
            Bridge._atomic_write_pinned(dfd, path.name, 'heartbeat', write, fsync_dir=True)
        finally:
            os.close(dfd)

    @staticmethod
    def _truncate_utf16(text, limit):
        """Cut text to at most `limit` UTF-16 code units (Telegram's message cap)."""
        encoded = text.encode('utf-16-le')
        if len(encoded) <= limit * 2:
            return text
        ellipsis = '…'
        budget = max(limit - len(ellipsis), 0) * 2
        return encoded[:budget].decode('utf-16-le', 'ignore') + ellipsis

    def mayor_quota_episode(self, now=None):
        """Return (episode_key, until_ms) when a fresh health report (the
        settings' health_file, rewritten by the city every few minutes)
        records the served agent's session (the settings' agent, default mayor)
        as quota_wait until a future time, else None.

        Read-only and fail-closed: no health_file setting, a missing/malformed health
        file (not written yet, or mid-rewrite), a missing, unparseable or stale
        observed_at, or a mayor entry lacking the episode field or a finite,
        still-future until all degrade to "no episode" rather than raising or
        blocking relay.
        """
        if not self.config.get('health_file'):
            return None
        try:
            health = json.loads(Path(self.config['health_file']).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(health, dict) or not isinstance(health.get('sessions'), list):
            return None
        now = now or datetime.datetime.now(datetime.timezone.utc)
        observed_at = health.get('observed_at')
        if not isinstance(observed_at, str):
            return None
        try:
            observed = datetime.datetime.fromisoformat(observed_at.replace('Z', '+00:00'))
        except ValueError:
            return None
        if (observed.tzinfo is None
                or not now - self.HEALTH_MAX_AGE <= observed <= now + self.HEALTH_CLOCK_SKEW):
            return None
        for session in health['sessions']:
            if not isinstance(session, dict) or session.get('template') != self.agent:
                continue
            if session.get('status') != 'quota_wait':
                return None
            episode, until = session.get('episode'), session.get('until')
            if (not isinstance(episode, str) or not episode
                    or type(until) not in (int, float) or not math.isfinite(until)
                    or until <= now.timestamp() * 1000):
                return None
            return episode, until
        return None

    def format_quota_exhausted_reply(self, until_ms):
        """Render the mayor's quota-exhaustion notice. Never raises: a bad
        timestamp degrades to 'unknown time' rather than blocking the reply."""
        try:
            dt = datetime.datetime.fromtimestamp(until_ms / 1000, tz=datetime.timezone.utc).astimezone(self.local_tz)
            reset_str = dt.strftime('%H:%M ') + utc_offset_label(dt)
        except (TypeError, ValueError, OSError, OverflowError):
            reset_str = 'unknown time'
        return f'<b>bot:</b> quota exhausted, resets at {html_text(reset_str)}'

    def notify_quota_exhausted(self):
        """Tell the user once per quota-exhaustion episode that the mayor cannot
        answer right now, instead of leaving the bridge silently unresponsive.

        The idempotency key is derived from the episode alone (not the
        triggering message), so every inbound message during the same episode
        publishes the identical request and the store's dedup delivers it
        exactly once; a new episode (new reset time) gets its own key. Never
        raises: this runs inline in inbound() and must not break message relay.
        """
        try:
            found = self.mayor_quota_episode()
            if not found:
                return
            episode, until = found
            request = dict(conversation=self.conversation, idempotency_key=f'quota-exhausted-{episode}',
                           text=self.format_quota_exhausted_reply(until), format='html')
            self.publish(request)
        except Exception:
            print('quota-exhaustion notice failed', file=sys.stderr, flush=True)

    def run(self):
        if not self.probe_with_retry():
            return
        server = http.server.ThreadingHTTPServer(('127.0.0.1', self.port), _callback_handler(self))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        registered = False
        try:
            self.register()
            registered = True
            print('Telegram bridge ready; private allowlist enforced', flush=True)
            if self.typing_seconds:
                self._start_typing()
            last_registration, failures = time.monotonic(), 0
            while not self.stop.is_set():
                try:
                    if time.monotonic() - last_registration >= 30:
                        self.register()
                        last_registration = time.monotonic()
                    status, data = self.telegram('getUpdates', dict(offset=self.store.offset(), timeout=20,
                                                                   allowed_updates=['message', 'callback_query']),
                                                 timeout=27)
                    if status == 409 or data.get('error_code') == 409:
                        raise PollConflict('another Telegram poller or webhook owns this bot; stop it before restarting')
                    if status != 200 or data.get('ok') is not True or not isinstance(data.get('result'), list):
                        raise TransportError('Telegram poll rejected')
                    self.record_poll_ok()
                    self.drain(data['result'])
                    failures = 0
                except PollConflict:
                    raise
                except TransportError:
                    failures += 1
                    print('Transport unavailable; inbound cursor retained', file=sys.stderr, flush=True)
                    if failures >= 10:
                        raise TransportError('transport failed repeatedly; restarting preserves cursor') from None
                    self.stop.wait(min(failures * 2, 10))
        finally:
            server.shutdown()
            server.server_close()
            if registered:
                try:
                    self.gc('adapters', dict(provider='telegram', account_id=self.account), method='DELETE')
                except TransportError:
                    pass


class _ConflictExitStatusAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace.conflict_exit_status_given = True


def _conflict_exit_status(value):
    try:
        status = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('--conflict-exit-status must be an integer from 0 to 255') from None
    if not 0 <= status <= 255:
        raise argparse.ArgumentTypeError('--conflict-exit-status must be an integer from 0 to 255')
    return status


def _message_id_arg(value):
    if not re.fullmatch(r'[1-9][0-9]{0,18}', value or '') or int(value) > 2 ** 63 - 1:
        raise argparse.ArgumentTypeError('--message-id must be a positive integer')
    return int(value)


def _thread_id_arg(value):
    if not re.fullmatch(r'[1-9][0-9]{0,18}', value or '') or int(value) > 2 ** 63 - 1:
        raise argparse.ArgumentTypeError('--thread-id must be a positive integer')
    return int(value)


def _build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path,
                        help=f'the city\'s settings file (default: ${SETTINGS_ENV}, else '
                             f'~/.config/<workspace_name>/{SETTINGS_NAME} from <city-root>/.gc/site.toml)')
    parser.add_argument('--city-root', type=Path, default=_default_city_root(),
                        help='only used to find the default settings file')
    parser.add_argument('--port', type=int, help='overrides the settings\' port')
    parser.add_argument('command', choices=('run', 'probe', 'send', 'reply-current', 'ask-close', 'react', 'pin',
                                            'topics', 'set-commands', 'render-unit', 'check-install'))
    parser.add_argument('--body-file', type=Path,
                        help='file holding the message text (send, reply-current)')
    parser.add_argument('--idempotency-key',
                        help='one stable key per logical message: a retry reuses it with the same body; a new message takes a new key')
    parser.add_argument('--session', default=os.environ.get('GC_SESSION_ID') or None,
                        help="the sending session (default: $GC_SESSION_ID, else the settings' agent)")
    parser.add_argument('--conversation-id',
                        help="optional; must equal the settings' bot.chat_id")
    parser.add_argument('--attachment', type=Path, action='append',
                        help='absolute path of a file to send, the body as its caption; '
                             'given 2-10 times, the files go as one album captioned by the body')
    parser.add_argument('--attachment-kind', choices=tuple(Bridge.ATTACHMENT_KINDS),
                        help='voice and video_note are never inferred; video_note takes an empty body')
    parser.add_argument('--keyboard-file', type=Path,
                        help='file holding an inline keyboard as JSON rows (send, reply-current; '
                             'a keyboard send publishes directly in this process, never through GC)')
    parser.add_argument('--format', choices=('plain', 'html'), default='plain',
                        help='html sends Telegram HTML (allowlisted tags, caller-escaped text)')
    parser.add_argument('--silent', action='store_true',
                        help='send, reply-current: notify with no sound '
                             '(publishes directly in this process, never through GC)')
    parser.add_argument('--message-id', type=_message_id_arg,
                        help='ask-close: the message ID the ask was sent as; react: the message to '
                             'react to (default: the last accepted message)')
    parser.add_argument('--ask-key',
                        help="ask-close: the ask send's --idempotency-key, instead of --message-id")
    parser.add_argument('--decision',
                        help='ask-close: the decision made, one line, shown under the ask in place of its buttons')
    parser.add_argument('--reaction',
                        help='react: seen or done (the settings\' emoji), none to clear, or one emoji')
    parser.add_argument('--slot',
                        help='pin: the pinned message slot, [a-z0-9_-]{1,32}, updated to the text of --body-file')
    parser.add_argument('--unpin', action='store_true',
                        help="pin: unpin the slot's message instead of updating it")
    parser.add_argument('--record', metavar='NAME',
                        help='topics: record the thread id of a topic that exists in the chat, with --thread-id, '
                             'instead of creating it')
    parser.add_argument('--thread-id', type=_thread_id_arg,
                        help='topics --record: the thread id of the topic')
    parser.add_argument('--retry', metavar='NAME',
                        help='topics: create a topic again after an unconfirmed or rejected create')
    parser.add_argument('--gpu-probe', action='store_true',
                        help='check-install: also transcribe a clip on the GPU inside the bridge unit\'s sandbox')
    parser.add_argument('--clip', type=Path,
                        help='the --gpu-probe clip (default: samples/jfk.wav of the whisper.cpp checkout)')
    parser.add_argument('--conflict-exit-status', type=_conflict_exit_status, default=2,
                        action=_ConflictExitStatusAction, metavar='N',
                        help='run: exit status for a polling conflict (0-255, default 2)')
    parser.set_defaults(conflict_exit_status_given=False)
    return parser


def _validate_attachment_flags(parser, args):
    if args.attachment_kind and not args.attachment:
        parser.error('--attachment-kind requires --attachment')
    if args.gpu_probe and args.command != 'check-install':
        parser.error('--gpu-probe is a check-install flag')
    if args.clip and not args.gpu_probe:
        parser.error('--clip requires --gpu-probe')
    if args.reaction is not None and args.command != 'react':
        parser.error('--reaction is a react flag')
    if args.message_id is not None and args.command not in ('react', 'ask-close'):
        parser.error('--message-id is a react and ask-close flag')
    if getattr(args, 'conflict_exit_status_given', False) and args.command != 'run':
        parser.error('--conflict-exit-status is a run flag')
    selectors = [flag for flag, value in (('--message-id', args.message_id), ('--ask-key', args.ask_key))
                 if value is not None]
    if args.command != 'ask-close':
        if args.ask_key is not None or args.decision is not None:
            parser.error('--ask-key and --decision are ask-close flags')
    elif len(selectors) != 1:
        parser.error('ask-close requires exactly one of --message-id and --ask-key')
    elif args.decision is None:
        parser.error('ask-close requires --decision')
    if args.command != 'pin':
        if args.slot is not None or args.unpin:
            parser.error('--slot and --unpin are pin flags')
    elif args.slot is None or not Bridge.PIN_SLOT_RE.fullmatch(args.slot):
        parser.error('pin requires --slot matching [a-z0-9_-]{1,32}')
    elif args.unpin == (args.body_file is not None):
        parser.error('pin requires exactly one of --body-file and --unpin')
    elif args.unpin and args.format != 'plain':
        parser.error('--format is a --body-file flag')
    if args.command != 'topics':
        if args.record is not None or args.thread_id is not None or args.retry is not None:
            parser.error('--record, --thread-id and --retry are topics flags')
    elif args.record is not None and args.retry is not None:
        parser.error('topics takes at most one of --record and --retry')
    elif (args.record is None) != (args.thread_id is None):
        parser.error('topics --record requires --thread-id, and --thread-id requires --record')


def _launchd_label(unit):
    return unit[:-len('.service')] if unit.endswith('.service') else unit


def bridge_unit_state(unit, platform=None, run=subprocess.run):
    """The bridge service's state, or None when it cannot be asked: systemd's
    ActiveState on Linux/WSL, the launchd agent's state on macOS with
    'running' reported as 'active' and a missing agent as 'not loaded'."""
    plat = voice_transcribe.host_platform() if platform is None else platform
    if plat == 'darwin':
        label = _launchd_label(unit)
        try:
            result = run(['launchctl', 'print', f'gui/{os.getuid()}/{label}'],
                         capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return 'not loaded'
        for line in (result.stdout or '').splitlines():
            match = re.match(r'^\s*state = (.+)$', line)
            if match:
                value = match.group(1).strip()
                return 'active' if value == 'running' else value
        return None
    try:
        result = run(['systemctl', '--user', 'is-active', unit],
                     capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _relay_outbound(bridge, outbound, unit_state=bridge_unit_state, platform=None, home=None):
    try:
        return bridge.gc('outbound', outbound, timeout=GC_OUTBOUND_TIMEOUT)
    except TransportError:
        # The relay callback lives in the bridge process; with the unit down GC
        # can only answer "rejected", which reads like a config problem.
        state = unit_state(bridge.unit) if bridge.unit else None
        if state is not None and state != 'active':
            plat = voice_transcribe.host_platform() if platform is None else platform
            if plat == 'darwin':
                label = _launchd_label(bridge.unit)
                uid = os.getuid()
                if state == 'not loaded':
                    plist = (Path(home) if home else Path.home()) / 'Library' / 'LaunchAgents' / f'{label}.plist'
                    raise TransportError(
                        f'bridge service not running (not loaded); run: launchctl bootstrap gui/{uid} {plist}') from None
                raise TransportError(
                    f'bridge service not running ({state}); run: launchctl kickstart -k gui/{uid}/{label}') from None
            raise TransportError(f'bridge service not running ({state}); run: systemctl --user restart {bridge.unit}') from None
        raise


def _print_gc_receipt(delivered, failure_kind, reason):
    """One machine-readable line on stderr for every non-delivered receipt, so a caller can act on the bridge's own reason instead of the human-readable text, and tell an idempotency-key replay from a real failure."""
    print('GC_RECEIPT ' + json.dumps({'delivered': delivered, 'failure_kind': failure_kind, 'reason': reason}),
          file=sys.stderr, flush=True)


NOT_SENT_MESSAGE = ('Telegram could not be reached; nothing was sent '
                    f'({NOT_SENT_REASON}): send again with the same key and body')


def _read_keyboard_file(path):
    """The inline keyboard rows of a --keyboard-file: parsed before Telegram
    is contacted, so a missing file or malformed JSON refuses the send."""
    try:
        raw = Path(path).read_text()
    except OSError:
        raise TransportError('keyboard file is missing or unreadable') from None
    try:
        return json.loads(raw)
    except ValueError:
        raise TransportError('keyboard file is not valid JSON') from None


def _dispatch_outbound(bridge, args, text):
    keyboard = None
    if getattr(args, 'keyboard_file', None) is not None:
        keyboard = _read_keyboard_file(args.keyboard_file)
        if keyboard is None:
            # A JSON null is valid JSON but not rows: refusing beats falling
            # through to GC's text-only relay and reporting buttons sent.
            raise TransportError('keyboard file must hold JSON rows')
    silent = getattr(args, 'silent', False)
    if args.attachment or args.format == 'html' or keyboard is not None or silent:
        # GC's outbound relay accepts only session, conversation, text, key and
        # reply (HTTP 422 for any other property, e.g. 'body.attachment'), so
        # its callback can never hand publish() an attachment, a format, a
        # keyboard or silent. Deliver directly in this process instead, sharing
        # the daemon's idempotency store; GC keeps no transcript record of this send.
        request = dict(conversation=bridge.conversation, idempotency_key=args.idempotency_key, text=text)
        if args.format == 'html':
            request['format'] = 'html'
        if silent:
            request['silent'] = True
        if keyboard is not None:
            request['keyboard'] = keyboard
        if args.attachment:
            # --attachment-kind applies to every file; otherwise each is inferred.
            files = [dict(path=str(path.resolve()), kind=args.attachment_kind or infer_attachment_kind(path))
                     for path in args.attachment]
            if len(files) == 1:
                request['attachment'] = files[0]
            else:
                request['attachments'] = files
        receipt = bridge.publish(request)
        delivered = receipt.get('delivered') is True
        failure_kind = receipt.get('failure_kind', 'permanent')
        reason = (receipt.get('metadata') or {}).get('reason') or 'delivery not confirmed'
        message_id = receipt.get('message_id', '')
        direct = True
    else:
        outbound = dict(session_id=args.session, conversation=bridge.conversation, text=text,
                        idempotency_key=args.idempotency_key)
        result = _relay_outbound(bridge, outbound)
        receipt = result.get('Receipt', {})
        delivered = receipt.get('Delivered') is True
        failure_kind = receipt.get('FailureKind', 'permanent')
        reason = (receipt.get('Metadata') or {}).get('reason') or 'delivery not confirmed'
        message_id = receipt.get('MessageID', '')
        direct = False
    if not delivered:
        _print_gc_receipt(False, failure_kind, reason)
        if reason == NOT_SENT_REASON:
            raise TransportError(NOT_SENT_MESSAGE)
        if direct:
            raise TransportError('Telegram delivery not confirmed: ' + reason)
        raise TransportError('GC did not confirm delivery; inspect receipt before trying a new key')
    return message_id


def _react(bridge, args):
    """Set the --reaction on --message-id, else on the last accepted message,
    directly from this process: GC's outbound relay cannot carry a reaction.
    Each call has its own ledger key, so an earlier ambiguous one never
    blocks it. Returns the message id."""
    if args.reaction in ('seen', 'done'):
        emoji = bridge.reactions[args.reaction]
    elif args.reaction == 'none':
        emoji = None
    elif valid_reaction_emoji(args.reaction):
        emoji = args.reaction
    else:
        raise TransportError('--reaction must be seen, done, none or one emoji')
    message_id = args.message_id if args.message_id is not None else bridge.store.last_accepted()
    if message_id is None:
        raise TransportError('no accepted message to react to')
    key = f'reaction:{message_id}:{time.time_ns()}-{secrets.token_hex(4)}'
    receipt = bridge.set_reaction(message_id, emoji, key)
    if not receipt.get('delivered'):
        reason = receipt['metadata']['reason']
        _print_gc_receipt(False, receipt.get('failure_kind', 'permanent'), reason)
        if reason == NOT_SENT_REASON:
            raise TransportError(NOT_SENT_MESSAGE)
        raise TransportError('Telegram reaction not set: ' + reason)
    return message_id


# ---- per-city settings ------------------------------------------------------
# Everything that differs between two cities running this bridge comes from
# one owner-only TOML file outside the repository (settings.example.toml in
# the pack): ids, credential paths, state directory, port, menu, transcriber.
# Unknown keys are refused, so a typo can never silently fall back to a
# default.

SETTINGS_MAX_BYTES = 64 * 1024
_SAFE_PATH_RE = re.compile(r'/[A-Za-z0-9/._@+-]*')
_UNIT_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9@._-]{0,120}\.service')
_COMMAND_RE = re.compile(r'[a-z0-9_]{1,32}')
_MODEL_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}')
SETTINGS_KEYS = {'city', 'state_dir', 'port', 'python', 'token_file', 'callback_secret_file', 'lock_dir', 'unit',
                 'user_display_name', 'timezone', 'gc_base_url', 'gc_api_token_file', 'health_file',
                 'pairing_update_id', 'bot', 'menu', 'transcribe', 'agent', 'command',
                 'handler_write_paths', 'reactions', 'typing', 'topic'}
BOT_KEYS = {'bot_id', 'user_id', 'chat_id', 'account_id'}
COMMAND_KEYS = {'name', 'description', 'handler', 'url', 'menu', 'callback_prefix', 'timeout'}
# Commands the bridge itself owns: /start is never relayed.
RESERVED_COMMANDS = ('start',)
# Telegram's limit on one chat's command menu.
MENU_MAX = 100
# A city command's handler URL: plain http to 127.0.0.1 or [::1], an explicit
# port and a path; nothing else is ever contacted.
_HANDLER_URL_RE = re.compile(r'http://(127\.0\.0\.1|\[::1\]):([0-9]{1,5})(/[A-Za-z0-9._~/-]{0,200})')
# A callback prefix: printable ASCII without spaces, at most callback_data's 64 bytes.
_CALLBACK_PREFIX_RE = re.compile(r'[\x21-\x7e]{1,64}')
HANDLER_WRITE_PATHS_MAX = 256
# A topic of the paired private chat (OPERATIONS.md, "Topics"): its name is
# createForumTopic's, 1-128 characters; [, ] and | would break the relay's
# topic line.
TOPIC_KEYS = {'name', 'agent'}
TOPIC_MAX = 50
TOPIC_NAME_MAX = 128
TOPIC_NAME_FORBIDDEN = '[]|'
TRANSCRIBE_KEYS = {'enabled', 'whisper_cli', 'models_dir', 'model', 'vad_model', 'threads', 'cpus', 'timeout',
                   'timeout_per_minute', 'gpu', 'gpu_min_free_mb', 'method', 'gpu_check', 'language'}
TRANSCRIBE_METHODS = ('auto', 'cpu', 'gpu')
_LANGUAGE_RE = voice_transcribe.LANGUAGE_RE
# Neutral defaults: the whisper.cpp build and models every city on the host
# shares (never under a city), the large turbo model with Silero VAD, every
# CPU the bridge has, a run limit of 120 s plus 120 s per minute of audio, and
# the CPU only (the legacy gpu = true means method = "auto").
TRANSCRIBE_DEFAULTS = dict(enabled=False, whisper_cli='~/work/whisper.cpp/build/bin/whisper-cli',
                           models_dir='~/work/whisper.cpp/models', model='large-v3-turbo-q5_0',
                           vad_model='silero-v6.2.0', threads=4, cpus='', timeout=120, timeout_per_minute=120,
                           gpu=False, gpu_min_free_mb=2048, language='en')
# The host-wide transcription queue, under the home directory of the user
# every city's bridge runs as. Deliberately not a setting: two cities naming
# different queues would run whisper at the same time.
QUEUE_SUBDIR = voice_transcribe.QUEUE_SUBDIR
_CPU_LIST_RE = voice_transcribe.CPU_LIST_RE
ADAPTER = PACK_DIR / 'transcriber' / 'adapter.py'
GPU_CHECK = PACK_DIR / 'transcriber' / 'gpu_check.py'
MODEL_SUMS = PACK_DIR / 'transcriber' / 'models.sha256'
UNIT_TEMPLATE = PACK_DIR / 'systemd' / 'telegram-bridge.service.in'


class SettingsError(TransportError):
    """The settings file is missing or invalid; the message names the first problem."""


class Settings:
    """One city's validated settings. `config` is the dict Bridge takes."""

    def __init__(self, **fields):
        self.__dict__.update(fields)


def _default_city_root():
    value = os.environ.get('GC_CITY_PATH') or os.environ.get('GC_CITY')
    return Path(value) if value else Path.cwd()


def settings_path(explicit, city_root, environ=None, home=None):
    """--settings, else $TELEGRAM_BRIDGE_SETTINGS, else ~/.config/<name>/telegram-bridge.toml
    where <name> is gc's workspace_name in <city_root>/.gc/site.toml."""
    import tomllib
    environ = os.environ if environ is None else environ
    if explicit:
        return Path(explicit).expanduser()
    if environ.get(SETTINGS_ENV):
        return Path(environ[SETTINGS_ENV]).expanduser()
    name = None
    if city_root is not None:
        try:
            name = tomllib.loads((Path(city_root) / '.gc' / 'site.toml').read_text()).get('workspace_name')
        except (OSError, ValueError):
            name = None
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise SettingsError(f'no settings file: pass --settings, set {SETTINGS_ENV}, or run from a city '
                            'whose .gc/site.toml names its workspace_name')
    return (Path(home) if home else Path.home()) / '.config' / name / SETTINGS_NAME


def _read_private_settings(path):
    try:
        with path.open('rb') as stream:
            st = os.fstat(stream.fileno())
            if not _is_owner_only_regular(st):
                raise SettingsError(f'settings file must be an owner-only regular file (mode 0600): {path}')
            raw = stream.read(SETTINGS_MAX_BYTES + 1)
    except FileNotFoundError:
        raise SettingsError(f'settings file not found: {path}') from None
    except OSError:
        raise SettingsError(f'settings file is unreadable: {path}') from None
    if len(raw) > SETTINGS_MAX_BYTES:
        raise SettingsError(f'settings file is larger than {SETTINGS_MAX_BYTES} bytes: {path}')
    return raw


def _table(data, key, allowed, required=False):
    value = data.get(key)
    if value is None:
        if required:
            raise SettingsError(f'settings: [{key}] is required')
        return {}
    if not isinstance(value, dict):
        raise SettingsError(f'settings: {key} must be a table')
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise SettingsError(f'settings: unknown key {key}.{unknown[0]}')
    return value


def _path_setting(value, what, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise SettingsError(f'settings: {what} must be a path')
    path = os.path.expanduser(value)
    if not _SAFE_PATH_RE.fullmatch(path) or os.path.normpath(path) != path:
        raise SettingsError(f'settings: {what} must be an absolute, normalized path of letters, digits and ._@+-')
    return Path(path)


def _same_file(first, second):
    """Whether two settings paths name one file: equal realpaths (an alias
    included), or, when both exist, the same device and inode (a hard link).
    Identity only; neither file is read."""
    if os.path.realpath(first) == os.path.realpath(second):
        return True
    try:
        a, c = os.stat(first), os.stat(second)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (c.st_dev, c.st_ino)


def _int_setting(value, what, low, high):
    if type(value) is not int or not low <= value <= high:
        raise SettingsError(f'settings: {what} must be an integer from {low} to {high}')
    return value


def _text_setting(value, what, pattern=None, limit=64):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or Bridge._CONTROL_RE.search(value) or (pattern is not None and not pattern.fullmatch(value))):
        raise SettingsError(f'settings: {what} is missing or malformed')
    return value


def _bool_setting(value, what):
    if type(value) is not bool:
        raise SettingsError(f'settings: {what} must be true or false')
    return value


def _handler_setting(value, what):
    """A command handler: an absolute path to an existing regular file this
    user may execute, writable by nobody but its owner, owned by this user
    or root, in a directory only they can write."""
    path = _path_setting(value, what)
    try:
        st = os.stat(path)
    except OSError:
        raise SettingsError(f'settings: {what} {path} does not exist') from None
    if not stat.S_ISREG(st.st_mode) or not os.access(path, os.X_OK):
        raise SettingsError(f'settings: {what} {path} must be an executable regular file')
    if st.st_mode & 0o022:
        raise SettingsError(f'settings: {what} {path} must not be writable by group or others')
    uid = os.getuid()
    directories = {path.parent, Path(os.path.realpath(path)).parent}
    owned = st.st_uid in (uid, 0)
    if owned:
        for directory in directories:
            try:
                dir_st = os.stat(directory)
            except OSError:
                owned = False
                break
            if dir_st.st_uid not in (uid, 0) or dir_st.st_mode & 0o022:
                owned = False
                break
    if not owned:
        raise SettingsError(f'settings: {what} {path} must be owned by this user or root, '
                            f'in a directory only they can write')
    return str(path)


def _handler_url_setting(value, what):
    match = _HANDLER_URL_RE.fullmatch(value) if isinstance(value, str) else None
    if not match or not 0 < int(match.group(2)) < 65536:
        raise SettingsError(f'settings: {what} must be http://127.0.0.1:<port>/<path> or http://[::1]:<port>/<path>')
    return value


def _command_settings(data, menu):
    """The validated [[command]] entries, in file order, each with its
    defaults filled in. `start` and every [[menu]] command always clash."""
    entries = data.get('command', [])
    if not isinstance(entries, list) or len(entries) > MENU_MAX or any(not isinstance(e, dict) for e in entries):
        raise SettingsError(f'settings: command must be a list of at most {MENU_MAX} [[command]] tables')
    menu_names = {name for name, _ in menu}
    commands = []
    for index, item in enumerate(entries, 1):
        name = _text_setting(item.get('name'), f'command entry {index} name', _COMMAND_RE)
        label = f'command entry {index} ({name})'
        unknown = sorted(set(item) - COMMAND_KEYS)
        if unknown:
            raise SettingsError(f'settings: unknown key {unknown[0]} in {label}')
        if any(name == other['name'] for other in commands):
            raise SettingsError(f'settings: {label} repeats a command name')
        if name in RESERVED_COMMANDS or name in menu_names:
            raise SettingsError(f'settings: {label} clashes with /{name}, which the bridge or [[menu]] already has')
        command = dict(name=name, description=_text_setting(item.get('description'), f'{label} description', limit=256),
                       menu=_bool_setting(item.get('menu', True), f'{label} menu'),
                       timeout=_int_setting(item.get('timeout', 10), f'{label} timeout', 1, 3600))
        if ('handler' in item) == ('url' in item):
            raise SettingsError(f'settings: {label} needs exactly one of handler and url')
        if 'handler' in item:
            command['handler'] = _handler_setting(item['handler'], f'{label} handler')
        else:
            command['url'] = _handler_url_setting(item['url'], f'{label} url')
        prefix = item.get('callback_prefix', name + ':')
        if not isinstance(prefix, str) or not _CALLBACK_PREFIX_RE.fullmatch(prefix):
            raise SettingsError(f'settings: {label} callback_prefix must be 1-64 printable ASCII characters, no spaces')
        for other in commands:
            if prefix.startswith(other['callback_prefix']) or other['callback_prefix'].startswith(prefix):
                raise SettingsError(f"settings: {label} callback_prefix overlaps the one of /{other['name']}")
        if prefix.startswith(Bridge.ASK_CALLBACK_PREFIX) or Bridge.ASK_CALLBACK_PREFIX.startswith(prefix):
            raise SettingsError(f'settings: {label} callback_prefix overlaps the bridge-owned ask: prefix')
        command['callback_prefix'] = prefix
        commands.append(command)
    shown = len(menu) + sum(1 for command in commands if command['menu'])
    if shown > MENU_MAX:
        raise SettingsError(f'settings: {shown} menu entries ([[menu]] and [[command]] with menu = true); '
                            f'Telegram takes at most {MENU_MAX}')
    return tuple(commands)


def _topic_settings(data, agent):
    """The validated [[topic]] entries, in file order, each {name, agent};
    a topic's agent defaults to the settings' `agent`."""
    entries = data.get('topic', [])
    if not isinstance(entries, list):
        raise SettingsError('settings: topic must be a list of [[topic]] tables')
    topics, seen = [], {}
    for index, item in enumerate(entries, 1):
        def fail(problem):
            raise SettingsError(f'settings: topic entry {index}: {problem}')
        if index > TOPIC_MAX:
            fail(f'more than {TOPIC_MAX} [[topic]] tables')
        if not isinstance(item, dict):
            fail('not a [[topic]] table')
        unknown = sorted(set(item) - TOPIC_KEYS)
        if unknown:
            fail(f'unknown key {unknown[0]}')
        name = item.get('name')
        if not isinstance(name, str) or not 1 <= len(name) <= TOPIC_NAME_MAX:
            fail(f'name must be a string of 1-{TOPIC_NAME_MAX} characters')
        if name != name.strip():
            fail('name has leading or trailing whitespace')
        if any(unicodedata.category(char) in ('Cc', 'Zl', 'Zp') for char in name):
            fail('name has a control or line separator character')
        if any(char in TOPIC_NAME_FORBIDDEN for char in name):
            fail('name has [, ] or |')
        if name.casefold() in seen:
            fail(f'name repeats topic entry {seen[name.casefold()]} (names differ in more than case)')
        seen[name.casefold()] = index
        topic_agent = item.get('agent', agent)
        if not isinstance(topic_agent, str) or not NAME_RE.fullmatch(topic_agent):
            fail('agent is missing or malformed')
        topics.append(dict(name=name, agent=topic_agent))
    return tuple(topics)


def _handler_write_paths(data):
    """Directories the unit lets handlers write, besides the bridge's own."""
    raw = data.get('handler_write_paths', [])
    if not isinstance(raw, list) or len(raw) > HANDLER_WRITE_PATHS_MAX:
        raise SettingsError(f'settings: handler_write_paths must be a list of at most {HANDLER_WRITE_PATHS_MAX} paths')
    paths = []
    for index, value in enumerate(raw, 1):
        path = _path_setting(value, f'handler_write_paths entry {index}')
        if path == Path('/') or path in paths:
            raise SettingsError(f'settings: handler_write_paths entry {index} must be a distinct directory other than /')
        paths.append(path)
    return tuple(paths)


def load_settings(path, home=None):
    """The validated Settings of one city, or SettingsError naming the first problem.
    `home` (default: this user's) holds the host-wide transcription queue."""
    import tomllib
    path = Path(path)
    raw = _read_private_settings(path)
    try:
        data = tomllib.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        raise SettingsError(f'settings file is not valid TOML: {path}') from None
    if 'quota' in data:
        raise SettingsError('settings: [quota] is no longer read; /quota is a [[command]] add-on (README.md, "Add-ons")')
    unknown = sorted(set(data) - SETTINGS_KEYS)
    if unknown:
        raise SettingsError(f'settings: unknown key {unknown[0]}')

    city = _text_setting(data.get('city'), 'city')
    bot = _table(data, 'bot', BOT_KEYS, required=True)
    ids = {key: bot.get(key) for key in ('bot_id', 'user_id', 'chat_id')}
    account = _text_setting(bot.get('account_id'), 'bot.account_id', NAME_RE)
    token_file = _path_setting(data.get('token_file'), 'token_file')
    callback_secret_file = _path_setting(data.get('callback_secret_file'), 'callback_secret_file')
    if _same_file(token_file, callback_secret_file):
        raise SettingsError('settings: token_file and callback_secret_file must name different files')
    lock_dir = _path_setting(data.get('lock_dir', str(token_file.parent)), 'lock_dir')
    timezone = _text_setting(data.get('timezone', 'UTC'), 'timezone')
    gc_base_url = _text_setting(data.get('gc_base_url', 'http://127.0.0.1:8372'), 'gc_base_url', limit=200)

    menu = data.get('menu', [])
    if not isinstance(menu, list) or len(menu) > 100:
        raise SettingsError('settings: menu must be a list of [[menu]] tables')
    menu_entries = []
    for index, item in enumerate(menu):
        if not isinstance(item, dict) or set(item) != {'command', 'description'}:
            raise SettingsError(f'settings: menu entry {index + 1} needs exactly command and description')
        menu_entries.append((_text_setting(item['command'], f'menu entry {index + 1} command', _COMMAND_RE),
                             _text_setting(item['description'], f'menu entry {index + 1} description', limit=256)))
    city_commands = _command_settings(data, menu_entries)

    config = dict(city=city, account_id=account, gc_base_url=gc_base_url,
                  user_display_name=_text_setting(data.get('user_display_name', 'User'), 'user_display_name'),
                  timezone=timezone, menu=tuple(menu_entries), **ids)
    # Present only when set, so a city without them gets the config it had.
    agent = _text_setting(data.get('agent', 'mayor'), 'agent')
    if agent != 'mayor':
        config['agent'] = agent
    if city_commands:
        config['commands'] = city_commands
    if 'reactions' in data:
        reactions = dict(REACTIONS_DEFAULTS, **_table(data, 'reactions', REACTIONS_KEYS))
        _bool_setting(reactions['on_accept'], 'reactions.on_accept')
        for name in ('seen', 'done'):
            if not valid_reaction_emoji(reactions[name]):
                raise SettingsError(f'settings: reactions.{name} must be one emoji: at most '
                                    f'{REACTION_MAX_CHARS} characters, no space, letter or digit')
        config['reactions'] = reactions
    if 'typing' in data:
        typing = dict(TYPING_DEFAULTS, **_table(data, 'typing', TYPING_KEYS))
        _int_setting(typing['seconds'], 'typing.seconds', 0, TYPING_MAX_SECONDS)
        config['typing'] = typing
    optional = (('gc_api_token_path', data.get('gc_api_token_file'), 'gc_api_token_file'),
                ('health_file', data.get('health_file'), 'health_file'))
    for key, value, what in optional:
        found = _path_setting(value, what, required=False)
        if found is not None:
            config[key] = str(found)
    protected = [str(token_file), str(callback_secret_file)]
    if config.get('gc_api_token_path') is not None:
        protected.append(config['gc_api_token_path'])
    protected.append(str(path))
    config['protected_paths'] = tuple(protected)
    check_bridge_config(config, settings=True)
    # After the check, so a malformed settings' agent is named as itself.
    topics = _topic_settings(data, agent)
    if topics:
        config['topics'] = topics
    unit = _text_setting(data.get('unit', f'{city}-telegram.service'), 'unit', _UNIT_RE, limit=128)
    config['unit'] = unit

    python = _path_setting(data.get('python', '/usr/bin/python3'), 'python')
    raw_transcribe = _table(data, 'transcribe', TRANSCRIBE_KEYS)
    found = dict(TRANSCRIBE_DEFAULTS, **raw_transcribe)
    gpu = _bool_setting(found['gpu'], 'transcribe.gpu')
    if 'method' in raw_transcribe:
        if 'gpu' in raw_transcribe:
            raise SettingsError('settings: set one of transcribe.method and transcribe.gpu')
        method = raw_transcribe['method']
        if method not in TRANSCRIBE_METHODS:
            raise SettingsError('settings: transcribe.method must be one of "auto", "cpu" or "gpu"')
    else:
        # The legacy boolean: gpu = true asks the GPU check, gpu = false is
        # the CPU, exactly method = "auto" and method = "cpu".
        method = 'auto' if gpu else 'cpu'
    if 'gpu_check' in raw_transcribe and method != 'auto':
        raise SettingsError('settings: transcribe.gpu_check needs method = "auto"')
    gpu_check = None
    if 'gpu_check' in raw_transcribe:
        gpu_check = _handler_setting(raw_transcribe['gpu_check'], 'transcribe.gpu_check')
    elif method == 'auto':
        gpu_check = _handler_setting(str(GPU_CHECK), 'transcribe.gpu_check')
    whisper_cli = _path_setting(found['whisper_cli'], 'transcribe.whisper_cli')
    models_dir = _path_setting(found['models_dir'], 'transcribe.models_dir')
    model = _text_setting(found['model'], 'transcribe.model', _MODEL_RE, limit=80)
    vad_model = _text_setting(found['vad_model'], 'transcribe.vad_model', _MODEL_RE, limit=80)
    threads = _int_setting(found['threads'], 'transcribe.threads', 1, 64)
    queue_dir = _path_setting(str((Path(home) if home else Path.home()) / QUEUE_SUBDIR), 'the home directory')
    timeout = _int_setting(found['timeout'], 'transcribe.timeout', 1, 3600)
    per_minute = _int_setting(found['timeout_per_minute'], 'transcribe.timeout_per_minute', 0, 3600)
    cpus = found['cpus']
    if not isinstance(cpus, str) or (cpus and not _CPU_LIST_RE.fullmatch(cpus)):
        raise SettingsError('settings: transcribe.cpus must be a CPU list such as "0-3"')
    gpu_min_free_mb = _int_setting(found['gpu_min_free_mb'], 'transcribe.gpu_min_free_mb', 256, 131072)
    language = found['language']
    if not isinstance(language, str) or not _LANGUAGE_RE.fullmatch(language):
        raise SettingsError('settings: transcribe.language must be a whisper language code '
                            '(short lowercase letters, such as "en" or "ru") or "auto"')
    enabled = _bool_setting(found['enabled'], 'transcribe.enabled')
    if enabled and language != 'en' and re.search(r'\.en(?:-|$)', model):
        raise SettingsError(f'settings: transcribe.model "{model}" is English-only and cannot decode '
                            f'transcribe.language "{language}"; use a multilingual model '
                            f'(such as large-v3-turbo-q5_0) or set language = "en"')
    transcribe = None
    if enabled:
        transcribe = dict(python=str(python), script=str(ADAPTER), model=model, timeout=timeout,
                          timeout_per_minute=per_minute, gpu=method in ('auto', 'gpu'), method=method,
                          language=language, env={
            'VOICE_TRANSCRIBE_WHISPER_CLI': str(whisper_cli),
            'VOICE_TRANSCRIBE_MODEL_DIR': str(models_dir),
            'VOICE_TRANSCRIBE_VAD_MODEL': vad_model,
            'VOICE_TRANSCRIBE_LANGUAGE': language,
            'VOICE_TRANSCRIBE_THREADS': str(threads),
            'VOICE_TRANSCRIBE_CPUS': cpus,
            'VOICE_TRANSCRIBE_TIMEOUT': str(timeout),
            'VOICE_TRANSCRIBE_TIMEOUT_PER_MINUTE': str(per_minute),
            'VOICE_TRANSCRIBE_GPU_MIN_FREE_MB': str(gpu_min_free_mb),
            'VOICE_TRANSCRIBE_METHOD': method,
        })
        if method == 'auto':
            transcribe['gpu_check'] = gpu_check
            transcribe['env']['VOICE_TRANSCRIBE_GPU_CHECK'] = gpu_check
    return Settings(
        path=path, city=city, config=config, unit=unit, python=python,
        state_dir=_path_setting(data.get('state_dir'), 'state_dir'),
        port=_int_setting(data.get('port'), 'port', 1024, 65535),
        token_file=token_file, lock_dir=lock_dir,
        callback_secret_file=callback_secret_file,
        pairing_update_id=_int_setting(data.get('pairing_update_id', -1), 'pairing_update_id', -1, 2 ** 63 - 1),
        transcribe=transcribe, whisper_cli=whisper_cli, models_dir=models_dir, model=model, vad_model=vad_model,
        language=language,
        queue_dir=queue_dir, agent=agent, handler_write_paths=_handler_write_paths(data))


def render_unit(settings, template=None, bridge_path=None):
    """The systemd user unit that runs this bridge for one city. Every path is
    one the settings loader already restricted to a safe absolute form, so
    nothing here can inject a systemd specifier, a quote or a second word."""
    bridge_path = Path(bridge_path or Path(__file__).resolve())
    for value, what in ((bridge_path, 'bridge path'), (settings.path, 'settings path')):
        _path_setting(str(value), what)
    text = Path(template or UNIT_TEMPLATE).read_text()
    # The transcription queue is the one path the bridge writes outside its
    # own directories, and only with transcription on.
    queue = f' {settings.queue_dir}' if settings.transcribe is not None else ''
    # City command handlers run inside this unit, so the directories they
    # write come last; a URL handler is its own process and needs none.
    handler_paths = ''.join(f' {path}' for path in getattr(settings, 'handler_write_paths', ()))
    # The credentials stay read-only inside the unit even when they sit in
    # the writable lock directory; the leading '-' lets a missing file pass.
    read_only = [settings.path, settings.token_file, settings.callback_secret_file]
    gc_token = settings.config.get('gc_api_token_path')
    if gc_token is not None:
        read_only.append(gc_token)
    read_only_paths = ' '.join(f'-{path}' for path in read_only)
    for name, value in (('CITY', settings.city), ('PYTHON', settings.python), ('BRIDGE', bridge_path),
                        ('SETTINGS', settings.path), ('STATE_DIR', settings.state_dir), ('LOCK_DIR', settings.lock_dir),
                        ('TRANSCRIBE_QUEUE', queue), ('HANDLER_WRITE_PATHS', handler_paths),
                        ('READ_ONLY_PATHS', read_only_paths),
                        ('AGENT', getattr(settings, 'agent', 'mayor'))):
        text = text.replace(f'@{name}@', str(value))
    if re.search(r'@[A-Z_]+@', text):
        raise SettingsError('unit template has an unknown placeholder')
    return text


def launchd_label(settings):
    """The launchd label of the bridge: the unit name without '.service'."""
    return _launchd_label(settings.unit)


def service_file(settings, platform=None, home=None):
    """Where the rendered service file lives: the systemd user unit on
    Linux/WSL (under $XDG_CONFIG_HOME or <home>/.config), the LaunchAgent
    plist on macOS."""
    plat = voice_transcribe.host_platform() if platform is None else platform
    home_path = Path(home) if home else Path.home()
    if plat == 'darwin':
        return home_path / 'Library' / 'LaunchAgents' / f'{launchd_label(settings)}.plist'
    xdg = os.environ.get('XDG_CONFIG_HOME')
    base = Path(xdg) if xdg else home_path / '.config'
    return base / 'systemd' / 'user' / settings.unit


def service_next_line(settings, bridge_path, platform=None, home=None):
    """The 'next:' line install.sh prints: the probe, the service-manager
    reload or bootstrap for this platform, and set-commands."""
    plat = voice_transcribe.host_platform() if platform is None else platform
    bridge = str(bridge_path)
    if plat == 'darwin':
        label = launchd_label(settings)
        plist = service_file(settings, platform=plat, home=home)
        return (f'next: python3 {bridge} --settings {settings.path} probe; '
                f'launchctl bootout gui/$(id -u)/{label} 2>/dev/null; '
                f'launchctl bootstrap gui/$(id -u) {plist}; '
                f'python3 {bridge} --settings {settings.path} set-commands '
                '(README.md, "Adopt in a city", step 6)')
    return (f'next: python3 {bridge} --settings {settings.path} probe; '
            'systemctl --user daemon-reload; '
            f'systemctl --user enable --now {settings.unit}; '
            f'python3 {bridge} --settings {settings.path} set-commands '
            '(README.md, "Adopt in a city", step 6)')


def render_service(settings, platform=None, home=None, bridge_path=None):
    """The service definition for this host: the systemd unit text on
    Linux/WSL (exactly render_unit), a launchd LaunchAgent plist on macOS.
    The agent passes --conflict-exit-status 0 so a polling conflict exits
    successfully and KeepAlive does not restart it."""
    plat = voice_transcribe.host_platform() if platform is None else platform
    if plat != 'darwin':
        return render_unit(settings, bridge_path=bridge_path)
    bridge_path = Path(bridge_path or Path(__file__).resolve())
    for value, what in ((bridge_path, 'bridge path'), (settings.path, 'settings path')):
        _path_setting(str(value), what)
    home_path = Path(home) if home else Path.home()
    label = launchd_label(settings)
    log = home_path / 'Library' / 'Logs' / f'{label}.log'
    agent = {
        'Label': label,
        'ProgramArguments': [str(settings.python), str(bridge_path), '--settings', str(settings.path),
                             'run', '--conflict-exit-status', '0'],
        'WorkingDirectory': str(settings.state_dir),
        'RunAtLoad': True,
        'KeepAlive': {'SuccessfulExit': False},
        'ThrottleInterval': 15,
        'ExitTimeOut': 45,
        'Umask': 63,
        'EnvironmentVariables': {'PATH': LAUNCHD_PATH},
        'StandardOutPath': str(log),
        'StandardErrorPath': str(log),
    }
    return plistlib.dumps(agent, fmt=plistlib.FMT_XML).decode('utf-8')


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def recorded_sums(path=None):
    """{file name: SHA-256} from the pack's sha256sum-format list."""
    sums = {}
    for line in Path(path or MODEL_SUMS).read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        digest, name = line.split(None, 1)
        sums[name.strip().lstrip('*')] = digest.lower()
    return sums


def supported_models(path=None, sums=None):
    """The supported transcription models: the ggml-<name>.bin entries of
    models.sha256 except the Silero VAD model, in file order."""
    if sums is None:
        sums = recorded_sums(path)
    return [name[5:-4] for name in sums
            if name.startswith('ggml-') and name.endswith('.bin') and not name.startswith('ggml-silero-')]


def _interpreter_version(python):
    """(major, minor) of the interpreter at `python`, or None when it cannot
    be run or its answer cannot be read."""
    try:
        done = subprocess.run([str(python), '-c', 'import sys; print(*sys.version_info[:2])'],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        major, minor = done.stdout.split()
        return int(major), int(minor)
    except (AttributeError, ValueError):
        return None


def install_problems(settings, which=shutil.which, sums=None, hasher=_sha256, platform=None,
                     version=_interpreter_version, osrelease='/proc/sys/kernel/osrelease',
                     systemd_dir='/run/systemd/system', linger_dir='/var/lib/systemd/linger', home=None):
    """[(ok, line)] for everything the bridge needs on this host; never prints
    a credential. The transcriber checks run only when it is enabled. A line
    starting with WARN is ok (not a failure): check-install prints it as a
    WARN line and still exits 0."""
    plat = voice_transcribe.host_platform() if platform is None else platform
    home_path = Path(home) if home else Path.home()
    results = []

    def check(ok, line):
        results.append((bool(ok), line))

    def warn(line):
        results.append((True, f'WARN  {line}'))

    def checked_paths():
        entries = []
        for value, what in ((getattr(settings, 'path', None), 'settings file'),
                            (getattr(settings, 'token_file', None), 'bot token file'),
                            (getattr(settings, 'callback_secret_file', None), 'callback secret file'),
                            (getattr(settings, 'state_dir', None), 'state directory'),
                            (getattr(settings, 'lock_dir', None), 'lock directory'),
                            (getattr(settings, 'whisper_cli', None), 'whisper-cli'),
                            (getattr(settings, 'models_dir', None), 'models directory')):
            if value is not None:
                entries.append((Path(value), what))
        for command in settings.config.get('commands', ()):
            if command.get('handler'):
                entries.append((Path(command['handler']), f"command /{command['name']} handler"))
        return entries

    if plat == 'wsl':
        try:
            release = Path(osrelease).read_text()
        except (OSError, UnicodeDecodeError):
            release = ''
        is_wsl2 = 'wsl2' in release.lower()
        systemd_ok = Path(systemd_dir).is_dir()
        if is_wsl2 and systemd_ok:
            check(True, 'WSL 2 with systemd')
        else:
            if not is_wsl2:
                check(False, 'WSL 1 is not supported: convert the distribution with '
                             'wsl --set-version <distro> 2')
            if not systemd_ok:
                check(False, 'systemd is not running in this WSL distribution: add [boot] systemd=true '
                             'to /etc/wsl.conf, run wsl --shutdown in Windows, reopen the distribution')
        for path, what in checked_paths():
            if str(path).startswith('/mnt/'):
                check(False, f'{what} {path} is on a Windows drive (/mnt/...): '
                             'keep it in the Linux file system')
        user = getpass.getuser()
        if not (Path(linger_dir) / user).exists():
            warn(f'no linger for {user}: the bridge stops when your last WSL session ends '
                 f'(loginctl enable-linger {user})')
    if plat == 'darwin':
        protected = (home_path / 'Desktop', home_path / 'Documents', home_path / 'Downloads',
                     home_path / 'Library' / 'Mobile Documents')
        for path, what in checked_paths():
            if any(path.is_relative_to(root) for root in protected):
                warn(f'{what} {path} is in a folder macOS privacy protection blocks for a launchd agent; move it')

    if not os.access(settings.python, os.X_OK):
        check(False, f'python {settings.python}')
    else:
        found = version(settings.python)
        try:
            new_enough = found is not None and tuple(found) >= (3, 11)
        except TypeError:
            new_enough = False
        if new_enough:
            check(True, f'python {settings.python}')
        else:
            check(False, f'python {settings.python} is not Python 3.11 or later '
                         '(set python in the settings to a 3.11+ interpreter)')
    if plat == 'darwin':
        check(which('curl', path=LAUNCHD_PATH), 'curl on PATH')
    else:
        check(which('curl'), 'curl on PATH')
    for path, what in ((settings.token_file, 'bot token'), (settings.callback_secret_file, 'callback secret')):
        try:
            private_text(path)
            check(True, f'{what} file is owner-only')
        except FileNotFoundError:
            # install.sh creates the callback secret before the unit starts:
            # the unit may not write next to it (its home is read-only).
            check(what == 'callback secret', f'{what} file {"is created by install.sh" if what == "callback secret" else "is missing"}')
        except (OSError, TransportError):
            check(False, f'{what} file must be an owner-only regular file')
    check(settings.state_dir.is_dir() or settings.state_dir.parent.is_dir(), f'state directory {settings.state_dir}')
    check(settings.lock_dir.is_dir(), f'lock directory {settings.lock_dir}')
    for command in settings.config.get('commands', ()):
        if command.get('handler'):
            try:
                _handler_setting(command['handler'], 'handler')
                ok = True
            except SettingsError:
                ok = False
            suffix = ("(runs with your user's full rights: no sandbox on macOS)" if plat == 'darwin'
                      else "(runs in the unit's sandbox)")
            check(ok, f"command /{command['name']} handler {command['handler']} {suffix}")
        else:
            check(True, f"command /{command['name']} handler {command['url']} (loopback; outside the unit's sandbox)")
    for path in getattr(settings, 'handler_write_paths', ()):
        check(path.is_dir(), f'handler write path {path}')
        if plat == 'darwin':
            warn(f'handler write path {path} is not enforced on macOS (no sandbox)')
    if settings.transcribe is None:
        check(True, 'transcription off')
        return results
    if plat == 'darwin':
        check(which('ffmpeg', path=LAUNCHD_PATH), 'ffmpeg on PATH')
        cpus = settings.transcribe.get('cpus') or (settings.transcribe.get('env') or {}).get('VOICE_TRANSCRIBE_CPUS', '')
        if cpus:
            check(False, 'transcribe.cpus is not supported on macOS (no CPU affinity); remove it')
    else:
        check(which('ffmpeg'), 'ffmpeg on PATH')
    try:
        st = os.stat(settings.queue_dir)
        check(_is_owner_only_directory(st),
              f'transcription queue {settings.queue_dir} is an owner-only directory')
    except FileNotFoundError:
        # install.sh creates it before the unit starts (the unit cannot).
        check(True, f'transcription queue {settings.queue_dir} is created by install.sh')
    except OSError:
        check(False, f'transcription queue {settings.queue_dir} cannot be inspected')
    check(os.path.isfile(settings.whisper_cli) and os.access(settings.whisper_cli, os.X_OK),
          f'whisper-cli {settings.whisper_cli}')
    try:
        sums = recorded_sums() if sums is None else sums
    except (OSError, ValueError):
        sums = {}
        check(False, 'recorded model checksums are unreadable')
    try:
        supported = supported_models(sums=sums)
    except (OSError, ValueError):
        supported = []
    unsupported = bool(supported) and settings.model not in supported
    model_file = settings.models_dir / f'ggml-{settings.model}.bin'
    try:
        local_unlisted = (unsupported and f'ggml-{settings.model}.bin' not in sums
                          and model_file.is_file() and model_file.stat().st_size > 0)
    except OSError:
        local_unlisted = False
    if unsupported and not local_unlisted:
        check(False, f'transcribe model {settings.model} is not one of the supported models: '
                     f'{", ".join(supported)}')
    for index, name in enumerate((settings.model, settings.vad_model)):
        if index == 0 and unsupported:
            if local_unlisted:
                warn(f'model ggml-{name}.bin is not in transcriber/models.sha256; skipping hash check')
            continue
        file_name = f'ggml-{name}.bin'
        path = settings.models_dir / file_name
        if file_name not in sums:
            check(False, f'model {file_name}: no recorded SHA-256 in the pack')
        elif not path.is_file():
            check(False, f'model {path} is missing')
        else:
            check(hasher(path) == sums[file_name], f'model {path} SHA-256')
    return results


GPU_PROBE = PACK_DIR / 'transcriber' / 'gpu_probe.py'
# The rendered unit's [Service] keys that start, run in or restart the bridge
# process; every other key there (the sandbox) goes onto the probe's
# transient unit as rendered, so a key the template gains reaches both.
UNIT_PROCESS_KEYS = frozenset({'Type', 'WorkingDirectory', 'ExecStart', 'Restart', 'RestartPreventExitStatus',
                               'RestartSec', 'TimeoutStopSec'})
_UNIT_KEY_RE = re.compile(r'[A-Za-z]+')
GPU_PROBE_OUTPUT_MAX = 64 * 1024
# With method auto a GPU check that answers false puts the note on the CPU
# for now, whatever its reason; only a GPU run that then failed, or a check
# the transcriber could not use, is a probe failure.
GPU_PROBE_AUTO_FAIL = ('cpu: gpu-failed', 'cpu: check-error')
GPU_PROBE_ERRORS = dict(Bridge.TRANSCRIBER_ERRORS, clip_unreadable='the clip is empty or over 25 MiB')


def unit_sandbox_properties(settings, bridge_path=None):
    """The rendered bridge unit's [Service] settings other than UNIT_PROCESS_KEYS,
    as Key=Value strings in unit order. SettingsError on a line that is not
    a section, a comment or Key=Value, or when PrivateTmp=true or
    ReadWritePaths is missing (the probe writes only inside its PrivateTmp)."""
    section, found = None, []
    for raw in render_unit(settings, bridge_path=bridge_path).splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
            continue
        key, sep, value = line.partition('=')
        if not sep or not _UNIT_KEY_RE.fullmatch(key):
            raise SettingsError('the rendered unit has a line that is not Key=Value')
        if section == 'Service' and key not in UNIT_PROCESS_KEYS:
            found.append(f'{key}={value}')
    if 'PrivateTmp=true' not in found or not any(p.startswith('ReadWritePaths=') for p in found):
        raise SettingsError('the rendered unit has no PrivateTmp=true or ReadWritePaths')
    return found


def gpu_probe_argv(settings, properties):
    """systemd-run for one probe: a transient user service with the bridge
    unit's sandbox, the settings' transcriber environment plus
    VOICE_TRANSCRIBE_GPU=1, and TMPDIR in its PrivateTmp."""
    env = dict(settings.transcribe['env'], VOICE_TRANSCRIBE_GPU='1', TMPDIR='/tmp')
    return (['systemd-run', '--user', '--wait', '--pipe', '--collect', '--quiet',
             '--description=Telegram bridge GPU probe']
            + [f'--property={p}' for p in properties]
            + [f'--setenv={key}={value}' for key, value in sorted(env.items())]
            + ['--', str(settings.python), str(GPU_PROBE), settings.model])


def darwin_gpu_probe_argv_env(settings, tmp_dir):
    """(argv, env) of one macOS probe: the pack's probe run directly with
    the launchd agent's environment, the settings' transcriber environment
    plus VOICE_TRANSCRIBE_GPU=1, and TMPDIR in a fresh private directory.
    There is no systemd-run and no sandbox to reproduce on macOS."""
    env = {'PATH': LAUNCHD_PATH, 'HOME': str(Path.home()),
           'LANG': os.environ.get('LANG') or 'C.UTF-8', 'TMPDIR': str(tmp_dir)}
    env.update(settings.transcribe['env'])
    env['VOICE_TRANSCRIBE_GPU'] = '1'
    return [str(settings.python), str(GPU_PROBE), settings.model], env


def default_probe_clip(settings):
    """samples/jfk.wav of the whisper.cpp checkout whisper_cli is built in
    (<checkout>/build/bin/whisper-cli), or None when it has no such layout."""
    parents = Path(os.path.realpath(settings.whisper_cli)).parents
    return parents[2] / 'samples' / 'jfk.wav' if len(parents) > 3 else None


def _probe_result(stdout):
    """The probe's final JSON object, or None when the output is not exactly
    running lines followed by one object of the known keys and values."""
    if len(stdout) > GPU_PROBE_OUTPUT_MAX:
        return None
    lines = stdout.decode('utf-8', 'replace').strip().splitlines()
    if not lines or any(line.encode() != Bridge.TRANSCRIBE_RUNNING_LINE for line in lines[:-1]):
        return None
    try:
        result = json.loads(lines[-1])
    except ValueError:
        return None
    if not isinstance(result, dict) or not result or set(result) - {'device', 'error', 'seconds'}:
        return None
    if 'device' in result and not Bridge._valid_transcribe_device(result['device']):
        return None
    if 'error' in result and not isinstance(result['error'], str):
        return None
    seconds = result.get('seconds')
    if 'seconds' in result and (type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0):
        return None
    return result


def gpu_probe(settings, clip=None, run=subprocess.run, which=shutil.which,
              platform=None, tmp_factory=tempfile.mkdtemp):
    """(status, line) of one GPU probe, status 'ok', 'FAIL' or 'skip': the clip
    transcribed by the pack's transcriber with VOICE_TRANSCRIBE_GPU=1. On
    Linux and WSL that is inside a transient unit carrying the bridge unit's
    sandbox; on macOS, where there is no systemd-run and no sandbox to
    reproduce, the probe runs directly with the launchd agent's
    environment. Waits in the host-wide transcription queue like any note.
    Never prints or returns the transcript, the unit's stderr or anything
    else the probe printed."""
    if settings.transcribe is None:
        return 'skip', 'gpu probe: transcription off ([transcribe] enabled = false)'
    if settings.transcribe.get('method') == 'cpu':
        return 'skip', 'gpu probe: method = cpu'
    if settings.transcribe.get('method') not in ('auto', 'gpu') and not settings.transcribe.get('gpu'):
        return 'skip', 'gpu probe: gpu = false'
    if clip is None:
        clip = default_probe_clip(settings)
        if clip is None or not clip.is_file():
            return 'FAIL', f'gpu probe: no clip: {clip or "samples/jfk.wav of the whisper.cpp checkout"} is missing; pass --clip'
    elif not Path(clip).is_file():
        return 'FAIL', f'gpu probe: clip {clip} is not a file'
    plat = voice_transcribe.host_platform() if platform is None else platform
    if plat == 'darwin':
        if not os.path.isdir(settings.queue_dir):
            return 'FAIL', f'gpu probe: {settings.queue_dir} is missing (the bridge writes it; run install.sh)'
        tmp_dir = tmp_factory(prefix='gpu-probe.')
        try:
            argv, env = darwin_gpu_probe_argv_env(settings, tmp_dir)
            try:
                with open(clip, 'rb') as stream:
                    done = run(argv, stdin=stream, capture_output=True, env=env)
            except OSError:
                return 'FAIL', 'gpu probe: the clip or the probe could not be opened'
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if done.returncode != 0:
            return 'FAIL', f'gpu probe: the probe failed (exit {done.returncode})'
    else:
        if which('systemd-run') is None:
            return 'FAIL', 'gpu probe: systemd-run not on PATH'
        try:
            properties = unit_sandbox_properties(settings)
        except (OSError, SettingsError):
            return 'FAIL', 'gpu probe: the bridge unit could not be rendered'
        for prop in properties:
            if prop.startswith('ReadWritePaths='):
                for path in prop.split('=', 1)[1].split():
                    if not os.path.isdir(path):
                        return 'FAIL', f'gpu probe: {path} is missing (the bridge unit writes it; run install.sh)'
        try:
            with open(clip, 'rb') as stream:
                done = run(gpu_probe_argv(settings, properties), stdin=stream, capture_output=True)
        except OSError:
            return 'FAIL', 'gpu probe: the clip or systemd-run could not be opened'
        if done.returncode != 0:
            return 'FAIL', f'gpu probe: the probe unit failed (exit {done.returncode})'
    result = _probe_result(done.stdout)
    device = (result or {}).get('device')
    if result is None:
        return 'FAIL', 'gpu probe: invalid output'
    if device == 'gpu' or (isinstance(device, str) and device.startswith('gpu: ')):
        if 'seconds' not in result:
            return 'FAIL', 'gpu probe: invalid output'
        return 'ok', f"gpu probe: device {device} ({result['seconds']} s)"
    if settings.transcribe.get('method') == 'auto' and isinstance(device, str) \
            and device.startswith('cpu: ') and device not in GPU_PROBE_AUTO_FAIL:
        return 'skip', f'gpu probe: GPU not free now ({device}), try later'
    if device is not None:
        return 'FAIL', f'gpu probe: device {device}'
    if 'error' in result:
        return 'FAIL', f"gpu probe: {GPU_PROBE_ERRORS.get(result['error'], 'invalid output')}"
    return 'FAIL', 'gpu probe: invalid output'


def main():
    parser = _build_parser()
    args = parser.parse_args()
    _validate_attachment_flags(parser, args)
    os.umask(0o077)
    settings = load_settings(settings_path(args.settings, args.city_root))
    if args.command == 'render-unit':
        sys.stdout.write(render_service(settings))
        return
    if args.command == 'check-install':
        results = install_problems(settings)
        for ok, line in results:
            print(line if line.startswith('WARN') else ('ok    ' if ok else 'FAIL  ') + line)
        failed = sum(1 for ok, _ in results if not ok)
        if args.gpu_probe:
            status, line = gpu_probe(settings, args.clip)
            print(f'{status:<6}{line}')
            failed += status == 'FAIL'
        if failed:
            raise TransportError(f'{failed} install check(s) failed')
        return
    port = args.port if args.port is not None else settings.port
    token = private_text(settings.token_file)
    if args.command == 'set-commands':
        # No Store, callback secret or lock: the running unit owns those.
        commands = Bridge(settings.config, None, token, None, port, state_dir=settings.state_dir).set_commands()
        names = ' '.join('/' + c['command'] for c in commands)
        print(f"commands set: {len(commands)} ({names}) for chat {settings.config['chat_id']}")
        return
    settings.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    store = Store(settings.state_dir / 'telegram-bridge.sqlite3', settings.pairing_update_id + 1)
    bridge = Bridge(settings.config, store, token, capability(settings.callback_secret_file), port,
                    state_dir=settings.state_dir, transcribe=settings.transcribe)
    if args.command == 'probe':
        bridge.probe()
        print('Telegram identity and webhook checks passed')
    elif args.command in ('send', 'reply-current'):
        if not args.body_file or not args.idempotency_key:
            parser.error('send/reply-current requires --body-file and --idempotency-key (reuse the key for retries)')
        args.session = args.session or bridge.agent
        if args.conversation_id and args.conversation_id != bridge.conversation['conversation_id']:
            raise TransportError('conversation ID is not the paired private recipient')
        message_id = _dispatch_outbound(bridge, args, args.body_file.read_text())
        print('Telegram delivery confirmed; message ID ' + str(message_id))
    elif args.command == 'ask-close':
        # Directly in this process through the shared ledger; GC is never called.
        receipt = bridge.close_ask(args.decision, message_id=args.message_id, ask_key=args.ask_key)
        if receipt.get('delivered') is not True:
            reason = (receipt.get('metadata') or {}).get('reason') or 'delivery not confirmed'
            _print_gc_receipt(False, receipt.get('failure_kind', 'permanent'), reason)
            if reason in ('ask_already_answered', 'ask_already_decided'):
                raise TransportError(f'ask not closed: {reason} (recorded: {receipt.get("answer")})')
            if reason in Bridge.ASK_CLOSE_REFUSALS:
                raise TransportError(f'ask not closed: {reason}')
            if reason == NOT_SENT_REASON:
                raise TransportError(f'ask closed; Telegram edit not confirmed: {reason}: '
                                     'run the same command again to send the edit')
            raise TransportError(f'ask closed; Telegram edit not confirmed: {reason}')
        print('Telegram ask closed; message ID ' + str(receipt['message_id']))
    elif args.command == 'react':
        if args.reaction is None:
            parser.error('react requires --reaction seen|done|none|<emoji>')
        print('Telegram reaction set; message ID ' + str(_react(bridge, args)))
    elif args.command == 'pin':
        # Directly in this process through the shared ledger; GC is never called.
        slot = args.slot
        receipt = bridge.unpin(slot) if args.unpin else bridge.pin(slot, args.body_file.read_text(), args.format)
        if receipt.get('delivered') is not True:
            reason = (receipt.get('metadata') or {}).get('reason') or 'delivery not confirmed'
            _print_gc_receipt(False, receipt.get('failure_kind', 'permanent'), reason)
            step = receipt.get('step')
            message = f'pin {slot} not updated: {reason}' if step is None else \
                f'pin {slot}: Telegram {step} not confirmed: {reason}'
            if receipt.get('message_id'):
                message += f' (message ID {receipt["message_id"]})'
            if reason == NOT_SENT_REASON:
                message += ': run the same command again'
            raise TransportError(message)
        if args.unpin and receipt.get('ended_generation') is not None:
            print(f'pin {slot} not pinned; unconfirmed send of generation {receipt["ended_generation"]} ended, '
                  'the next update sends a new message')
        elif args.unpin:
            print(f'pin {slot} not pinned' if receipt['message_id'] is None
                  else f'unpinned message ID {receipt["message_id"]} (slot {slot})')
        elif not receipt['changed']:
            print(f'pin {slot} unchanged; message ID {receipt["message_id"]}')
        else:
            print(f'pinned message ID {receipt["message_id"]} (slot {slot}, revision {receipt["revision"]})')
    elif args.command == 'topics':
        # Directly in this process through the shared ledger, under its own
        # lock; never getUpdates, so it runs beside the polling unit.
        lines, complete = bridge.topics(record=args.record, thread_id=args.thread_id, retry=args.retry)
        for line in lines:
            print(line)
        if not complete:
            raise TransportError('not every [[topic]] has a thread id')
    else:
        # Token-wide lock survives alternate state directories; never unlink while locked.
        with (settings.lock_dir / ('telegram-bot-' + str(settings.config['bot_id']) + '.lock')).open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TransportError('another bridge already owns this bot') from None
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, lambda *_: bridge.stop.set())
            bridge.reconcile_pending()
            try:
                bridge.run()
            except PollConflict as error:
                print(str(error), file=sys.stderr)
                sys.exit(args.conflict_exit_status)


if __name__ == '__main__':
    try:
        main()
    except PollConflict as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
    except TransportError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError):
        print('Telegram bridge operation failed; check private configuration, network, and delivery state', file=sys.stderr)
        sys.exit(1)
