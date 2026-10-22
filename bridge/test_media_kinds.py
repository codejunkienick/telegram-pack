"""Voice notes and round video notes out: the shared attachment path (validation,
multipart send, ledger, receipts, 4xx text fallback) with the fake Telegram,
and the CLI and reply-current command passing both kinds through."""
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
from test_bridge import BridgeTestCase, MENU, run_main, write_fake_curl, write_settings  # noqa: E402

PACK = Path(__file__).resolve().parents[1]
LIMIT = 50 * 1024 * 1024


class MediaKindTests(BridgeTestCase):
    # Own root/store fixture; the enterContext cleanup replaces tearDown.
    def tearDown(self):
        pass

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = b.Store(self.root / 'state.sqlite3', 10)
        self.calls, self.multipart_calls = [], []
        self.multipart_answer = (200, dict(ok=True, result=dict(message_id=88, chat=dict(id=123))))
        config = dict(bot_id=999, user_id=123, chat_id=123, bot_username='test_bot', city='test-city',
                      user_display_name='Pat', timezone='UTC', unit='test-city-telegram.service', menu=MENU)
        self.bridge = b.Bridge(config, self.store, 'SECRET', 'CAPABILITY', request=self.transport,
                               request_multipart=self.multipart_transport)

    def transport(self, url, body, *args, **kwargs):
        self.calls.append((url, body))
        return 200, dict(ok=True, result=dict(message_id=77, chat=dict(id=123)))

    def multipart_transport(self, url, fields, file_field, file_path, timeout=20):
        self.multipart_calls.append((url, fields, file_field, file_path))
        if isinstance(self.multipart_answer, Exception):
            raise self.multipart_answer
        return self.multipart_answer

    def media(self, name, size=100):
        path = self.root / name
        path.write_bytes(b'')
        os.truncate(path, size)
        return str(path)

    def request(self, kind, text, key='media-1', path=None, **extra):
        path = path or self.media('note.ogg' if kind == 'voice' else 'circle.mp4')
        return dict(conversation=self.bridge.conversation, idempotency_key=key, text=text,
                    attachment=dict(path=path, kind=kind), **extra)

    def ledger(self, key):
        with closing(sqlite3.connect(self.store.path)) as db:
            row = db.execute('SELECT receipt FROM deliveries WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def test_voice_goes_out_as_send_voice_with_its_caption(self):
        receipt = self.bridge.publish(self.request('voice', 'listen to this', reply_to_message_id='5'))
        self.assertEqual((receipt['delivered'], receipt['message_id']), (True, '88'))
        url, fields, field, path = self.multipart_calls[0]
        self.assertTrue(url.endswith('/sendVoice'))
        self.assertEqual((field, Path(path).name), ('voice', 'note.ogg'))
        self.assertEqual(fields['caption'], 'listen to this')
        self.assertEqual(json.loads(fields['reply_parameters']), {'message_id': 5})
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ledger('media-1'), receipt)

    def test_voice_caption_keeps_the_html_validator_and_the_1024_unit_cap(self):
        request = self.request('voice', '<b>listen</b>')
        request['format'] = 'html'
        self.assertTrue(self.bridge.publish(request)['delivered'])
        self.assertEqual(self.multipart_calls[0][1]['parse_mode'], 'HTML')
        bad = self.request('voice', '<script>x</script>', key='media-2')
        bad['format'] = 'html'
        self.assertEqual(self.bridge.publish(bad)['metadata']['reason'], 'html_tag_not_allowed')
        long = self.bridge.publish(self.request('voice', 'x' * 1025, key='media-3'))
        self.assertEqual(long['metadata']['reason'], 'caption_exceeds_1024_utf16_units')
        self.assertEqual(len(self.multipart_calls), 1)

    def test_video_note_goes_out_as_send_video_note_with_no_caption(self):
        receipt = self.bridge.publish(self.request('video_note', ''))
        self.assertTrue(receipt['delivered'])
        url, fields, field, path = self.multipart_calls[0]
        self.assertTrue(url.endswith('/sendVideoNote'))
        self.assertEqual((field, Path(path).name), ('video_note', 'circle.mp4'))
        self.assertEqual(fields, {'chat_id': '123'})
        html = self.request('video_note', '', key='media-2')
        html['format'] = 'html'
        self.assertTrue(self.bridge.publish(html)['delivered'])
        self.assertNotIn('parse_mode', self.multipart_calls[1][1])

    def test_a_video_note_with_text_is_refused_before_any_send(self):
        for text in ('a caption', '<b>x</b>', ' ', '\n'):
            receipt = self.bridge.publish(self.request('video_note', text))
            self.assertEqual(receipt['metadata']['reason'], 'video_note_takes_no_caption')
        # Markup that renders to no visible text is still a body.
        for text in ('<b></b>', '<i> </i>', ' \n'):
            html = self.request('video_note', text)
            html['format'] = 'html'
            self.assertEqual(self.bridge.publish(html)['metadata']['reason'], 'video_note_takes_no_caption')
        self.assertEqual((self.multipart_calls, self.calls), ([], []))
        self.assertIsNone(self.ledger('media-1'))

    def test_only_the_video_note_takes_an_empty_body(self):
        for kind in ('voice', 'photo', 'video', 'document'):
            receipt = self.bridge.publish(self.request(kind, ' \n', key='empty-' + kind))
            self.assertEqual(receipt['metadata']['reason'], 'text_required_maximum_4096_utf16_units')
        self.assertEqual(self.multipart_calls, [])

    def test_both_kinds_refuse_a_file_over_50_mib(self):
        for kind in ('voice', 'video_note'):
            exact = self.request(kind, '' if kind == 'video_note' else 'c', key='fits-' + kind,
                                 path=self.media(kind + '.bin', LIMIT))
            self.assertTrue(self.bridge.publish(exact)['delivered'])
            over = self.request(kind, '' if kind == 'video_note' else 'c', key='over-' + kind,
                                path=self.media(kind + '-big.bin', LIMIT + 1))
            self.assertEqual(self.bridge.publish(over)['metadata']['reason'], 'attachment_exceeds_size_limit')
            self.assertIsNone(self.ledger('over-' + kind))
        self.assertEqual(len(self.multipart_calls), 2)

    def test_a_confirmed_send_is_one_receipt_per_key(self):
        for kind, text in (('voice', 'v'), ('video_note', '')):
            request = self.request(kind, text, key='once-' + kind)
            first = self.bridge.publish(request)
            self.assertEqual(self.bridge.publish(request), first)
            self.assertEqual(self.ledger('once-' + kind), first)
            changed = dict(request, attachment=dict(request['attachment'], kind='document'), text='v')
            self.assertEqual(self.bridge.publish(changed)['metadata']['reason'], 'idempotency_key_conflict')
        self.assertEqual(len(self.multipart_calls), 2)

    def test_a_telegram_4xx_falls_back_to_one_plain_text_message(self):
        self.multipart_answer = (400, dict(ok=False, error_code=400, description='VOICE_MESSAGES_FORBIDDEN'))
        voice = self.bridge.publish(self.request('voice', 'the caption', key='fb-voice'))
        circle = self.bridge.publish(self.request('video_note', '', key='fb-circle'))
        for receipt in (voice, circle):
            self.assertEqual((receipt['delivered'], receipt['fallback_reason']),
                             (True, 'telegram_rejected_not_sent_400'))
        texts = [body['text'] for url, body in self.calls]
        self.assertEqual(texts, ['the caption\n[attachment rejected: telegram_rejected_not_sent_400]',
                                 '[attachment rejected: telegram_rejected_not_sent_400]'])
        self.assertEqual(self.ledger('fb-circle'), circle)

    def test_an_unconfirmed_send_stays_ambiguous_and_is_never_resent(self):
        for kind, text, answer in (('voice', 'v', b.TransportError('network response unconfirmed')),
                                   ('video_note', '', (502, dict(ok=False, error_code=502)))):
            self.multipart_answer = answer
            request = self.request(kind, text, key='amb-' + kind)
            receipt = self.bridge.publish(request)
            self.assertFalse(receipt['delivered'])
            self.assertEqual(receipt['metadata']['reason'], 'ambiguous_delivery_requires_manual_reconciliation')
            self.assertEqual(self.ledger('amb-' + kind), receipt)
            self.multipart_answer = (200, dict(ok=True, result=dict(message_id=89, chat=dict(id=123))))
            self.assertEqual(self.bridge.publish(request), receipt)
        self.assertEqual(len(self.multipart_calls), 2)
        self.assertEqual(self.calls, [])


class MediaKindCliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def test_send_and_reply_current_accept_both_kinds(self):
        parser = b._build_parser()
        for command in ('send', 'reply-current'):
            for kind in ('voice', 'video_note'):
                args = parser.parse_args([command, '--attachment', '/tmp/x', '--attachment-kind', kind])
                self.assertEqual(args.attachment_kind, kind)
        self.assertEqual(b.infer_attachment_kind(Path('note.ogg')), 'document')  # never inferred

    def fake_curl(self):
        """A curl on PATH that answers every send with message 42 and keeps
        each config it was given on stdin."""
        bin_dir = self.root / 'bin'
        write_fake_curl(bin_dir, f'#!{sys.executable}\nimport json, sys, uuid\nfrom pathlib import Path\n'
                        f'Path({str(self.root)!r}, "curl-" + uuid.uuid4().hex).write_text(sys.stdin.read())\n'
                        'print(json.dumps({"ok": True, "result": {"message_id": 42, "chat": {"id": 123}}}))\n'
                        'print("200 0.010000 0.020000 0", end="")\n')
        return bin_dir

    def test_gc_telegram_reply_current_passes_both_kinds_to_send_voice_and_send_video_note(self):
        settings = write_settings(self.root)
        env = {k: v for k, v in os.environ.items() if not k.startswith('GC_')}
        env.update(PATH=f'{self.fake_curl()}:{env["PATH"]}', TELEGRAM_BRIDGE_SETTINGS=str(settings))
        voice, circle = self.root / 'note.ogg', self.root / 'circle.mp4'
        voice.write_bytes(b'OggS')
        circle.write_bytes(b'mp4')
        caption, empty = self.root / 'caption.txt', self.root / 'empty.txt'
        caption.write_text('hear me')
        empty.write_text('')
        for key, body, path, kind in (('cli-voice', caption, voice, 'voice'),
                                      ('cli-circle', empty, circle, 'video_note')):
            result = subprocess.run(['sh', str(PACK / 'commands' / 'reply-current' / 'run.sh'), '--body-file', str(body),
                                     '--idempotency-key', key, '--attachment', str(path), '--attachment-kind', kind],
                                    env=env, cwd=self.root, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Telegram delivery confirmed; message ID 42', result.stdout)
        configs = sorted((p.read_text() for p in self.root.glob('curl-*')), key=lambda c: 'sendVoice' not in c)
        self.assertEqual(len(configs), 2)
        self.assertIn('/sendVoice"', configs[0])
        self.assertIn(f'form = "voice=@{voice}"', configs[0])
        self.assertIn('form-string = "caption=hear me"', configs[0])
        self.assertIn('/sendVideoNote"', configs[1])
        self.assertIn(f'form = "video_note=@{circle}"', configs[1])
        self.assertNotIn('caption', configs[1])
        with closing(sqlite3.connect(self.root / 'state' / 'telegram-bridge.sqlite3')) as db:
            rows = dict(db.execute('SELECT key, receipt FROM deliveries'))
        self.assertEqual({k: json.loads(v)['delivered'] for k, v in rows.items()}, {'cli-voice': True, 'cli-circle': True})

    def test_a_refused_video_note_caption_prints_the_receipt_line(self):
        settings = write_settings(self.root)
        circle, body = self.root / 'circle.mp4', self.root / 'body.txt'
        circle.write_bytes(b'mp4')
        body.write_text('no captions here')
        argv = ['bridge.py', '--settings', str(settings), 'reply-current', '--body-file', str(body),
                '--idempotency-key', 'k', '--attachment', str(circle), '--attachment-kind', 'video_note']
        with patch.object(b.subprocess, 'run', side_effect=AssertionError('nothing may be sent')), \
             patch('sys.stderr', new_callable=io.StringIO) as err:
            with self.assertRaisesRegex(b.TransportError, 'video_note_takes_no_caption'):
                run_main(self, *argv)
        self.assertIn('"reason": "video_note_takes_no_caption"', err.getvalue())


if __name__ == '__main__':
    unittest.main()
