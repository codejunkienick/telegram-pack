"""Pinned messages (OPERATIONS.md "Pinned messages"): one bot message per slot,
sent silently and pinned once per generation, edited in place after, with
the fake Telegram, temporary directories and no network."""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
from test_bridge import BridgeTestCase, run_main, write_fake_curl, write_settings  # noqa: E402

PACK = Path(__file__).resolve().parents[1]
NOT_MODIFIED = 400, dict(ok=False, error_code=400, description='Bad Request: message is not modified: specified '
                         'new message content and reply markup are exactly the same')
NOT_FOUND = 400, dict(ok=False, error_code=400, description='Bad Request: message to edit not found')
PIN_NOT_FOUND = 400, dict(ok=False, error_code=400, description='Bad Request: message to pin not found')
REJECTED = 400, dict(ok=False, error_code=400, description='Bad Request: chat not found')
UNCONFIRMED = 502, dict(ok=False, error_code=502)
HTML_REJECTED = 400, dict(ok=False, error_code=400, description="Bad Request: can't parse entities: x")
PREVIEW_OFF = {'is_disabled': True}


def not_sent():
    return b.RequestNotSent('network request not sent', 7, 'connect')


class PinTests(BridgeTestCase):
    """Bridge.pin and Bridge.unpin against a fake Telegram."""

    def setUp(self):
        super().setUp()
        self.answers = {}  # method -> answers used in order before the default
        self.next_id = 500
        self.bridge.request = self.pin_transport
        self.bridge.sleep = lambda delay: None

    def pin_transport(self, url, body, *args, **kwargs):
        method = url.rsplit('/', 1)[1]
        self.calls.append((method, body))
        if self.answers.get(method):
            answer = self.answers[method].pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if method == 'sendMessage':
            self.next_id += 1
            return 200, dict(ok=True, result=dict(message_id=self.next_id, chat=dict(id=123)))
        if method == 'editMessageText':
            return 200, dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
        if method in ('pinChatMessage', 'unpinChatMessage'):
            return 200, dict(ok=True, result=True)
        raise AssertionError('unexpected Telegram method ' + method)

    def methods(self):
        return [method for method, _ in self.calls]

    def bodies(self, method):
        return [body for name, body in self.calls if name == method]

    def ledger(self):
        with closing(sqlite3.connect(self.path)) as db:
            return {key: json.loads(receipt) for key, receipt in db.execute('SELECT key, receipt FROM deliveries')}

    def state(self, slot='waiting'):
        return {k: v for k, v in self.store.pin_state(slot).items() if k != 'fingerprint'}

    def assert_pinned(self, receipt, message_id, revision, changed=True):
        self.assertTrue(receipt['delivered'], receipt)
        self.assertEqual((receipt['message_id'], receipt['revision'], receipt['changed']),
                         (str(message_id), revision, changed))

    def assert_failed(self, receipt, step, reason):
        self.assertFalse(receipt['delivered'])
        self.assertEqual((receipt.get('step'), receipt['metadata']['reason']), (step, reason))

    def test_the_first_update_sends_silently_and_pins(self):
        self.assert_pinned(self.bridge.pin('waiting', 'Nothing waiting'), 501, 0)
        self.assertEqual(self.calls, [
            ('sendMessage', dict(chat_id=123, text='Nothing waiting', link_preview_options=PREVIEW_OFF,
                                 disable_notification=True)),
            ('pinChatMessage', dict(chat_id=123, message_id=501, disable_notification=True))])
        self.assertEqual(sorted(self.ledger()), ['pin:waiting:g1:message', 'pin:waiting:g1:pin'])
        self.assertEqual(self.state(), dict(slot='waiting', generation=1, message_id=501, revision=0))

    def test_the_same_body_and_format_make_no_call(self):
        self.bridge.pin('waiting', 'Nothing waiting')
        before = list(self.calls)
        self.assert_pinned(self.bridge.pin('waiting', 'Nothing waiting'), 501, 0, changed=False)
        self.assertEqual(self.calls, before)
        # The format is part of what was applied: the same text as HTML edits.
        self.assert_pinned(self.bridge.pin('waiting', 'Nothing waiting', 'html'), 501, 1)
        self.assertEqual(self.methods()[-1], 'editMessageText')

    def test_a_new_body_edits_the_live_message_under_r1(self):
        self.bridge.pin('waiting', 'Nothing waiting')
        self.calls.clear()
        self.assert_pinned(self.bridge.pin('waiting', '2 asks wait'), 501, 1)
        self.assertEqual(self.calls, [('editMessageText', dict(chat_id=123, message_id=501, text='2 asks wait',
                                                               link_preview_options=PREVIEW_OFF))])
        self.assertIn('pin:waiting:g1:r1', self.ledger())
        self.assertEqual(self.state()['revision'], 1)

    def test_a_then_b_then_a_edits_every_time(self):
        self.bridge.pin('waiting', 'start')
        for text in ('A', 'B', 'A'):
            self.bridge.pin('waiting', text)
        self.assertEqual([body['text'] for body in self.bodies('editMessageText')], ['A', 'B', 'A'])
        self.assertEqual(sorted(k for k in self.ledger() if ':r' in k),
                         ['pin:waiting:g1:r1', 'pin:waiting:g1:r2', 'pin:waiting:g1:r3'])
        self.assertEqual(self.methods().count('sendMessage'), 1)
        self.assertEqual(self.methods().count('pinChatMessage'), 1)

    def test_message_is_not_modified_counts_as_applied(self):
        self.bridge.pin('waiting', 'A')
        self.answers['editMessageText'] = [NOT_MODIFIED]
        self.assert_pinned(self.bridge.pin('waiting', 'B'), 501, 1)
        before = list(self.calls)
        self.assert_pinned(self.bridge.pin('waiting', 'B'), 501, 1, changed=False)
        self.assertEqual(self.calls, before)

    def test_message_to_edit_not_found_sends_and_pins_a_new_generation(self):
        self.bridge.pin('waiting', 'A')
        self.answers['editMessageText'] = [NOT_FOUND]
        self.calls.clear()
        self.assert_pinned(self.bridge.pin('waiting', 'B'), 502, 0)
        self.assertEqual(self.methods(), ['editMessageText', 'sendMessage', 'pinChatMessage'])
        self.assertEqual(self.bodies('sendMessage')[0]['text'], 'B')
        self.assertEqual(self.bodies('pinChatMessage'), [dict(chat_id=123, message_id=502, disable_notification=True)])
        self.assertEqual(self.state(), dict(slot='waiting', generation=2, message_id=502, revision=0))
        self.assertTrue({'pin:waiting:g2:message', 'pin:waiting:g2:pin'} <= set(self.ledger()))
        before = list(self.calls)
        self.assert_pinned(self.bridge.pin('waiting', 'B'), 502, 0, changed=False)
        self.assertEqual(self.calls, before)

    def test_an_ambiguous_first_send_fails_and_a_rerun_sends_nothing(self):
        self.answers['sendMessage'] = [UNCONFIRMED]
        with patch('sys.stderr', new_callable=io.StringIO):
            first = self.bridge.pin('waiting', 'A')
        self.assert_failed(first, 'send', 'ambiguous_delivery_requires_manual_reconciliation')
        self.assertIsNone(self.state()['message_id'])
        for text in ('A', 'B'):
            self.assertEqual(self.bridge.pin('waiting', text), first)
        self.assertEqual(self.methods(), ['sendMessage'])

    def test_a_rejected_first_send_fails_and_the_next_update_starts_a_new_generation(self):
        self.answers['sendMessage'] = [REJECTED]
        self.assert_failed(self.bridge.pin('waiting', 'A'), 'send', 'telegram_rejected_not_sent_400')
        self.assertEqual(self.state(), dict(slot='waiting', generation=2, message_id=None, revision=0))
        self.assert_pinned(self.bridge.pin('waiting', 'A'), 501, 0)
        self.assertIn('pin:waiting:g2:message', self.ledger())

    def test_a_not_sent_first_send_is_sent_by_the_next_update(self):
        self.answers['sendMessage'] = [not_sent() for _ in range(b.SEND_ATTEMPTS)]
        with patch('sys.stderr', new_callable=io.StringIO):
            self.assert_failed(self.bridge.pin('waiting', 'A'), 'send', b.NOT_SENT_REASON)
        self.assert_pinned(self.bridge.pin('waiting', 'A'), 501, 0)
        self.assertEqual(self.state()['generation'], 1)

    def test_a_pin_that_failed_after_a_confirmed_send_is_retried_first(self):
        for index, answer in enumerate((REJECTED, UNCONFIRMED, not_sent())):
            with self.subTest(answer=answer):
                slot = f'slot-{index}'
                self.answers['pinChatMessage'] = [answer] * (b.SEND_ATTEMPTS if isinstance(answer, Exception) else 1)
                with patch('sys.stderr', new_callable=io.StringIO):
                    failed = self.bridge.pin(slot, 'A')
                self.assertFalse(failed['delivered'])
                self.assertEqual(failed['step'], 'pin')
                sent = failed['message_id']
                self.assertEqual(str(self.store.pin_state(slot)['message_id']), sent)
                self.calls.clear()
                self.assert_pinned(self.bridge.pin(slot, 'A'), sent, 0)
                self.assertEqual(self.calls, [('pinChatMessage', dict(chat_id=123, message_id=int(sent),
                                                                      disable_notification=True))])
                self.assertTrue(self.ledger()[f'pin:{slot}:g1:pin']['delivered'])

    def test_a_pin_rejected_as_not_found_sends_and_pins_a_new_generation(self):
        # The user deleted the message before its pin landed: first an
        # unconfirmed pin, then the next update's pin finds it gone.
        self.answers['pinChatMessage'] = [(502, dict(ok=False, error_code=502)), PIN_NOT_FOUND]
        with patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(self.bridge.pin('waiting', 'A')['step'], 'pin')
        self.calls.clear()
        self.assert_pinned(self.bridge.pin('waiting', 'A'), 502, 0)
        self.assertEqual(self.methods(), ['pinChatMessage', 'sendMessage', 'pinChatMessage'])
        self.assertEqual(self.bodies('pinChatMessage')[-1], dict(chat_id=123, message_id=502, disable_notification=True))
        self.assertEqual(self.state(), dict(slot='waiting', generation=2, message_id=502, revision=0))
        before = list(self.calls)
        self.assert_pinned(self.bridge.pin('waiting', 'A'), 502, 0, changed=False)
        self.assertEqual(self.calls, before)

    def test_a_message_gone_twice_in_one_run_fails_with_the_generation_ended(self):
        self.answers['pinChatMessage'] = [PIN_NOT_FOUND, PIN_NOT_FOUND]
        failed = self.bridge.pin('waiting', 'A')
        self.assert_failed(failed, 'pin', 'telegram_message_to_pin_not_found')
        self.assertEqual(self.methods(), ['sendMessage', 'pinChatMessage'] * 2)
        self.assertEqual(self.state(), dict(slot='waiting', generation=3, message_id=None, revision=0))
        self.assert_pinned(self.bridge.pin('waiting', 'A'), 503, 0)

    def test_unpin_ends_an_unconfirmed_first_send_so_the_next_update_sends_anew(self):
        for answers in ([(502, dict(ok=False, error_code=502))], [not_sent() for _ in range(b.SEND_ATTEMPTS)]):
            with self.subTest(answer=answers[0]):
                slot = f'slot-{len(answers)}'
                self.answers['sendMessage'] = answers
                with patch('sys.stderr', new_callable=io.StringIO):
                    self.assertEqual(self.bridge.pin(slot, 'A')['step'], 'send')
                self.calls.clear()
                # The operator checked the chat (and deleted any copy), then:
                ended = self.bridge.unpin(slot)
                self.assertTrue(ended['delivered'])
                self.assertEqual((ended['message_id'], ended['ended_generation']), (None, 1))
                self.assertEqual(self.calls, [])
                self.assertEqual(self.state(slot), dict(slot=slot, generation=2, message_id=None, revision=0))
                again = self.bridge.unpin(slot)
                self.assertNotIn('ended_generation', again)
                sent = self.bridge.pin(slot, 'A')
                self.assertTrue(sent['delivered'])
                self.assertEqual(self.methods(), ['sendMessage', 'pinChatMessage'])
                self.assertIn(f'pin:{slot}:g2:message', self.ledger())
        self.assertNotIn('ended_generation', self.bridge.unpin('never-used'))

    def test_an_unconfirmed_edit_is_edited_again_by_the_next_update(self):
        self.bridge.pin('waiting', 'A')
        self.answers['editMessageText'] = [UNCONFIRMED]
        with patch('sys.stderr', new_callable=io.StringIO):
            self.assert_failed(self.bridge.pin('waiting', 'B'), 'edit',
                               'ambiguous_delivery_requires_manual_reconciliation')
        # Even the last applied body edits again: the message may show B.
        self.assert_pinned(self.bridge.pin('waiting', 'A'), 501, 2)
        self.assertEqual([body['text'] for body in self.bodies('editMessageText')], ['B', 'A'])

    def test_a_crash_after_the_confirmed_send_never_sends_a_second_message(self):
        with patch.object(self.store, 'set_pin', side_effect=RuntimeError('crash')), \
                self.assertRaises(RuntimeError):
            self.bridge.pin('waiting', 'A')
        self.assertIsNone(self.state()['message_id'])
        self.calls.clear()
        self.assert_pinned(self.bridge.pin('waiting', 'B'), 501, 1)
        # The saved send's body is unknown, so it is brought to B by an edit.
        self.assertEqual(self.methods(), ['pinChatMessage', 'editMessageText'])
        self.assertEqual(self.bodies('editMessageText')[0]['text'], 'B')

    def test_unpin_unpins_and_the_next_update_is_a_new_generation(self):
        self.bridge.pin('waiting', 'A')
        self.calls.clear()
        unpinned = self.bridge.unpin('waiting')
        self.assertTrue(unpinned['delivered'])
        self.assertEqual(unpinned['message_id'], '501')
        self.assertEqual(self.calls, [('unpinChatMessage', dict(chat_id=123, message_id=501))])
        self.assertEqual(self.state(), dict(slot='waiting', generation=2, message_id=None, revision=0))
        nothing = self.bridge.unpin('waiting')
        self.assertTrue(nothing['delivered'])
        self.assertIsNone(nothing['message_id'])
        self.assertEqual(len(self.calls), 1)
        self.assert_pinned(self.bridge.pin('waiting', 'A'), 502, 0)
        self.assertEqual(self.methods()[1:], ['sendMessage', 'pinChatMessage'])

    def test_an_unconfirmed_unpin_keeps_the_message_and_is_attempted_again(self):
        self.bridge.pin('waiting', 'A')
        self.answers['unpinChatMessage'] = [UNCONFIRMED]
        with patch('sys.stderr', new_callable=io.StringIO):
            failed = self.bridge.unpin('waiting')
        self.assertEqual((failed['step'], failed['message_id']), ('unpin', '501'))
        self.assertEqual(self.state()['message_id'], 501)
        self.assertTrue(self.bridge.unpin('waiting')['delivered'])
        self.assertEqual(self.methods().count('unpinChatMessage'), 2)
        self.assertIsNone(self.state()['message_id'])

    def test_an_update_after_an_unconfirmed_unpin_pins_again_before_unchanged(self):
        self.bridge.pin('waiting', 'A')
        self.answers['unpinChatMessage'] = [UNCONFIRMED]  # applied, its answer lost
        with patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(self.bridge.unpin('waiting')['step'], 'unpin')
        del self.calls[:]
        self.assert_pinned(self.bridge.pin('waiting', 'A'), 501, 0)
        self.assertEqual(self.methods(), ['pinChatMessage'])
        self.assertEqual(self.ledger()['pin:waiting:g1:unpin']['metadata']['reason'], 'superseded_by_pin')
        del self.calls[:]
        self.assert_pinned(self.bridge.pin('waiting', 'A'), 501, 0, changed=False)
        self.assertEqual(self.calls, [])
        # The --unpin after that re-pin unpins again rather than replaying a receipt.
        self.assertTrue(self.bridge.unpin('waiting')['delivered'])
        self.assertEqual(self.methods(), ['unpinChatMessage'])
        self.assertEqual(self.state(), dict(slot='waiting', generation=2, message_id=None, revision=0))

    def test_a_crash_after_a_delivered_unpin_still_unpins_after_a_re_pin(self):
        self.bridge.pin('waiting', 'A')
        with patch.object(self.store, 'set_pin', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.bridge.unpin('waiting')
        self.assertTrue(self.ledger()['pin:waiting:g1:unpin']['delivered'])
        self.assertEqual(self.state()['message_id'], 501)
        del self.calls[:]
        self.assert_pinned(self.bridge.pin('waiting', 'A'), 501, 0)
        self.assertEqual(self.methods(), ['pinChatMessage'])
        self.assertTrue(self.bridge.unpin('waiting')['delivered'])
        self.assertEqual(self.methods(), ['pinChatMessage', 'unpinChatMessage'])
        self.assertIsNone(self.state()['message_id'])

    def test_a_bad_slot_refuses_before_anything(self):
        for slot in ('', 'Waiting', 'a b', 'a/b', '../x', 'x' * 33, None):
            with self.subTest(slot=slot):
                self.assertEqual(self.bridge.pin(slot, 'A')['metadata']['reason'], 'pin_slot_invalid')
                self.assertEqual(self.bridge.unpin(slot)['metadata']['reason'], 'pin_slot_invalid')
        self.assertEqual(self.calls, [])
        self.assertEqual(list(Path(self.tmp.name).glob('telegram-pin-*')), [])
        self.assertTrue(b.Bridge.PIN_SLOT_RE.fullmatch('x' * 32))

    def test_a_body_over_the_limit_or_bad_html_refuses_whole(self):
        cases = [('<b>' + 'x' * 4097 + '</b>', 'html', 'text_required_maximum_4096_utf16_units'),
                 ('x' * 4097, 'plain', 'text_required_maximum_4096_utf16_units'),
                 ('  \n', 'plain', 'text_required_maximum_4096_utf16_units'),
                 ('<b>open', 'html', 'html_tags_unbalanced'),
                 ('A', 'markdown', 'format_unsupported')]
        for text, fmt, reason in cases:
            with self.subTest(fmt=fmt, text=text[:12]):
                self.assertEqual(self.bridge.pin('waiting', text, fmt)['metadata']['reason'], reason)
        self.assertEqual(self.calls, [])
        self.assert_pinned(self.bridge.pin('waiting', '<b>' + 'x' * 4096 + '</b>', 'html'), 501, 0)

    def test_html_bodies_keep_the_one_plain_fallback(self):
        self.answers['sendMessage'] = [HTML_REJECTED]
        self.assert_pinned(self.bridge.pin('waiting', '<b>A</b> &amp; B', 'html'), 501, 0)
        first, second = self.bodies('sendMessage')
        self.assertEqual((first['text'], first['parse_mode']), ('<b>A</b> &amp; B', 'HTML'))
        self.assertEqual(second, dict(chat_id=123, text='A & B', link_preview_options=PREVIEW_OFF,
                                      disable_notification=True))
        self.answers['editMessageText'] = [HTML_REJECTED]
        receipt = self.bridge.pin('waiting', '<i>C</i>', 'html')
        self.assert_pinned(receipt, 501, 1)
        first, second = self.bodies('editMessageText')
        self.assertEqual((first['text'], first['parse_mode']), ('<i>C</i>', 'HTML'))
        self.assertEqual(second, dict(chat_id=123, message_id=501, text='C', link_preview_options=PREVIEW_OFF))

    def test_the_slot_lock_is_owner_only_in_the_state_directory(self):
        self.bridge.pin('waiting', 'A')
        lock = Path(self.tmp.name) / 'telegram-pin-waiting.lock'
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        self.assertNotIn('telegram-bot-', lock.name)

    def test_two_concurrent_updates_of_one_slot_send_one_message(self):
        gate = threading.Lock()

        def slow_transport(url, body, *args, **kwargs):
            if url.endswith('/sendMessage'):
                time.sleep(0.2)  # the other caller must wait on the slot lock meanwhile
            with gate:
                return self.pin_transport(url, body)
        bridges = [b.Bridge(self.config, b.Store(self.path, 10), 'SECRET', 'CAPABILITY', request=slow_transport,
                            sleep=lambda delay: None) for _ in range(2)]
        results, start = [None, None], threading.Barrier(2)

        def update(index):
            start.wait()
            results[index] = bridges[index].pin('waiting', 'A')
        threads = [threading.Thread(target=update, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(self.methods(), ['sendMessage', 'pinChatMessage'])
        self.assertEqual(sorted(result['changed'] for result in results), [False, True])
        self.assertEqual({result['message_id'] for result in results}, {'501'})


class PinCliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.settings = write_settings(self.root)
        self.body = self.root / 'body.txt'
        self.body.write_text('Nothing waiting')

    def answer(self, url, body):
        if url.endswith('/sendMessage'):
            return 200, {'ok': True, 'result': {'message_id': 42, 'chat': {'id': 123}}}
        if url.endswith(('/pinChatMessage', '/unpinChatMessage')):
            return 200, {'ok': True, 'result': True}
        raise AssertionError('unexpected curl invocation ' + url)

    def cli(self, *argv):
        """Run main() pin with the temporary settings and curl faked
        in-process; returns stdout, and keeps stderr and each request."""
        self.requests = []

        def fake_run(argv, input='', **kwargs):
            config = dict(line.split(' = ', 1) for line in input.splitlines() if ' = ' in line)
            url = json.loads(config['url'])
            body = json.loads(json.loads(config['data']))
            self.requests.append((url.rsplit('/', 1)[1], body))
            status, payload = self.answer(url, body)
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload) + f'\n{status} 0.010000 0.020000 0', '')
        with patch.object(b.subprocess, 'run', fake_run), \
                patch('sys.stdout', new_callable=io.StringIO) as out, \
                patch('sys.stderr', new_callable=io.StringIO) as err:
            try:
                run_main(self, 'bridge.py', '--settings', str(self.settings), 'pin', *argv)
            finally:
                self.err = err.getvalue()
        return out.getvalue()

    def test_pin_prints_each_outcome(self):
        self.assertEqual(self.cli('--slot', 'waiting', '--body-file', str(self.body)),
                         'pinned message ID 42 (slot waiting, revision 0)\n')
        self.assertEqual([method for method, _ in self.requests], ['sendMessage', 'pinChatMessage'])
        self.assertEqual(self.cli('--slot', 'waiting', '--body-file', str(self.body)),
                         'pin waiting unchanged; message ID 42\n')
        self.assertEqual(self.requests, [])
        self.assertEqual(self.cli('--slot', 'waiting', '--unpin'), 'unpinned message ID 42 (slot waiting)\n')
        self.assertEqual(self.requests, [('unpinChatMessage', dict(chat_id=123, message_id=42))])
        self.assertEqual(self.cli('--slot', 'waiting', '--unpin'), 'pin waiting not pinned\n')
        self.assertEqual(self.requests, [])

    def test_unpin_reports_an_ended_unconfirmed_send(self):
        self.answer = lambda url, body: (502, {'ok': False, 'error_code': 502})
        with self.assertRaisesRegex(b.TransportError, 'Telegram send not confirmed: '
                                    'ambiguous_delivery_requires_manual_reconciliation'):
            self.cli('--slot', 'waiting', '--body-file', str(self.body))
        self.assertEqual(self.cli('--slot', 'waiting', '--unpin'),
                         'pin waiting not pinned; unconfirmed send of generation 1 ended, '
                         'the next update sends a new message\n')
        self.assertEqual(self.requests, [])
        self.assertTrue((self.root / 'state' / 'telegram-pin-waiting.lock').exists())

    def test_a_failed_update_prints_the_receipt_line_and_fails(self):
        self.answer = lambda url, body: (400, {'ok': False, 'error_code': 400, 'description': 'Bad Request: x'})
        with self.assertRaisesRegex(b.TransportError,
                                    r'^pin waiting: Telegram send not confirmed: telegram_rejected_not_sent_400$'):
            self.cli('--slot', 'waiting', '--body-file', str(self.body))
        self.assertIn('GC_RECEIPT {"delivered": false, "failure_kind": "permanent", '
                      '"reason": "telegram_rejected_not_sent_400"}', self.err)

    def test_flag_errors_exit_through_the_parser(self):
        body = str(self.body)
        cases = [['pin', '--body-file', body],
                 ['pin', '--slot', 'Waiting', '--body-file', body],
                 ['pin', '--slot', 'x' * 33, '--unpin'],
                 ['pin', '--slot', 'waiting'],
                 ['pin', '--slot', 'waiting', '--body-file', body, '--unpin'],
                 ['pin', '--slot', 'waiting', '--unpin', '--format', 'html'],
                 ['send', '--body-file', body, '--idempotency-key', 'k', '--slot', 'waiting'],
                 ['run', '--unpin']]
        for argv in cases:
            with self.subTest(argv=argv), \
                    patch.object(b, 'load_settings', side_effect=AssertionError('settings are never read')), \
                    patch('sys.stderr', new_callable=io.StringIO) as err, \
                    self.assertRaises(SystemExit) as raised:
                run_main(self, 'bridge.py', *argv)
            self.assertEqual(raised.exception.code, 2)
            self.assertIn('error:', err.getvalue())

    def test_the_pack_command_forwards_its_arguments(self):
        bin_dir = self.root / 'bin'
        write_fake_curl(bin_dir, f'#!{sys.executable}\nimport json, sys, uuid\nfrom pathlib import Path\n'
                        'config = sys.stdin.read()\n'
                        f'Path({str(self.root)!r}, "curl-" + uuid.uuid4().hex).write_text(config)\n'
                        'result = True if "ChatMessage" in config else {"message_id": 42, "chat": {"id": 123}}\n'
                        'print(json.dumps({"ok": True, "result": result}))\n'
                        'print("200 0.010000 0.020000 0", end="")\n')
        env = {k: v for k, v in os.environ.items() if not k.startswith('GC_')}
        env.update(PATH=f'{bin_dir}:{env["PATH"]}', TELEGRAM_BRIDGE_SETTINGS=str(self.settings))
        self.body.write_text('<b>2</b> asks wait')
        result = subprocess.run(['sh', str(PACK / 'commands' / 'pin' / 'run.sh'), '--slot', 'waiting',
                                 '--format', 'html', '--body-file', str(self.body)],
                                env=env, cwd=self.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'pinned message ID 42 (slot waiting, revision 0)\n')
        configs = sorted((p.read_text() for p in self.root.glob('curl-*')), key=lambda c: 'sendMessage' not in c)
        self.assertEqual(len(configs), 2)
        self.assertIn('/sendMessage"', configs[0])
        self.assertIn('<b>2</b> asks wait', configs[0])
        self.assertIn('\\"parse_mode\\": \\"HTML\\"', configs[0])
        self.assertIn('/pinChatMessage"', configs[1])


if __name__ == '__main__':
    unittest.main()
