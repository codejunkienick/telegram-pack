"""Silent sends: `silent: true` on publish() (`--silent` on send and
reply-current) puts disable_notification on every Telegram request of the
send, fallbacks included, joins the fingerprint only when true, and always
publishes directly in the CLI process, never through GC's relay."""
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
from test_bridge import BridgeTestCase, run_main, write_settings  # noqa: E402

PARSE_REJECTED = (400, dict(ok=False, error_code=400, description="Bad Request: can't parse entities: x"))
SENT_PHOTO = (200, dict(ok=True, result=dict(message_id=88, chat=dict(id=123))))


class SilentPublishTests(BridgeTestCase):
    def silent(self, key='silent-1', text='landed', **fields):
        return dict(self.publish(key=key, text=text), silent=True, **fields)

    def text_body(self, text, **fields):
        return dict(chat_id=123, text=text, link_preview_options={'is_disabled': True}, **fields)

    def fingerprint(self, key):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT fingerprint FROM deliveries WHERE key=?', (key,)).fetchone()[0]

    def test_silent_plain_send_carries_disable_notification(self):
        result = self.bridge.publish(self.silent(reply_to_message_id='5'))
        self.assertTrue(result['delivered'])
        self.assertEqual(self.calls, [('https://api.telegram.org/botSECRET/sendMessage', self.text_body(
            'landed', disable_notification=True, reply_parameters={'message_id': 5}))])
        self.assertIs(self.calls[0][1]['disable_notification'], True)

    def test_sends_without_silent_keep_todays_exact_bodies(self):
        self.assertTrue(self.bridge.publish(self.publish(key='plain', text='a status'))['delivered'])
        html = dict(self.publish(key='html', text='<b>a</b> status'), format='html')
        self.assertTrue(self.bridge.publish(html)['delivered'])
        explicit = dict(self.publish(key='false', text='a status'), silent=False)
        self.assertTrue(self.bridge.publish(explicit)['delivered'])
        self.assertEqual([body for _, body in self.calls], [
            self.text_body('a status'),
            self.text_body('<b>a</b> status', parse_mode='HTML'),
            self.text_body('a status'),
        ])

    def test_silent_html_parse_rejection_fallback_stays_silent(self):
        def transport(url, body, *args, **kwargs):
            self.calls.append((url, body))
            return PARSE_REJECTED if body.get('parse_mode') == 'HTML' else (
                200, dict(ok=True, result=dict(message_id=77, chat=dict(id=123))))
        self.bridge.request = transport
        result = self.bridge.publish(self.silent(text='<b>review</b> passed', format='html'))
        self.assertTrue(result['delivered'])
        self.assertEqual(result['format_fallback_reason'], 'telegram_html_parse_rejected_400')
        self.assertEqual([body for _, body in self.calls], [
            self.text_body('<b>review</b> passed', disable_notification=True, parse_mode='HTML'),
            self.text_body('review passed', disable_notification=True),
        ])

    def test_silent_photo_and_document_carry_the_multipart_field(self):
        for key, name, kind, method in (('photo', 'shot.png', 'photo', 'sendPhoto'),
                                        ('doc', 'notes.pdf', 'document', 'sendDocument')):
            path = self.attachment_file(name)
            result = self.bridge.publish(self.silent(key=key, text='merged',
                                                     attachment=dict(path=path, kind=kind)))
            self.assertTrue(result['delivered'])
            url, fields, _, file_path = self.multipart_calls[-1]
            self.assertTrue(url.endswith('/' + method))
            self.assertEqual(file_path, path)
            self.assertEqual(fields, {'chat_id': '123', 'caption': 'merged', 'disable_notification': 'true'})
        self.assertEqual(self.calls, [])

    def test_attachment_without_silent_keeps_todays_exact_fields(self):
        request = self.publish(key='photo', text='shot')
        request['attachment'] = dict(path=self.attachment_file(), kind='photo')
        self.assertTrue(self.bridge.publish(request)['delivered'])
        self.assertEqual(self.multipart_calls[0][1], {'chat_id': '123', 'caption': 'shot'})

    def test_silent_html_caption_parse_rejection_resend_stays_silent(self):
        def multipart(url, fields, file_field, file_path, timeout=20):
            self.multipart_calls.append((url, fields, file_field, file_path))
            return PARSE_REJECTED if fields.get('parse_mode') == 'HTML' else SENT_PHOTO
        self.bridge.request_multipart = multipart
        request = self.silent(text='<b>shot</b>', format='html',
                              attachment=dict(path=self.attachment_file(), kind='photo'))
        self.assertTrue(self.bridge.publish(request)['delivered'])
        self.assertEqual([call[1] for call in self.multipart_calls], [
            {'chat_id': '123', 'caption': '<b>shot</b>', 'parse_mode': 'HTML', 'disable_notification': 'true'},
            {'chat_id': '123', 'caption': 'shot', 'disable_notification': 'true'},
        ])

    def test_rejected_silent_attachment_text_fallback_stays_silent(self):
        def multipart(url, fields, file_field, file_path, timeout=20):
            self.multipart_calls.append((url, fields, file_field, file_path))
            return 400, dict(ok=False, error_code=400, description='PHOTO_INVALID_DIMENSIONS')
        self.bridge.request_multipart = multipart
        request = self.silent(text='progress', attachment=dict(path=self.attachment_file(), kind='photo'))
        result = self.bridge.publish(request)
        self.assertTrue(result['delivered'])
        self.assertEqual(result['fallback_reason'], 'telegram_rejected_not_sent_400')
        self.assertEqual(self.multipart_calls[0][1]['disable_notification'], 'true')
        self.assertEqual([body for _, body in self.calls], [self.text_body(
            'progress\n[attachment rejected: telegram_rejected_not_sent_400]', disable_notification=True)])

    def test_silent_keyboard_send_carries_both_fields(self):
        ask_id = b.Bridge._ask_id_for_key('silent-kb')
        rows = [[dict(text='Open', callback_data=f'ask:{ask_id}:open')]]
        self.assertTrue(self.bridge.publish(self.silent(key='silent-kb', text='FYI', keyboard=rows))['delivered'])
        self.assertEqual([body for _, body in self.calls], [self.text_body(
            'FYI', reply_markup={'inline_keyboard': rows}, disable_notification=True)])

    def test_same_key_with_and_without_silent_is_a_conflict(self):
        self.assertTrue(self.bridge.publish(self.publish(key='k1', text='landed'))['delivered'])
        conflict = self.bridge.publish(self.silent(key='k1', text='landed'))
        self.assertEqual(conflict['metadata']['reason'], 'idempotency_key_conflict')
        self.assertTrue(self.bridge.publish(self.silent(key='k2', text='landed'))['delivered'])
        back = self.bridge.publish(self.publish(key='k2', text='landed'))
        self.assertEqual(back['metadata']['reason'], 'idempotency_key_conflict')
        self.assertEqual(len(self.calls), 2)

    def test_silent_false_fingerprints_like_no_field(self):
        request = self.publish(key='existing', text='a status')
        first = self.bridge.publish(request)
        self.assertTrue(first['delivered'])
        # The row's fingerprint is the request exactly as before silent existed.
        self.assertEqual(self.fingerprint('existing'),
                         b.hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest())
        self.assertEqual(self.bridge.publish(dict(request, silent=False)), first)
        self.assertEqual(self.bridge.publish(request), first)
        self.assertEqual(len(self.calls), 1)
        # A silent row's fingerprint carries the field.
        silent = self.silent(key='quiet')
        self.assertTrue(self.bridge.publish(silent)['delivered'])
        self.assertEqual(self.fingerprint('quiet'),
                         b.hashlib.sha256(json.dumps(silent, sort_keys=True).encode()).hexdigest())

    def test_a_silent_value_other_than_a_boolean_refuses_before_telegram(self):
        for n, value in enumerate(('true', 'false', 1, 0, None, [], {})):
            result = self.bridge.publish(dict(self.publish(key=f'bad-{n}'), silent=value))
            self.assertFalse(result['delivered'])
            self.assertEqual(result['metadata']['reason'], 'silent_must_be_boolean')
        self.assertEqual(self.calls, [])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0], 0)


