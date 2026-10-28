"""Albums out: 2-10 files as one sendMediaGroup under one key, with the fake
Telegram and its injected album transport, temporary files, no network; one
file still takes today's single-file path byte for byte."""
from contextlib import closing
import argparse
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
from test_bridge import BridgeTestCase, run_main, write_settings  # noqa: E402

HTML_REJECTED = 400, dict(ok=False, error_code=400, description="Bad Request: can't parse entities: x")
REJECTED = 400, dict(ok=False, error_code=400, description='Bad Request: wrong file identifier')
AMBIGUOUS = 'ambiguous_delivery_requires_manual_reconciliation'


def messages(count, chat=123, first=90):
    return 200, dict(ok=True, result=[dict(message_id=first + index, chat=dict(id=chat)) for index in range(count)])


class AlbumTests(BridgeTestCase):
    """publish() with attachments, against an injected sendMediaGroup transport."""

    # BridgeTests' callback server helpers (imported by module, so discovery
    # runs BridgeTests once, in test_bridge).
    start_callback_server = test_bridge.BridgeTests.start_callback_server
    callback_post = test_bridge.BridgeTests.callback_post

    def setUp(self):
        super().setUp()
        self.album_calls = []
        self.album_answers = []  # answers used in order before the default
        self.bridge.request_media_group = self.album_transport
        self.bridge.sleep = lambda delay: None

    def album_transport(self, url, fields, files, timeout=20):
        self.album_calls.append((url, dict(fields), list(files)))
        if self.album_answers:
            answer = self.album_answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return messages(len(files))

    def files(self, *kinds):
        names = dict(photo='shot{}.png', video='clip{}.mp4', document='trace{}.zip', voice='note{}.ogg',
                     video_note='circle{}.mp4')
        files = []
        for kind in kinds:  # a new file each time, never one made earlier
            self.serial = getattr(self, 'serial', 0) + 1
            files.append(dict(path=self.attachment_file(names[kind].format(self.serial)), kind=kind))
        return files

    def album(self, attachments, key='album-1', text='three screens', **extra):
        return dict(self.publish(key=key, text=text), attachments=attachments, **extra)

    def media(self, call=0):
        return json.loads(self.album_calls[call][1]['media'])

    def ledger_count(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT count(*) FROM deliveries').fetchone()[0]

    def assert_refused(self, request, reason):
        receipt = self.bridge.publish(request)
        self.assertFalse(receipt['delivered'])
        self.assertEqual(receipt['metadata']['reason'], reason)

    def test_two_photos_go_as_one_media_group(self):
        attachments = self.files('photo', 'photo')
        receipt = self.bridge.publish(self.album(attachments))
        self.assertTrue(receipt['delivered'])
        self.assertEqual((receipt['message_id'], receipt['message_ids']), ('90', ['90', '91']))
        [(url, fields, files)] = self.album_calls
        self.assertTrue(url.endswith('/botSECRET/sendMediaGroup'))
        self.assertEqual(fields, {'chat_id': '123', 'media': json.dumps([
            {'type': 'photo', 'media': 'attach://file0', 'caption': 'three screens'},
            {'type': 'photo', 'media': 'attach://file1'}])})
        self.assertEqual(files, [('file0', attachments[0]['path']), ('file1', attachments[1]['path'])])
        self.assertEqual((self.calls, self.multipart_calls), ([], []))

    def test_photos_and_videos_mix_and_documents_group_together(self):
        for kinds in (('photo', 'video', 'photo'), ('document', 'document', 'document')):
            with self.subTest(kinds=kinds):
                self.album_calls.clear()
                self.assertTrue(self.bridge.publish(self.album(self.files(*kinds), key='-'.join(kinds)))['delivered'])
                self.assertEqual([item['type'] for item in self.media()], list(kinds))
                self.assertEqual([item['media'] for item in self.media()],
                                 [f'attach://file{index}' for index in range(len(kinds))])
        self.assertTrue(self.bridge.publish(self.album(self.files(*['photo'] * 10), key='ten'))['delivered'])

    def test_bad_albums_refuse_whole_before_anything_is_sent_or_reserved(self):
        missing = dict(path=str(Path(self.tmp.name) / 'gone.png'), kind='photo')
        unreadable = self.files('photo', 'photo')[1]
        os.chmod(unreadable['path'], 0)
        credential = self.files('document', 'document')
        self.bridge.protected_paths = (credential[1]['path'],)
        cases = [(self.files('document', 'photo'), 'album_mixes_documents'),
                 (self.files('video', 'document'), 'album_mixes_documents'),
                 (self.files('photo', 'voice'), 'album_kind_not_groupable'),
                 (self.files('video_note', 'video'), 'album_kind_not_groupable'),
                 (self.files(*['photo'] * 11), 'album_needs_2_to_10_files'),
                 (self.files('photo'), 'album_needs_2_to_10_files'),
                 ('not-a-list', 'album_needs_2_to_10_files'),
                 (self.files('photo') + [missing], 'attachment_file_missing_or_not_regular'),
                 (credential, 'attachment_path_is_a_bridge_credential'),
                 (self.files('photo') + [dict(path='shot.png', kind='photo')], 'attachment_path_must_be_absolute'),
                 (self.files('photo') + [dict(path='/tmp/a;b.png', kind='photo')],
                  'attachment_path_unsupported_characters'),
                 (self.files('photo') + [dict(kind='photo')], 'attachment_path_required'),
                 (self.files('photo') + ['shot.png'], 'attachment_must_be_an_object'),
                 (self.files('photo') + [dict(path='/tmp/x.png', kind='audio')], 'attachment_kind_unsupported')]
        if os.geteuid() != 0:
            cases.append((self.files('photo') + [unreadable], 'attachment_file_not_readable'))
        for index, (attachments, reason) in enumerate(cases):
            with self.subTest(reason=reason, index=index):
                self.assert_refused(self.album(attachments, key=f'bad-{index}'), reason)
        self.assert_refused(self.album(self.files('photo', 'photo'), key='long', text='x' * 1025),
                            'caption_exceeds_1024_utf16_units')
        self.assertEqual((self.album_calls, self.calls, self.multipart_calls), ([], [], []))
        self.assertEqual(self.ledger_count(), 0)

    def test_a_keyboard_or_a_single_attachment_with_an_album_refuses(self):
        attachments = self.files('photo', 'photo')
        ask_id = b.Bridge._ask_id_for_key('album-1')
        self.assert_refused(self.album(attachments, keyboard=[[dict(text='Yes', callback_data=f'ask:{ask_id}:y')]]),
                            'invalid_keyboard')
        self.assert_refused(self.album(attachments, attachment=attachments[0]), 'attachment_and_attachments_together')
        self.assertEqual((self.album_calls, self.calls), ([], []))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM asks').fetchone()[0], 0)

    def test_silent_adds_the_field_and_the_caption_rides_on_the_first_item_only(self):
        self.assertTrue(self.bridge.publish(self.album(self.files('photo', 'video'), silent=True))['delivered'])
        self.assertEqual(self.album_calls[0][1]['disable_notification'], 'true')
        self.assertEqual(['caption' in item for item in self.media()], [True, False])
        self.assertTrue(self.bridge.publish(self.album(self.files('photo', 'photo'), key='bare', text=''))['delivered'])
        self.assertEqual(self.media(1), [{'type': 'photo', 'media': 'attach://file0'},
                                         {'type': 'photo', 'media': 'attach://file1'}])
        self.assertNotIn('disable_notification', self.album_calls[1][1])

    def test_an_html_caption_is_re_sent_once_plain_on_a_parse_rejection(self):
        self.album_answers = [HTML_REJECTED]
        receipt = self.bridge.publish(self.album(self.files('photo', 'photo'), text='<b>A</b> &amp; B', format='html'))
        self.assertTrue(receipt['delivered'])
        self.assertEqual(receipt['format_fallback_reason'], 'telegram_html_parse_rejected_400')
        self.assertEqual(len(self.album_calls), 2)
        self.assertEqual(self.media(0)[0], {'type': 'photo', 'media': 'attach://file0',
                                            'caption': '<b>A</b> &amp; B', 'parse_mode': 'HTML'})
        self.assertEqual(self.media(1)[0], {'type': 'photo', 'media': 'attach://file0', 'caption': 'A & B'})
        self.assertEqual(self.calls, [])

    def test_another_rejection_falls_back_to_one_text_message(self):
        self.album_answers = [REJECTED]
        receipt = self.bridge.publish(self.album(self.files('photo', 'photo'), silent=True))
        self.assertTrue(receipt['delivered'])
        self.assertEqual(receipt['fallback_reason'], 'telegram_rejected_not_sent_400')
        self.assertEqual(len(self.album_calls), 1)
        [(url, body)] = self.calls
        self.assertTrue(url.endswith('/sendMessage'))
        self.assertEqual(body, dict(chat_id=123, text='three screens\n[album rejected: telegram_rejected_not_sent_400]',
                                    link_preview_options={'is_disabled': True}, disable_notification=True))

    def test_an_unconfirmed_album_stays_ambiguous_and_is_never_resent(self):
        cases = [messages(1), messages(2, chat=124), (502, dict(ok=False, error_code=502)),
                 (200, dict(ok=True, result=True)), (200, dict(ok=True, result=[dict(message_id='x'), 7])),
                 b.RequestUnconfirmed('network response unconfirmed', 0, 'after-send')]
        for index, answer in enumerate(cases):
            with self.subTest(index=index):
                self.album_answers = [answer]
                request = self.album(self.files('photo', 'photo'), key=f'amb-{index}')
                with patch('sys.stderr', new_callable=io.StringIO):
                    receipt = self.bridge.publish(request)
                self.assertFalse(receipt['delivered'])
                self.assertEqual(receipt['metadata']['reason'], AMBIGUOUS)
                calls = len(self.album_calls)
                self.assertEqual(self.bridge.publish(request), receipt)
                self.assertEqual(len(self.album_calls), calls)
        self.assertEqual(self.calls, [])

    def test_a_not_sent_album_is_sent_by_the_same_key(self):
        self.album_answers = [b.RequestNotSent('network request not sent', 7, 'connect')
                              for _ in range(b.SEND_ATTEMPTS)]
        request = self.album(self.files('photo', 'photo'))
        with patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(self.bridge.publish(request)['metadata']['reason'], b.NOT_SENT_REASON)
        self.assertTrue(self.bridge.publish(request)['delivered'])
        self.assertEqual(len(self.album_calls), b.SEND_ATTEMPTS + 1)

    def test_the_same_key_returns_the_saved_receipt_and_the_files_are_its_identity(self):
        attachments = self.files('photo', 'video')
        request = self.album(attachments)
        first = self.bridge.publish(request)
        self.assertEqual(self.bridge.publish(request), first)
        self.assertEqual(len(self.album_calls), 1)
        self.assert_refused(self.album(attachments[::-1]), 'idempotency_key_conflict')
        self.assert_refused(dict(self.publish(key='album-1', text='three screens'), attachment=attachments[0]),
                            'idempotency_key_conflict')
        self.assertEqual(len(self.album_calls), 1)

    def test_one_attachment_keeps_the_single_file_path_and_its_exact_fields(self):
        photo = self.files('photo')[0]
        receipt = self.bridge.publish(dict(self.publish(key='one', text='shot'), attachment=photo))
        self.assertEqual(receipt['message_id'], '88')
        self.assertNotIn('message_ids', receipt)
        [(url, fields, field, path)] = self.multipart_calls
        self.assertTrue(url.endswith('/sendPhoto'))
        self.assertEqual((fields, field, path), ({'chat_id': '123', 'caption': 'shot'}, 'photo', photo['path']))
        self.assertEqual(self.album_calls, [])

    def test_the_callback_refuses_an_album(self):
        port = self.start_callback_server()
        status, result = self.callback_post(port, self.album(self.files('photo', 'photo'), key='cb-album'))
        self.assertEqual(status, 200)
        self.assertEqual(result['metadata']['reason'], 'attachment_not_accepted_over_callback')
        self.assertEqual((self.album_calls, self.calls), ([], []))

    def args(self, *attachments, kind=None):
        return argparse.Namespace(keyboard_file=None, silent=False, format='plain', idempotency_key='cli-album',
                                  attachment=[Path(path) for path in attachments], attachment_kind=kind,
                                  session='mayor')

    def test_dispatch_builds_attachments_only_for_two_or_more_files(self):
        shot, clip = self.attachment_file('a.png'), self.attachment_file('b.mp4')
        seen = []

        def publish(request):
            seen.append(request)
            return dict(delivered=True, message_id='90')
        with patch.object(self.bridge, 'publish', side_effect=publish), patch.object(self.bridge, 'gc') as gc:
            self.assertEqual(b._dispatch_outbound(self.bridge, self.args(shot, clip), 'two'), '90')
            b._dispatch_outbound(self.bridge, self.args(shot, clip, kind='document'), 'docs')
            b._dispatch_outbound(self.bridge, self.args(shot), 'one')
        gc.assert_not_called()
        self.assertEqual(seen[0]['attachments'], [dict(path=shot, kind='photo'), dict(path=clip, kind='video')])
        self.assertNotIn('attachment', seen[0])
        self.assertEqual([item['kind'] for item in seen[1]['attachments']], ['document', 'document'])
        self.assertEqual(seen[2]['attachment'], dict(path=shot, kind='photo'))
        self.assertNotIn('attachments', seen[2])


class AlbumTransportTests(unittest.TestCase):
    def run_curl(self, call):
        seen = []

        def fake_run(argv, input='', **kwargs):
            seen.append((argv, input))
            return subprocess.CompletedProcess(argv, 0, '{"ok": true, "result": []}\n200 0.010000 0.020000 0', '')
        with patch.object(b.subprocess, 'run', fake_run):
            call()
        [(argv, config)] = seen
        return argv, config.split('\n')

    def test_the_album_helper_names_every_file_on_stdin_and_never_the_token_in_argv(self):
        url = 'https://api.telegram.org/botSECRET-TOKEN/sendMediaGroup'
        argv, config = self.run_curl(lambda: b.http_multipart_files(
            url, {'chat_id': '123', 'media': '[{"type": "photo"}]'},
            [('file0', '/tmp/one.png'), ('file1', '/tmp/two "q".png')]))
        self.assertEqual(config, ['url = "https://api.telegram.org/botSECRET-TOKEN/sendMediaGroup"',
                                  'form-string = "chat_id=123"',
                                  'form-string = "media=[{\\"type\\": \\"photo\\"}]"',
                                  'form = "file0=@/tmp/one.png"',
                                  'form = "file1=@/tmp/two \\"q\\".png"'])
        self.assertFalse(any('SECRET-TOKEN' in arg for arg in argv))
        self.assertEqual(argv[argv.index('--config') + 1], '-')

    def test_the_single_file_helper_keeps_its_exact_configuration(self):
        url = 'https://api.telegram.org/botSECRET-TOKEN/sendPhoto'
        argv, config = self.run_curl(lambda: b.http_multipart(url, {'chat_id': '123', 'caption': 'shot'},
                                                              'photo', '/tmp/one.png'))
        self.assertEqual(config, ['url = "https://api.telegram.org/botSECRET-TOKEN/sendPhoto"',
                                  'form-string = "chat_id=123"', 'form-string = "caption=shot"',
                                  'form = "photo=@/tmp/one.png"'])
        self.assertFalse(any('SECRET-TOKEN' in arg for arg in argv))


class AlbumCliTests(unittest.TestCase):
    def test_send_with_two_attachments_prints_the_first_message_id(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        settings = write_settings(root)
        body, shot, clip = root / 'body.txt', root / 'a.png', root / 'b.mp4'
        body.write_text('two screens')
        shot.write_bytes(b'png')
        clip.write_bytes(b'mp4')
        configs = []

        def fake_run(argv, input='', **kwargs):
            configs.append(input.split('\n'))
            payload = {'ok': True, 'result': [{'message_id': 42, 'chat': {'id': 123}},
                                              {'message_id': 43, 'chat': {'id': 123}}]}
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload) + '\n200 0.010000 0.020000 0', '')
        with patch.object(b.subprocess, 'run', fake_run), patch('sys.stdout', new_callable=io.StringIO) as out:
            run_main(self, 'bridge.py', '--settings', str(settings), 'send', '--body-file', str(body),
                     '--idempotency-key', 'cli-album', '--session', 'mayor', '--silent',
                     '--attachment', str(shot), '--attachment', str(clip))
        self.assertEqual(out.getvalue(), 'Telegram delivery confirmed; message ID 42\n')
        [config] = configs
        self.assertTrue(config[0].endswith('/sendMediaGroup"'))
        self.assertEqual(config[-2:], [f'form = "file0=@{shot}"', f'form = "file1=@{clip}"'])
        self.assertIn('form-string = "disable_notification=true"', config)
        with closing(sqlite3.connect(root / 'state' / 'telegram-bridge.sqlite3')) as db:
            [(receipt,)] = db.execute("SELECT receipt FROM deliveries WHERE key='cli-album'")
        self.assertEqual(json.loads(receipt)['message_ids'], ['42', '43'])

    def test_more_than_ten_attachments_refuse_before_telegram(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        settings = write_settings(root)
        body = root / 'body.txt'
        body.write_text('many')
        argv = ['bridge.py', '--settings', str(settings), 'send', '--body-file', str(body),
                '--idempotency-key', 'k', '--session', 'mayor']
        for index in range(11):
            (root / f'{index}.png').write_bytes(b'png')
            argv += ['--attachment', str(root / f'{index}.png')]
        with patch.object(b.subprocess, 'run', side_effect=AssertionError('nothing may be sent')), \
                patch('sys.stderr', new_callable=io.StringIO) as err, \
                self.assertRaisesRegex(b.TransportError, 'album_needs_2_to_10_files'):
            run_main(self, *argv)
        self.assertIn('"reason": "album_needs_2_to_10_files"', err.getvalue())


if __name__ == '__main__':
    unittest.main()
