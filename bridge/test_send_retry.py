"""Outbound sends that never left the host: curl's exit code and transfer
metrics classified in one place, a not-sent attempt retried inside its
reservation (3 attempts, 1 s and 3 s apart, within the send budget), a
definite not-sent receipt whose ledger row is not final, and ambiguity kept
for every send that may have reached Telegram. Fake curl, fake Telegram,
fake clock; no network and no real sleep."""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
from test_bridge import BridgeTestCase, MENU, run_main, write_settings  # noqa: E402

URL = 'https://api.telegram.org/botSECRET/sendMessage'
OK = (200, dict(ok=True, result=dict(message_id=77, chat=dict(id=123))))
NOT_SENT = 'transport_unavailable_not_sent'
AMBIGUOUS = 'ambiguous_delivery_requires_manual_reconciliation'
ATTEMPT_LINE = re.compile(r'telegram (\w+) attempt ([123])/3 (not sent|unconfirmed): '
                          r'curl exit (-|[0-9]+), phase (dns|connect|tls|after-send|http-5xx|unknown), ([0-9]+) ms')


def metrics(http=0, connect=0.0, appconnect=0.0, uploaded=0):
    return f'\n{http:03d} {connect:.6f} {appconnect:.6f} {uploaded}'


def not_sent(code=35, phase='tls'):
    return b.RequestNotSent('network request not sent', code, phase)


def unconfirmed(code=56, phase='after-send'):
    return b.RequestUnconfirmed('network request failed; outcome unconfirmed', code, phase)


