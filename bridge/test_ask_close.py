"""Closing a mayor ask with the decision made (OPERATIONS.md A6): the guarded
ledger write, the shared edit step, a tap racing a close, the decided column
of an existing ledger, the ask-close CLI and the gc telegram ask-close
command, all with the fake Telegram and temporary directories."""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
import test_bridge  # noqa: E402
from test_bridge import BridgeTestCase, run_main, write_fake_curl, write_settings  # noqa: E402

PACK = Path(__file__).resolve().parents[1]
INBOUND = '/v0/city/test-city/extmsg/inbound'
HTML_REJECTED = 400, dict(ok=False, error_code=400, description="Bad Request: can't parse entities: x")


class AskCloseTests(BridgeTestCase):
    """Bridge.close_ask against AskTests' fake transport."""

    # AskTests' fake transport and ask helpers, without its tests (imported
    # by module, so discovery runs AskTests once, in test_bridge).
    ask_transport = test_bridge.AskTests.ask_transport
    ask_id = test_bridge.AskTests.ask_id
    keyboard = test_bridge.AskTests.keyboard
    ask = test_bridge.AskTests.ask
    press = test_bridge.AskTests.press
    sent = test_bridge.AskTests.sent
    photo_ask = test_bridge.AskTests.photo_ask

    def setUp(self):
        super().setUp()
        self.bridge.request = self.ask_transport
        self.bridge.sleep = lambda delay: None

    def gc_calls(self):
        return [url for url, _ in self.calls if url.startswith(self.bridge.base)]

    def edits(self):
        return self.sent('editMessageText') + self.sent('editMessageCaption')

    def ledger_keys(self):
        with closing(sqlite3.connect(self.path)) as db:
            return sorted(key for (key,) in db.execute('SELECT key FROM deliveries'))

    def assert_refused(self, receipt, reason):
        self.assertFalse(receipt['delivered'])
        self.assertEqual(receipt['metadata']['reason'], reason)

    def test_closing_a_text_ask_edits_once_and_records_the_decision(self):
        self.assertTrue(self.bridge.publish(self.ask())['delivered'])
        receipt = self.bridge.close_ask('Ship it on Monday', message_id=77)
        self.assertTrue(receipt['delivered'])
        self.assertEqual(receipt['message_id'], '77')
        [edit] = self.sent('editMessageText')
        self.assertEqual(edit['text'], 'Pick one?\n✓ Decided: Ship it on Monday')
        self.assertEqual(edit['message_id'], 77)
        self.assertEqual(edit['reply_markup'], {'inline_keyboard': []})
        self.assertNotIn('parse_mode', edit)
        self.assertEqual(self.sent('editMessageCaption'), [])
        self.assertEqual(self.gc_calls(), [])
        ask = self.store.get_ask(self.ask_id())
        self.assertTrue(ask['answered'])
        self.assertTrue(ask['decided'])
        self.assertTrue(ask['relayed'])
        self.assertEqual(ask['answer'], 'Ship it on Monday')
        self.assertIsNone(ask['update_id'])
        self.assertEqual(self.ledger_keys(), ['ask-send-1', f'ask:{self.ask_id()}:edit'])

    def test_closing_a_photo_ask_edits_the_caption(self):
        self.assertTrue(self.bridge.publish(self.photo_ask())['delivered'])
        receipt = self.bridge.close_ask('Neither, redo it', message_id=88)
        self.assertTrue(receipt['delivered'])
        self.assertEqual(receipt['message_id'], '88')
        [edit] = self.sent('editMessageCaption')
        self.assertEqual(edit['caption'], 'Pick one?\n✓ Decided: Neither, redo it')
        self.assertEqual(edit['message_id'], 88)
        self.assertEqual(edit['reply_markup'], {'inline_keyboard': []})
        self.assertEqual(self.sent('editMessageText'), [])
        self.assertTrue(self.store.get_ask(self.ask_id('photo-ask-1'))['decided'])

    def test_an_html_ask_escapes_the_decision_and_falls_back_to_plain(self):
        def transport(url, body, *args, **kwargs):
            if url.endswith('/editMessageText') and body.get('parse_mode') == 'HTML':
                self.calls.append((url, body))
                return HTML_REJECTED
            return self.ask_transport(url, body)
        request = self.ask(text='<b>Pick</b> one')
        request['format'] = 'html'
        self.assertTrue(self.bridge.publish(request)['delivered'])
        self.assertTrue(self.bridge.close_ask('x < y & z > w', message_id=77)['delivered'])
        [edit] = self.sent('editMessageText')
        self.assertEqual(edit['parse_mode'], 'HTML')
        self.assertEqual(edit['text'], '<b>Pick</b> one\n✓ Decided: x &lt; y &amp; z &gt; w')

        self.calls.clear()
        self.bridge.request = transport
        request = self.ask(key='html-2', text='<b>Pick</b> one')
        request['format'] = 'html'
        self.assertTrue(self.bridge.publish(request)['delivered'])
        receipt = self.bridge.close_ask('x < y', ask_key='html-2')
        self.assertTrue(receipt['delivered'])
        self.assertEqual(receipt['format_fallback_reason'], 'telegram_html_parse_rejected_400')
        first, second = self.sent('editMessageText')
        self.assertEqual(first['text'], '<b>Pick</b> one\n✓ Decided: x &lt; y')
        self.assertNotIn('parse_mode', second)
        self.assertEqual(second['text'], 'Pick one\n✓ Decided: x < y')
        self.assertEqual(second['reply_markup'], {'inline_keyboard': []})

    def test_ask_key_and_message_id_close_the_same_ask(self):
        self.assertTrue(self.bridge.publish(self.ask())['delivered'])
        by_key = self.bridge.close_ask('Later', ask_key='ask-send-1')
        self.assertTrue(by_key['delivered'])
        self.assertEqual(self.bridge.close_ask('Later', message_id=77), by_key)
        self.assertEqual(len(self.edits()), 1)

    def test_a_repeat_with_the_same_decision_returns_the_saved_receipt(self):
        self.assertTrue(self.bridge.publish(self.ask())['delivered'])
        first = self.bridge.close_ask('Later', message_id=77)
        before = list(self.calls)
        self.assertEqual(self.bridge.close_ask('Later', message_id=77), first)
        self.assertEqual(self.calls, before)

    def test_another_decision_refuses_as_already_decided(self):
        self.assertTrue(self.bridge.publish(self.ask())['delivered'])
        self.assertTrue(self.bridge.close_ask('Later', message_id=77)['delivered'])
        before = list(self.calls)
        refused = self.bridge.close_ask('Sooner', message_id=77)
        self.assert_refused(refused, 'ask_already_decided')
        self.assertEqual(refused['answer'], 'Later')
        self.assertEqual(self.calls, before)
        self.assertEqual(self.store.get_ask(self.ask_id())['answer'], 'Later')

    def test_a_tapped_ask_refuses_as_already_answered(self):
        self.assertTrue(self.bridge.publish(self.ask())['delivered'])
        self.bridge.drain([self.press(uid=41)])
        before = list(self.calls)
        refused = self.bridge.close_ask('Later', message_id=77)
        self.assert_refused(refused, 'ask_already_answered')
        self.assertEqual(refused['answer'], 'Yes')
        self.assertEqual(self.calls, before)
        ask = self.store.get_ask(self.ask_id())
        self.assertFalse(ask['decided'])
        self.assertEqual(ask['answer'], 'Yes')

    def test_a_tap_after_a_close_is_already_answered_and_relays_nothing(self):
        self.assertTrue(self.bridge.publish(self.ask())['delivered'])
        self.assertTrue(self.bridge.close_ask('Later', message_id=77)['delivered'])
        self.bridge.drain([self.press(uid=41)])
        self.assertEqual(self.sent('answerCallbackQuery'),
                         [dict(callback_query_id='query-41', text='Already answered')])
        self.assertEqual(self.gc_calls(), [])
        self.assertEqual(len(self.edits()), 1)
        self.assertEqual(self.store.get_ask(self.ask_id())['answer'], 'Later')
        self.assertEqual(self.store.offset(), 42)

    def test_a_close_that_loses_the_guarded_write_to_a_tap_refuses(self):
        self.assertTrue(self.bridge.publish(self.ask())['delivered'])
        close = self.store.close_ask

        def tap_first(ask_id, decision):
            self.assertTrue(self.store.mark_ask_answered(ask_id, 'Yes', 41))  # the daemon's tap, meanwhile
            return close(ask_id, decision)
        with patch.object(self.store, 'close_ask', side_effect=tap_first):
            refused = self.bridge.close_ask('Later', message_id=77)
        self.assert_refused(refused, 'ask_already_answered')
        self.assertEqual(refused['answer'], 'Yes')
        self.assertEqual(self.edits(), [])
        ask = self.store.get_ask(self.ask_id())
        self.assertFalse(ask['decided'])
        self.assertEqual(ask['answer'], 'Yes')

    def test_a_tap_whose_mark_loses_to_a_close_relays_nothing_and_edits_once(self):
        self.assertTrue(self.bridge.publish(self.ask())['delivered'])
        mark = self.store.mark_ask_answered

        def close_first(ask_id, answer, update_id):
            self.assertTrue(self.store.close_ask(ask_id, 'Later'))  # the CLI's write, meanwhile
            return mark(ask_id, answer, update_id)
        with patch.object(self.store, 'mark_ask_answered', side_effect=close_first):
            self.bridge.drain([self.press(uid=41)])
        # The race note: this callback was answered empty before the mark.
        self.assertEqual(self.sent('answerCallbackQuery'), [dict(callback_query_id='query-41')])
        self.assertEqual(self.gc_calls(), [])
        [edit] = self.sent('editMessageText')
        self.assertEqual(edit['text'], 'Pick one?\n✓ Decided: Later')
        self.assertEqual(edit['reply_markup'], {'inline_keyboard': []})
        ask = self.store.get_ask(self.ask_id())
        self.assertEqual((ask['answer'], ask['decided'], ask['update_id']), ('Later', True, None))
        self.assertEqual(self.store.offset(), 42)
        # The CLI's own edit step then finds the tap's edit: one request.
        self.assertTrue(self.bridge.close_ask('Later', message_id=77)['delivered'])
        self.assertEqual(len(self.edits()), 1)

    def test_unknown_non_ask_and_unconfirmed_asks_refuse_before_any_write(self):
        self.assertTrue(self.bridge.publish(self.publish(key='plain-1', text='just news'))['delivered'])
        self.assert_refused(self.bridge.close_ask('Later', message_id=77), 'ask_not_found')
        self.assert_refused(self.bridge.close_ask('Later', message_id=999), 'ask_not_found')
        self.assert_refused(self.bridge.close_ask('Later', ask_key='never-sent'), 'ask_not_found')
        self.assert_refused(self.bridge.close_ask('Later', ask_key='plain-1'), 'ask_not_found')

        def ambiguous(url, body, *args, **kwargs):
            self.calls.append((url, body))
            return 502, dict(ok=False, error_code=502)
        self.bridge.request = ambiguous
        with patch('sys.stderr', new_callable=io.StringIO):
            self.assertFalse(self.bridge.publish(self.ask(key='unconfirmed'))['delivered'])
        self.assertIsNone(self.store.get_ask(self.ask_id('unconfirmed'))['message_id'])
        self.bridge.request = self.ask_transport
        self.assert_refused(self.bridge.close_ask('Later', ask_key='unconfirmed'), 'ask_message_unknown')
        self.assertEqual(self.edits(), [])
        self.assertEqual(self.ledger_keys(), ['plain-1', 'unconfirmed'])
        self.assertFalse(self.store.get_ask(self.ask_id('unconfirmed'))['answered'])

    def test_empty_line_break_and_control_character_decisions_refuse(self):
        self.assertTrue(self.bridge.publish(self.ask())['delivered'])
        cases = [('', 'decision_required'), ('   ', 'decision_required'), ('\n', 'decision_required'),
                 (None, 'decision_required'), ('a\nb', 'decision_breaks_line'),
                 ('a\rb', 'decision_breaks_line'), ('a\u2028b', 'decision_breaks_line'),
                 ('a\x85b', 'decision_breaks_line'), ('a\tb', 'decision_breaks_line'),
                 ('a\x00b', 'decision_breaks_line'), ('a\x1bb', 'decision_breaks_line')]
        for decision, reason in cases:
            with self.subTest(decision=decision):
                self.assert_refused(self.bridge.close_ask(decision, message_id=77), reason)
        self.assertEqual(self.edits(), [])
        self.assertFalse(self.store.get_ask(self.ask_id())['answered'])
        self.bridge.drain([self.press(uid=41)])  # still answerable by a tap
        self.assertEqual(self.sent('editMessageText')[0]['text'], 'Pick one?\n✓ Yes')

    def test_a_decision_must_fit_the_text_and_caption_room(self):
        self.assertEqual(b.utf16_units(b.Bridge.ASK_DECIDED_PREFIX), 12)
        for limit, request, mid, method in ((4096, self.ask(key='room-text', text='q' * 4000), 77, 'editMessageText'),
                                            (1024, self.photo_ask(key='room-photo', caption='q' * 1000), 88,
                                             'editMessageCaption')):
            with self.subTest(limit=limit):
                self.assertTrue(self.bridge.publish(request)['delivered'])
                room = limit - len(request['text']) - 12
                # One unit over, with an astral character counting two units.
                self.assert_refused(self.bridge.close_ask('d' * (room - 1) + '\U0001F600', message_id=mid),
                                    'decision_exceeds_room')
                self.assertEqual(self.sent(method), [])
                self.assertTrue(self.bridge.close_ask('d' * room, message_id=mid)['delivered'])
                [edit] = self.sent(method)
                self.assertEqual(b.utf16_units(edit.get('text', edit.get('caption'))), limit)

    def test_a_rejected_edit_leaves_the_ask_closed_and_reports_the_reason(self):
        def transport(url, body, *args, **kwargs):
            if url.endswith('/editMessageText'):
                self.calls.append((url, body))
                return 400, dict(ok=False, error_code=400, description='Bad Request: message to edit not found')
            return self.ask_transport(url, body)
        self.assertTrue(self.bridge.publish(self.ask())['delivered'])
        self.bridge.request = transport
        receipt = self.bridge.close_ask('Later', message_id=77)
        self.assert_refused(receipt, 'telegram_rejected_not_sent_400')
        ask = self.store.get_ask(self.ask_id())
        self.assertTrue(ask['answered'] and ask['decided'] and ask['relayed'])
        self.assertEqual(self.bridge.close_ask('Later', message_id=77), receipt)
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            self.bridge.drain([self.press(uid=41)])
        self.assertEqual(log.getvalue().splitlines(), ['ask edit not confirmed (update 41)'])
        self.assertEqual(len(self.sent('editMessageText')), 1)
        self.assertEqual(self.sent('answerCallbackQuery')[-1]['text'], 'Already answered')
        self.assertEqual(self.gc_calls(), [])

    def test_a_not_sent_edit_is_retried_by_a_repeat(self):
        state = dict(down=True)

        def transport(url, body, *args, **kwargs):
            if url.endswith('/editMessageText') and state['down']:
                self.calls.append((url, body))
                raise b.RequestNotSent('network request not sent', 7, 'connect')
            return self.ask_transport(url, body)
        self.assertTrue(self.bridge.publish(self.ask())['delivered'])
        self.bridge.request = transport
        with patch('sys.stderr', new_callable=io.StringIO):
            receipt = self.bridge.close_ask('Later', message_id=77)
        self.assert_refused(receipt, b.NOT_SENT_REASON)
        self.assertTrue(self.store.get_ask(self.ask_id())['decided'])
        tries = len(self.sent('editMessageText'))
        state['down'] = False
        retried = self.bridge.close_ask('Later', message_id=77)
        self.assertTrue(retried['delivered'])
        edits = self.sent('editMessageText')
        self.assertEqual(len(edits), tries + 1)
        self.assertEqual(edits[-1]['text'], 'Pick one?\n✓ Decided: Later')

    BASE_ASKS = ('CREATE TABLE asks (ask_id TEXT PRIMARY KEY, question TEXT NOT NULL, '
                 'format TEXT NOT NULL, options TEXT NOT NULL, message_id INTEGER, '
                 'answered INTEGER NOT NULL DEFAULT 0, answer TEXT, '
                 'update_id INTEGER, relayed INTEGER NOT NULL DEFAULT 0, '
                 "kind TEXT NOT NULL DEFAULT 'text')")

    def test_a_ledger_without_the_decided_column_gains_it_and_stays_answerable(self):
        path = Path(self.tmp.name) / 'base.sqlite3'
        ask_id = self.ask_id()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE cursor (id INTEGER PRIMARY KEY, offset INTEGER NOT NULL)')
            db.execute('INSERT INTO cursor VALUES (1, 10)')
            db.execute('CREATE TABLE deliveries (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, receipt TEXT NOT NULL)')
            db.execute(self.BASE_ASKS)
            db.execute('INSERT INTO asks(ask_id, question, format, options, message_id) VALUES (?,?,?,?,?)',
                       (ask_id, 'Old question?', 'plain', json.dumps(self.keyboard()[0]), 77))
        self.store = b.Store(path, 10)
        b.Store(path, 10)  # a second open finds the column and adds nothing
        with closing(sqlite3.connect(path)) as db:
            columns = [(row[1], row[2], row[3], row[4]) for row in db.execute('PRAGMA table_info(asks)')]
        self.assertEqual(columns[-2:], [('decided', 'INTEGER', 1, '0'), ('conversation_id', 'TEXT', 0, None)])
        self.assertEqual([name for name, *_ in columns].count('decided'), 1)
        ask = self.store.get_ask(ask_id)
        self.assertEqual((ask['answered'], ask['decided'], ask['message_id']), (False, False, 77))
        self.bridge.store = self.store
        self.bridge.drain([self.press(uid=41)])
        [edit] = self.sent('editMessageText')
        self.assertEqual(edit['text'], 'Old question?\n✓ Yes')
        self.assertEqual(len([url for url in self.gc_calls() if url.endswith(INBOUND)]), 1)
        ask = self.store.get_ask(ask_id)
        self.assertEqual((ask['answered'], ask['decided'], ask['relayed'], ask['answer']), (True, False, True, 'Yes'))

    def test_close_ask_takes_exactly_one_selector(self):
        for selectors in ({}, dict(message_id=77, ask_key='ask-send-1')):
            with self.subTest(selectors=selectors), self.assertRaises(ValueError):
                self.bridge.close_ask('Later', **selectors)


class AskCloseCliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.settings = write_settings(self.root)

    def record_ask(self, key='cli-ask', message_id=42, question='Merge now?'):
        state = self.root / 'state'
        state.mkdir(mode=0o700, exist_ok=True)
        store = b.Store(state / 'telegram-bridge.sqlite3', 6)
        ask_id = b.Bridge._ask_id_for_key(key)
        store.record_ask(ask_id, question, 'plain', [dict(text='Yes', callback_data=f'ask:{ask_id}:yes')])
        store.set_ask_message([ask_id], message_id)
        return store, ask_id

    def cli(self, *argv):
        """Run main() with the temporary settings and curl faked in-process;
        returns (stdout, stderr, the (url, JSON body) of each request)."""
        requests = []

        def fake_run(argv, input='', **kwargs):
            config = dict(line.split(' = ', 1) for line in input.splitlines() if ' = ' in line)
            url = json.loads(config['url'])
            body = json.loads(json.loads(config['data']))
            requests.append((url, body))
            if not url.endswith('/editMessageText'):
                raise AssertionError('unexpected curl invocation')
            payload = {'ok': True, 'result': {'message_id': body['message_id'], 'chat': {'id': 123}}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload) + '\n200 0.010000 0.020000 0', '')
        with patch.object(b.subprocess, 'run', fake_run), \
                patch('sys.stdout', new_callable=io.StringIO) as out, \
                patch('sys.stderr', new_callable=io.StringIO) as err:
            try:
                run_main(self, 'bridge.py', '--settings', str(self.settings), 'ask-close', *argv)
            finally:
                self.out, self.err, self.requests = out.getvalue(), err.getvalue(), requests

    def test_ask_close_edits_the_ask_and_prints_the_closed_line(self):
        store, ask_id = self.record_ask()
        self.cli('--message-id', '42', '--decision', 'Merged by hand')
        self.assertEqual(self.out, 'Telegram ask closed; message ID 42\n')
        [(url, body)] = self.requests
        self.assertTrue(url.endswith('/editMessageText'))
        self.assertEqual(body['text'], 'Merge now?\n✓ Decided: Merged by hand')
        self.assertEqual(body['reply_markup'], {'inline_keyboard': []})
        self.assertTrue(store.get_ask(ask_id)['decided'])
        self.cli('--ask-key', 'cli-ask', '--decision', 'Merged by hand')  # the idempotent repeat
        self.assertEqual(self.out, 'Telegram ask closed; message ID 42\n')
        self.assertEqual(self.requests, [])

    def test_a_refusal_prints_the_receipt_line_and_fails(self):
        self.record_ask()
        with self.assertRaisesRegex(b.TransportError, r'^ask not closed: ask_not_found$'):
            self.cli('--message-id', '43', '--decision', 'Merged by hand')
        self.assertIn('GC_RECEIPT {"delivered": false, "failure_kind": "permanent", "reason": "ask_not_found"}',
                      self.err)
        self.assertEqual(self.requests, [])
        self.cli('--message-id', '42', '--decision', 'Merged by hand')
        with self.assertRaisesRegex(b.TransportError, r'ask_already_decided \(recorded: Merged by hand\)'):
            self.cli('--message-id', '42', '--decision', 'Not merged')
        self.assertIn('"reason": "ask_already_decided"', self.err)

    def test_an_unconfirmed_edit_says_the_ask_is_closed(self):
        store, ask_id = self.record_ask()

        def rejecting_run(argv, input='', **kwargs):
            payload = {'ok': False, 'error_code': 400, 'description': 'Bad Request: message to edit not found'}
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload) + '\n400 0.010000 0.020000 0', '')
        with patch.object(b.subprocess, 'run', rejecting_run), \
                patch('sys.stderr', new_callable=io.StringIO) as err, \
                self.assertRaisesRegex(b.TransportError,
                                       r'^ask closed; Telegram edit not confirmed: telegram_rejected_not_sent_400$'):
            run_main(self, 'bridge.py', '--settings', str(self.settings), 'ask-close',
                     '--message-id', '42', '--decision', 'Merged by hand')
        self.assertIn('"reason": "telegram_rejected_not_sent_400"', err.getvalue())
        self.assertTrue(store.get_ask(ask_id)['decided'])

    def test_flag_errors_exit_through_the_parser(self):
        cases = [['ask-close', '--decision', 'x'],
                 ['ask-close', '--message-id', '42', '--ask-key', 'k', '--decision', 'x'],
                 ['ask-close', '--message-id', '42'],
                 ['ask-close', '--message-id', '0', '--decision', 'x'],
                 ['ask-close', '--message-id', '-3', '--decision', 'x'],
                 ['ask-close', '--message-id', 'abc', '--decision', 'x'],
                 ['send', '--body-file', 'x', '--idempotency-key', 'k', '--decision', 'x'],
                 ['reply-current', '--body-file', 'x', '--idempotency-key', 'k', '--message-id', '42'],
                 ['run', '--ask-key', 'k']]
        for argv in cases:
            with self.subTest(argv=argv), \
                    patch.object(b, 'load_settings', side_effect=AssertionError('settings are never read')), \
                    patch('sys.stderr', new_callable=io.StringIO) as err, \
                    self.assertRaises(SystemExit) as raised:
                run_main(self, 'bridge.py', *argv)
            self.assertEqual(raised.exception.code, 2)
            self.assertIn('error:', err.getvalue())
        parser = b._build_parser()
        args = parser.parse_args(['ask-close', '--ask-key', 'k', '--decision', ''])
        b._validate_attachment_flags(parser, args)  # an empty decision is the bridge's decision_required

    def test_the_pack_command_closes_a_recorded_ask(self):
        store, ask_id = self.record_ask()
        bin_dir = self.root / 'bin'
        write_fake_curl(bin_dir, f'#!{sys.executable}\nimport json, sys, uuid\nfrom pathlib import Path\n'
                        f'Path({str(self.root)!r}, "curl-" + uuid.uuid4().hex).write_text(sys.stdin.read())\n'
                        'print(json.dumps({"ok": True, "result": {"message_id": 42, "chat": {"id": 123}}}))\n'
                        'print("200 0.010000 0.020000 0", end="")\n')
        env = {k: v for k, v in os.environ.items() if not k.startswith('GC_')}
        env.update(PATH=f'{bin_dir}:{env["PATH"]}', TELEGRAM_BRIDGE_SETTINGS=str(self.settings))
        result = subprocess.run(['sh', str(PACK / 'commands' / 'ask-close' / 'run.sh'),
                                 '--message-id', '42', '--decision', 'Typed: merge it'],
                                env=env, cwd=self.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'Telegram ask closed; message ID 42\n')
        [config] = [p.read_text() for p in self.root.glob('curl-*')]
        self.assertIn('/editMessageText"', config)
        self.assertIn('Typed: merge it', config)
        ask = store.get_ask(ask_id)
        self.assertEqual((ask['answered'], ask['decided'], ask['answer']), (True, True, 'Typed: merge it'))


if __name__ == '__main__':
    unittest.main()