class SilentCliTests(BridgeTestCase):
    def args(self, *extra, command='reply-current', key='k-silent'):
        return b._build_parser().parse_args([command, '--body-file', 'x.txt', '--idempotency-key', key, *extra])

    def test_silent_flag_defaults_off_and_combines_with_the_send_flags(self):
        self.assertFalse(self.args().silent)
        args = self.args('--silent', '--format', 'html', '--attachment', '/tmp/x.png',
                         '--keyboard-file', '/tmp/k.json', command='send')
        self.assertTrue(args.silent)
        self.assertEqual(args.format, 'html')

    def test_dispatch_silent_publishes_directly_not_through_gc(self):
        for command in ('send', 'reply-current'):
            with patch.object(self.bridge, 'gc') as gc_mock:
                message_id = b._dispatch_outbound(self.bridge, self.args('--silent', command=command, key=command),
                                                  'review passed')
            gc_mock.assert_not_called()
            self.assertEqual(message_id, '77')
        self.assertEqual([body['disable_notification'] for _, body in self.calls], [True, True])

    def test_dispatch_silent_attachment_carries_the_field(self):
        path = self.attachment_file()
        with patch.object(self.bridge, 'gc') as gc_mock:
            b._dispatch_outbound(self.bridge, self.args('--silent', '--attachment', path), 'shot')
        gc_mock.assert_not_called()
        self.assertEqual(self.multipart_calls[0][1]['disable_notification'], 'true')

    def cli(self, *extra, key):
        """Run reply-current through main() with a temporary settings file and
        a fake curl; returns (stdout, the (url, JSON body) of each request)."""
        requests = []
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            settings = write_settings(root)
            body_file = root / 'body.txt'
            body_file.write_text('merged')

            def fake_run(argv, input='', **kwargs):
                config = dict(line.split(' = ', 1) for line in input.splitlines() if ' = ' in line)
                url = json.loads(config['url'])
                requests.append((url, json.loads(json.loads(config['data']))))
                if url.endswith('/extmsg/outbound'):
                    payload = {'Receipt': {'Delivered': True, 'MessageID': '55'}}
                elif url.endswith('/sendMessage'):
                    payload = {'ok': True, 'result': {'message_id': 42, 'chat': {'id': 123}}}
                else:
                    raise AssertionError('unexpected curl invocation')
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload) + '\n200 0.010000 0.020000 0', '')

            # The default sender is $GC_SESSION_ID, else the settings' agent.
            with patch.object(b.subprocess, 'run', fake_run), \
                    patch.dict(os.environ, {'GC_SESSION_ID': 'mayor'}), \
                    patch('sys.stdout', new_callable=io.StringIO) as out:
                run_main(self, 'bridge.py', '--settings', str(settings), 'reply-current',
                         '--body-file', str(body_file), '--idempotency-key', key, *extra)
        return out.getvalue(), requests

    def test_cli_reply_current_silent_sends_directly_and_never_calls_gc_outbound(self):
        out, requests = self.cli('--silent', key='cli-silent')
        self.assertIn('Telegram delivery confirmed; message ID 42', out)
        [(url, body)] = requests
        self.assertTrue(url.endswith('/sendMessage'))
        self.assertEqual(body, dict(chat_id=123, text='merged', link_preview_options={'is_disabled': True},
                                    disable_notification=True))

    def test_cli_reply_current_without_silent_keeps_the_gc_relay(self):
        out, requests = self.cli(key='cli-plain')
        self.assertIn('Telegram delivery confirmed; message ID 55', out)
        [(url, body)] = requests
        self.assertTrue(url.endswith('/v0/city/test-city/extmsg/outbound'))
        self.assertEqual(body, dict(session_id='mayor', conversation=body['conversation'],
                                    text='merged', idempotency_key='cli-plain'))
        self.assertNotIn('silent', json.dumps(body))


if __name__ == '__main__':
    unittest.main()