class TransportClassificationTests(unittest.TestCase):
    """http_json and http_multipart read curl's exit code and write-out."""

    def run_curl(self, code, stdout, url=URL, multipart=False):
        with patch.object(b.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], code, stdout, 'curl: (99) SECRET hello body')
            if multipart:
                return b.http_multipart(url, {'chat_id': '123', 'caption': 'hello body'}, 'photo', '/tmp/x.png'), run
            return b.http_json(url, {'chat_id': 123, 'text': 'hello body'}), run

    def failure(self, code, stdout, **kwargs):
        with self.assertRaises(b.TransportFailure) as caught:
            self.run_curl(code, stdout, **kwargs)
        return caught.exception

    def test_dns_connect_and_tls_failures_are_not_sent(self):
        for code, stdout, phase in ((6, metrics(), 'dns'), (7, metrics(), 'connect'), (35, metrics(connect=0.08), 'tls'),
                                    (28, metrics(), 'connect'), (28, metrics(connect=0.08), 'tls'),
                                    (7, '', 'connect'), (35, '', 'tls')):
            for multipart in (False, True):
                with self.subTest(code=code, stdout=stdout, multipart=multipart):
                    error = self.failure(code, stdout, multipart=multipart)
                    self.assertIsInstance(error, b.RequestNotSent)
                    self.assertEqual((error.exit_code, error.phase), (code, phase))
                    self.assertIn(f'curl exit {code}, phase {phase}', str(error))

    def test_anything_that_may_have_reached_telegram_is_unconfirmed(self):
        cases = ((28, metrics(connect=0.08, appconnect=0.2), 'after-send'),   # timed out after the handshake
                 (28, metrics(connect=0.08, uploaded=40), 'after-send'),      # bytes went out
                 (28, '', 'after-send'),                                       # no metrics to prove otherwise
                 (52, metrics(connect=0.08, appconnect=0.2, uploaded=40), 'after-send'),
                 (56, metrics(connect=0.08, appconnect=0.2, uploaded=40), 'after-send'),
                 (92, metrics(connect=0.08, appconnect=0.2, uploaded=40), 'after-send'),
                 (16, metrics(connect=0.08, appconnect=0.2), 'after-send'),
                 (55, metrics(), 'unknown'),                                   # an unlisted code is never "not sent"
                 (60, metrics(connect=0.08), 'unknown'),
                 (35, metrics(connect=0.08, appconnect=0.2), 'after-send'),   # 35 after a completed handshake
                 (7, metrics(uploaded=1), 'after-send'))
        for code, stdout, phase in cases:
            for multipart in (False, True):
                with self.subTest(code=code, stdout=stdout, multipart=multipart):
                    error = self.failure(code, stdout, multipart=multipart)
                    self.assertIsInstance(error, b.RequestUnconfirmed)
                    self.assertEqual((error.exit_code, error.phase), (code, phase))
                    self.assertIn('outcome unconfirmed', str(error))

    def test_a_timeout_on_plain_http_is_never_not_sent(self):
        # time_appconnect is 0 on every plain-HTTP transfer: it proves nothing there.
        error = self.failure(28, metrics(connect=0.0), url='http://127.0.0.1:8372/v0/city/c/extmsg/outbound')
        self.assertIsInstance(error, b.RequestUnconfirmed)

    def test_a_completed_transfer_returns_its_status_and_body(self):
        (status, data), run = self.run_curl(0, '{"ok": true}' + metrics(200, 0.01, 0.02, 40))
        self.assertEqual((status, data), (200, {'ok': True}))
        (status, data), run = self.run_curl(0, '{"ok": false, "error_code": 502}' + metrics(502, 0.01, 0.02, 40))
        self.assertEqual(status, 502)  # the send loop, not the transport, decides about a 5xx

    def test_unreadable_answers_after_exit_0_are_unconfirmed(self):
        for stdout, phase in (('<html>Bad Gateway</html>' + metrics(502, 0.01, 0.02, 40), 'http-5xx'),
                              ('not json' + metrics(200, 0.01, 0.02, 40), 'after-send'),
                              ('{"ok": true}\n200', 'after-send'),
                              ('{"ok": true}', 'after-send'),
                              ('x' * 2_000_001 + metrics(200, 0.01, 0.02, 40), 'after-send')):
            with self.subTest(stdout=stdout[:40]):
                error = self.failure(0, stdout)
                self.assertIsInstance(error, b.RequestUnconfirmed)
                self.assertEqual(error.phase, phase)

    def test_a_python_side_timeout_is_unconfirmed(self):
        with patch.object(b.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['curl'], 22)):
            with self.assertRaises(b.RequestUnconfirmed):
                b.http_json(URL, {'text': 'hello body'})

    def test_write_out_and_limits_reach_curl_and_errors_carry_no_secret(self):
        error = self.failure(35, metrics(connect=0.08))
        (_, _), run = self.run_curl(0, '{"ok": true}' + metrics(200, 0.01, 0.02, 40))
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index('--write-out') + 1],
                         '\n%{http_code} %{time_connect} %{time_appconnect} %{size_upload}')
        self.assertEqual(argv[argv.index('--connect-timeout') + 1], '5')
        self.assertEqual(argv[argv.index('--max-time') + 1], '20')
        self.assertNotIn('SECRET', repr(argv))
        for text in (str(error), repr(error)):
            for leak in ('SECRET', 'api.telegram.org', 'hello body', '(99)'):
                self.assertNotIn(leak, text)

    def test_a_fractional_attempt_timeout_reaches_curl_as_seconds(self):
        with patch.object(b.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"ok": true}' + metrics(200), '')
            b.http_json(URL, {}, timeout=11.25)
        argv, kwargs = run.call_args.args[0], run.call_args.kwargs
        self.assertEqual(argv[argv.index('--max-time') + 1], '11.250')
        self.assertEqual(kwargs['timeout'], 13.25)


class SendRetryTests(BridgeTestCase):
    # Own root/store fixture; the enterContext cleanup replaces tearDown.
    def tearDown(self):
        pass

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = b.Store(self.root / 'state.sqlite3', 10)
        self.answers, self.calls, self.sleeps, self.clock = [], [], [], [0.0]
        self.config = dict(bot_id=999, user_id=123, chat_id=123, bot_username='test_bot', city='test-city',
                           user_display_name='Pat', timezone='UTC', unit='test-city-telegram.service', menu=MENU)
        self.bridge = b.Bridge(self.config, self.store, 'SECRET', 'CAPABILITY', request=self.transport,
                               request_multipart=self.multipart_transport, sleep=self.sleep,
                               monotonic=lambda: self.clock[0])
        self.log = self.enterContext(patch('sys.stderr', new_callable=io.StringIO))

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock[0] += seconds

    def answer(self, method, payload, timeout):
        self.calls.append((method, payload, timeout))
        answer = self.answers.pop(0) if self.answers else OK
        if isinstance(answer, Exception):
            raise answer
        return answer

    def transport(self, url, body, timeout=20, **kwargs):
        return self.answer(url.rsplit('/', 1)[1], body, timeout)

    def multipart_transport(self, url, fields, file_field, file_path, timeout=20):
        return self.answer(url.rsplit('/', 1)[1], fields, timeout)

    def publish(self, key='send-1', text='hello body', **extra):
        return self.bridge.publish(dict(conversation=self.bridge.conversation, idempotency_key=key, text=text, **extra))

    def attachment(self):
        path = self.root / 'shot.png'
        path.write_bytes(b'png')
        return dict(path=str(path), kind='photo')

    def ledger(self, key):
        with closing(sqlite3.connect(self.store.path)) as db:
            row = db.execute('SELECT receipt FROM deliveries WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def attempt_lines(self):
        lines = [line for line in self.log.getvalue().splitlines() if line.startswith('telegram ')]
        for line in lines:
            self.assertRegex(line, ATTEMPT_LINE)
            self.assertIsNotNone(ATTEMPT_LINE.fullmatch(line), line)
        return lines

    def assert_not_sent(self, receipt):
        self.assertFalse(receipt['delivered'])
        self.assertEqual(receipt['failure_kind'], 'transient')
        self.assertEqual(receipt['metadata'], {'reason': NOT_SENT})

    def assert_ambiguous(self, receipt):
        self.assertFalse(receipt['delivered'])
        self.assertEqual(receipt['failure_kind'], 'permanent')
        self.assertEqual(receipt['metadata'], {'reason': AMBIGUOUS})

    def test_two_tls_cuts_then_success_deliver_in_three_attempts(self):
        self.answers = [not_sent(), not_sent()]
        receipt = self.publish()
        self.assertEqual((receipt['delivered'], receipt['message_id']), (True, '77'))
        self.assertEqual([method for method, _, _ in self.calls], ['sendMessage'] * 3)
        self.assertEqual(self.sleeps, [1, 3])
        self.assertEqual(self.ledger('send-1'), receipt)
        lines = self.attempt_lines()
        self.assertEqual(lines, ['telegram sendMessage attempt 1/3 not sent: curl exit 35, phase tls, 0 ms',
                                 'telegram sendMessage attempt 2/3 not sent: curl exit 35, phase tls, 0 ms'])
        self.assertNotIn('hello body', self.log.getvalue())

    def test_every_attempt_not_sent_is_a_definite_not_sent_that_the_same_key_sends_again(self):
        for code, phase in ((6, 'dns'), (7, 'connect'), (35, 'tls'), (28, 'connect')):
            with self.subTest(code=code):
                key, before = f'send-{code}', len(self.calls)
                self.sleeps.clear()
                self.answers = [not_sent(code, phase)] * 3
                receipt = self.publish(key)
                self.assert_not_sent(receipt)
                self.assertEqual(len(self.calls) - before, 3)
                self.assertEqual(self.sleeps, [1, 3])
                self.assertEqual(self.ledger(key), receipt)
                # Another body under the key is still a conflict, sending nothing.
                self.assertEqual(self.publish(key, 'another body')['metadata']['reason'], 'idempotency_key_conflict')
                self.assertEqual(len(self.calls) - before, 3)
                # The same key and body reserve the row again and send.
                again = self.publish(key)
                self.assertEqual((again['delivered'], again['message_id']), (True, '77'))
                self.assertEqual(len(self.calls) - before, 4)
                self.assertEqual(self.ledger(key), again)
                self.assertEqual(self.publish(key), again)
                self.assertEqual(len(self.calls) - before, 4)
                self.assertEqual(self.publish(key, 'another body')['metadata']['reason'], 'idempotency_key_conflict')

    def test_a_send_that_may_have_reached_telegram_is_tried_once_and_stays_ambiguous(self):
        cases = (unconfirmed(28, 'after-send'), unconfirmed(52), unconfirmed(56), unconfirmed(92),
                 unconfirmed(16), unconfirmed(55, 'unknown'), b.RequestUnconfirmed('network request timed out'),
                 b.TransportError('network response unconfirmed'),
                 (502, dict(ok=False, error_code=502, description='Bad Gateway')), (500, dict(ok=False)))
        for index, answer in enumerate(cases):
            with self.subTest(answer=answer):
                key, before = f'amb-{index}', len(self.calls)
                self.log.truncate(0)
                self.log.seek(0)
                self.answers = [answer, OK]
                receipt = self.publish(key)
                self.assert_ambiguous(receipt)
                self.assertEqual(len(self.calls) - before, 1)
                self.assertEqual(self.ledger(key), receipt)
                self.assertEqual(self.publish(key), receipt)
                self.assertEqual(len(self.calls) - before, 1)
                lines = self.attempt_lines()
                self.assertEqual(len(lines), 1)
                self.assertIn('attempt 1/3 unconfirmed', lines[0])
                self.answers.clear()
        self.assertEqual(self.sleeps, [])
        self.assertIn('curl exit 0, phase http-5xx', self.log.getvalue())

    def test_not_sent_then_ambiguous_ends_ambiguous(self):
        self.answers = [not_sent(), unconfirmed(56)]
        receipt = self.publish()
        self.assert_ambiguous(receipt)
        self.assertEqual((len(self.calls), self.sleeps), (2, [1]))
        self.assertEqual(self.publish(), receipt)
        self.assertEqual(len(self.calls), 2)

    def test_not_sent_then_a_4xx_is_the_definite_rejection(self):
        self.answers = [not_sent(), (403, dict(ok=False, error_code=403, description='Forbidden'))]
        receipt = self.publish()
        self.assertEqual(receipt['metadata'], {'reason': 'telegram_rejected_not_sent_403'})
        self.assertEqual(receipt['failure_kind'], 'permanent')
        self.assertEqual(self.publish(), receipt)
        self.assertEqual(len(self.calls), 2)

    def test_html_fallback_that_cannot_go_out_is_not_sent_and_resends_from_the_html(self):
        rejected = (400, dict(ok=False, error_code=400, description="Bad Request: can't parse entities"))
        self.answers = [rejected, not_sent(), not_sent(), not_sent()]
        receipt = self.publish(text='<b>hi</b>', format='html')
        self.assert_not_sent(receipt)
        self.assertEqual([body.get('parse_mode') for _, body, _ in self.calls], ['HTML', None, None, None])
        self.assertEqual(self.sleeps, [1, 3])
        again = self.publish(text='<b>hi</b>', format='html')
        self.assertTrue(again['delivered'])
        self.assertNotIn('format_fallback_reason', again)
        self.assertEqual(self.calls[-1][1]['parse_mode'], 'HTML')
        self.assertEqual(len(self.calls), 5)

    def test_html_not_sent_first_attempts_retry_the_html_before_any_fallback(self):
        rejected = (400, dict(ok=False, error_code=400, description="Bad Request: can't parse entities"))
        self.answers = [not_sent(), rejected, OK]
        receipt = self.publish(text='<b>hi</b>', format='html')
        self.assertTrue(receipt['delivered'])
        self.assertEqual(receipt['format_fallback_reason'], 'telegram_html_parse_rejected_400')
        self.assertEqual([body.get('parse_mode') for _, body, _ in self.calls], ['HTML', 'HTML', None])

    def test_attachment_sends_follow_the_same_rules(self):
        self.answers = [not_sent(), not_sent()]
        receipt = self.publish('file-1', 'caption', attachment=self.attachment())
        self.assertTrue(receipt['delivered'])
        self.assertEqual([method for method, _, _ in self.calls], ['sendPhoto'] * 3)
        self.answers = [not_sent()] * 3
        receipt = self.publish('file-2', 'caption', attachment=self.attachment())
        self.assert_not_sent(receipt)
        again = self.publish('file-2', 'caption', attachment=self.attachment())
        self.assertTrue(again['delivered'])
        self.assertEqual(len(self.calls), 7)
        for index, answer in enumerate((unconfirmed(56), unconfirmed(28), (502, dict(ok=False)))):
            self.answers = [answer, OK]
            key = f'file-amb-{index}'
            receipt = self.publish(key, 'caption', attachment=self.attachment())
            self.assert_ambiguous(receipt)
            self.assertEqual(self.publish(key, 'caption', attachment=self.attachment()), receipt)
        self.assertEqual(len(self.calls), 10)
        self.assertEqual(len(self.attempt_lines()), 2 + 3 + 3)

    def test_an_attachment_rejection_whose_text_fallback_cannot_go_out_is_not_sent(self):
        self.answers = [(400, dict(ok=False, error_code=400, description='Bad Request: wrong file')),
                        not_sent(), not_sent(), not_sent()]
        receipt = self.publish('file-1', 'caption', attachment=self.attachment())
        self.assert_not_sent(receipt)
        self.assertEqual([method for method, _, _ in self.calls], ['sendPhoto'] + ['sendMessage'] * 3)
        again = self.publish('file-1', 'caption', attachment=self.attachment())
        self.assertTrue(again['delivered'])
        self.assertEqual(self.calls[-1][0], 'sendPhoto')

    def test_command_messages_and_press_answers_follow_the_same_rules(self):
        action = dict(type='send', text='done', format='plain', plain=None)
        self.answers = [not_sent()] * 3
        self.assert_not_sent(self.bridge._command_message('command:9:0', action))
        self.assertTrue(self.bridge._command_message('command:9:0', action)['delivered'])
        self.answers = [unconfirmed(56), OK]
        receipt = self.bridge._command_message('command:9:1', action)
        self.assert_ambiguous(receipt)
        self.assertEqual(self.bridge._command_message('command:9:1', action), receipt)
        self.assertEqual(len(self.calls), 5)

        accepted = (200, dict(ok=True, result=True))
        self.answers = [not_sent(), not_sent(), not_sent()]
        self.assert_not_sent(self.bridge._answer_callback(9, 'query-9'))
        self.answers = [not_sent(), accepted]
        self.assertTrue(self.bridge._answer_callback(9, 'query-9')['delivered'])
        self.answers = [(502, dict(ok=False)), accepted]
        receipt = self.bridge._answer_callback(10, 'query-10')
        self.assert_ambiguous(receipt)
        self.assertEqual(self.bridge._answer_callback(10, 'query-10'), receipt)
        self.assertEqual([m for m, _, _ in self.calls][5:], ['answerCallbackQuery'] * 6)
        self.assertEqual(self.sleeps, [1, 3, 1, 3, 1])

    def test_attempts_share_the_send_budget(self):
        def slow(seconds):
            def answer(url, body, timeout=20, **kwargs):
                self.calls.append(('sendMessage', body, timeout))
                self.clock[0] += min(seconds, timeout)  # curl's --max-time cuts a longer one
                raise not_sent()
            return answer
        self.bridge.request = slow(5)
        self.assert_not_sent(self.publish('budget-1'))
        self.assertEqual([timeout for _, _, timeout in self.calls], [20, 19, 11])
        self.assertEqual(self.clock[0], 19)  # 3 x 5 s connect timeout + 4 s of waits
        self.calls.clear()
        self.clock[0] = 0.0
        self.bridge.request = slow(15)
        self.assert_not_sent(self.publish('budget-2'))
        # 15 s + 1 s wait leaves 9 s for the 2nd; after it, 3 s of wait would leave under 6 s.
        self.assertEqual([timeout for _, _, timeout in self.calls], [20, 9])
        self.assertLessEqual(self.clock[0], b.SEND_BUDGET)

    def test_a_saved_receipt_that_is_not_the_bridge_s_not_sent_one_is_final(self):
        request = dict(conversation=self.bridge.conversation, idempotency_key='old-1', text='hello body')
        fingerprint = b.hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        for reason, kind in ((NOT_SENT, 'permanent'), (AMBIGUOUS, 'transient'), ('telegram_rejected_not_sent_429', 'permanent')):
            with self.subTest(reason=reason, kind=kind):
                with closing(sqlite3.connect(self.store.path)) as db, db:
                    db.execute('DELETE FROM deliveries')
                saved = self.bridge.receipt(reason=reason, failure_kind=kind)
                self.assertIsNone(self.store.reserve('old-1', fingerprint, saved))
                self.assertEqual(self.bridge.publish(request), saved)
        self.assertEqual(self.calls, [])

    def test_probes_and_other_single_requests_are_not_retried(self):
        self.answers = [not_sent()]
        with self.assertRaises(b.RequestNotSent):
            self.bridge.probe()
        self.assertEqual((len(self.calls), self.sleeps), (1, []))

    def test_the_receipt_gc_reads_and_the_receipt_line_carry_failure_kind_transient(self):
        self.answers = [not_sent()] * 3
        receipt = self.publish()
        self.assertEqual(receipt['failure_kind'], 'transient')  # the wire field GC's adapter reads
        args = b._build_parser().parse_args(['send', '--body-file', 'x', '--idempotency-key', 'html-1',
                                             '--format', 'html'])
        self.answers = [not_sent()] * 3
        with self.assertRaises(b.TransportError) as caught:
            b._dispatch_outbound(self.bridge, args, '<b>hi</b>')
        self.assertIn('send again with the same key and body', str(caught.exception))
        relayed = []
        def fake_gc(path, body, method='POST', timeout=20):
            relayed.append(timeout)
            return {'Receipt': {'Delivered': False, 'FailureKind': 'transient', 'Metadata': {'reason': NOT_SENT}}}
        args = b._build_parser().parse_args(['send', '--body-file', 'x', '--idempotency-key', 'plain-1'])
        with patch.object(self.bridge, 'gc', fake_gc), self.assertRaises(b.TransportError):
            b._dispatch_outbound(self.bridge, args, 'hi')
        self.assertEqual(relayed, [b.GC_OUTBOUND_TIMEOUT])
        lines = [json.loads(line[len('GC_RECEIPT '):]) for line in self.log.getvalue().splitlines()
                 if line.startswith('GC_RECEIPT ')]
        self.assertEqual(lines, [dict(delivered=False, failure_kind='transient', reason=NOT_SENT)] * 2)


class SendRetryCliTests(unittest.TestCase):
    """reply-current through the real curl transport, curl faked in-process."""

    def test_reply_current_retries_tls_cuts_and_prints_a_transient_receipt_line(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        settings = write_settings(root)
        body = root / 'body.txt'
        body.write_text('<b>hello body</b>')
        state = dict(ok=False)
        configs, sleeps = [], []

        def fake_run(argv, input='', **kwargs):
            configs.append((argv, input))
            if state['ok']:
                payload = json.dumps({'ok': True, 'result': {'message_id': 42, 'chat': {'id': 123}}})
                return subprocess.CompletedProcess(argv, 0, payload + metrics(200, 0.01, 0.02, 90), '')
            return subprocess.CompletedProcess(argv, 35, metrics(connect=0.08), 'curl: (35) Send failure: Broken pipe')

        argv = ['bridge.py', '--settings', str(settings), 'reply-current', '--body-file', str(body),
                '--idempotency-key', 'cli-1', '--format', 'html']
        with patch.object(b.subprocess, 'run', fake_run), patch.object(b.time, 'sleep', sleeps.append), \
             patch('sys.stderr', new_callable=io.StringIO) as err:
            with self.assertRaisesRegex(b.TransportError, 'nothing was sent'):
                run_main(self, *argv)
        self.assertEqual((len(configs), sleeps), (3, [1, 3]))
        log = err.getvalue()
        self.assertIn('GC_RECEIPT {"delivered": false, "failure_kind": "transient", "reason": "transport_unavailable_not_sent"}', log)
        attempts = [line for line in log.splitlines() if line.startswith('telegram ')]
        self.assertEqual(len(attempts), 3)
        for line in attempts:
            self.assertRegex(line, r'^telegram sendMessage attempt [123]/3 not sent: curl exit 35, phase tls, [0-9]+ ms$')
        for leak in ('TESTTOKEN', 'hello body', 'api.telegram.org', 'Broken pipe', '123'):
            self.assertNotIn(leak, log.replace('"chat": {"id": 123}', ''))
        self.assertTrue(all('TESTTOKEN' not in repr(call_argv) for call_argv, _ in configs))
        self.assertTrue(all('TESTTOKEN' in config for _, config in configs))

        state['ok'] = True
        with patch.object(b.subprocess, 'run', fake_run), patch.object(b.time, 'sleep', sleeps.append), \
             patch('sys.stderr', new_callable=io.StringIO), \
             patch('sys.stdout', new_callable=io.StringIO) as out:
            run_main(self, *argv)
        self.assertIn('Telegram delivery confirmed; message ID 42', out.getvalue())
        self.assertEqual(len(configs), 4)


if __name__ == '__main__':
    unittest.main()
