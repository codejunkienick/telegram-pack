"""Outbound in topics the paired user creates ([auto_topics], OPERATIONS.md,
"Topics the user creates"): a send on a served auto topic's conversation,
bound or not yet bound, carries its message_thread_id on every Telegram
request, and send, reply-current and react take its --conversation-id. With
[auto_topics] off, a shadowed row or an unknown thread, nothing changes;
General and the settings topics send byte for byte as before. Fake Telegram
and GC, temporary directories; no network."""
from contextlib import closing
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
import test_topics  # noqa: E402
from test_auto_topic_inbound import AUTO_OFF, AUTO_ON, NEW, AutoTopicInboundCase  # noqa: E402
from test_bridge import run_main, write_settings  # noqa: E402
from test_topic_acks import CHAT_ID, TopicAckCase, typing_in  # noqa: E402
from test_topic_inbound import OPS, in_topic, record_topics  # noqa: E402
from test_topic_outbound import GONE, HTML_REJECTED, TopicOutboundCase, topic_ref  # noqa: E402

AUTO, FRESH, UNKNOWN = 601, 602, 603  # a bound auto row, a not yet bound one, a thread with no row
AUTO_ID = f'123:topic:{AUTO}'
DELETED = 400, dict(ok=False, error_code=400, description='Bad Request: message thread not found')
UNRESOLVED = 'conversation ID is not the paired private chat or a configured topic'


def add_auto_rows(store):
    """AUTO bound and named, FRESH not yet bound, and two shadowed rows: one
    on the settings topic "ops", one on a topics row the settings dropped."""
    for thread_id in (AUTO, FRESH, OPS, GONE):
        store.add_auto_topic(thread_id, 'mayor')
    store.set_auto_topic_name(AUTO, 'Trip', False)
    store.set_auto_topic_bound(AUTO, True)


class AutoTopicOutboundCase(TopicOutboundCase):
    """The topic outbound fixture ("ops" recorded as thread 7, "old" as the
    leftover thread 13) with [auto_topics] on and the auto rows above."""

    def setUp(self):
        super().setUp()
        self.store.record_topic('old', GONE, 'created')
        add_auto_rows(self.store)
        self.config['auto_topics'] = AUTO_ON
        self.bridge = self.make_bridge(self.config, self.store)

    def request(self, key='auto-1', text='hello', conversation=None, **fields):
        return super().request(key, text, conversation or topic_ref(AUTO), **fields)

    def assert_in_thread(self, receipt, thread_id=AUTO):
        self.assertTrue(receipt['delivered'], receipt)
        self.assertEqual(receipt['conversation'], topic_ref(thread_id))


class AutoTopicPublishTests(AutoTopicOutboundCase):
    def test_a_send_on_a_bound_or_not_yet_bound_auto_topic_carries_its_thread(self):
        for thread_id in (AUTO, FRESH):
            with self.subTest(thread_id=thread_id):
                del self.calls[:]
                request = self.request(key=f'text-{thread_id}', conversation=topic_ref(thread_id),
                                       reply_to_message_id='41')
                receipt = self.bridge.publish(request)
                self.assert_in_thread(receipt, thread_id)
                self.assertEqual(self.calls, [('https://api.telegram.org/botSECRET/sendMessage', dict(
                    chat_id=123, text='hello', link_preview_options={'is_disabled': True},
                    message_thread_id=thread_id, reply_parameters={'message_id': 41}))])
                self.assertIs(type(self.calls[0][1]['message_thread_id']), int)
                self.assertEqual(self.ledger()[f'text-{thread_id}'], hashlib.sha256(json.dumps(
                    request, sort_keys=True).encode()).hexdigest())
                self.assertEqual(self.bridge.publish(request), receipt)
                self.assertEqual(len(self.calls), 1)
        self.assertIsNone(next(row for row in self.store.auto_topic_rows() if row['thread_id'] == FRESH)['bound_at'])

    def test_the_html_plain_fallback_and_a_silent_send_stay_in_the_auto_topic(self):
        self.answers['sendMessage'] = [HTML_REJECTED]
        receipt = self.bridge.publish(self.request(text='<b>x</b> &amp; y', format='html'))
        self.assert_in_thread(receipt)
        self.assertEqual(receipt['format_fallback_reason'], 'telegram_html_parse_rejected_400')
        first, second = self.sent('sendMessage')
        self.assertEqual((first['parse_mode'], first['message_thread_id']), ('HTML', AUTO))
        self.assertEqual((second['text'], second['message_thread_id']), ('x & y', AUTO))
        self.assertNotIn('parse_mode', second)
        self.assert_in_thread(self.bridge.publish(self.request(key='quiet', silent=True)))
        quiet = self.sent('sendMessage')[2]
        self.assertEqual((quiet['disable_notification'], quiet['message_thread_id']), (True, AUTO))

    def test_an_ask_records_the_auto_conversation_and_carries_the_thread(self):
        receipt = self.bridge.publish(self.request(key='ask-auto', text='Pick one?',
                                                   keyboard=self.keyboard('ask-auto')))
        self.assert_in_thread(receipt)
        [send] = self.sent('sendMessage')
        self.assertEqual((send['message_thread_id'], send['reply_markup']),
                         (AUTO, {'inline_keyboard': self.keyboard('ask-auto')}))
        self.assertEqual(self.store.get_ask(self.ask_id('ask-auto'))['conversation_id'], AUTO_ID)

    def test_a_photo_and_an_album_carry_the_thread_as_a_multipart_field(self):
        self.assert_in_thread(self.bridge.publish(self.request(key='photo', text='a shot', silent=True,
                                                               attachment=self.files('photo')[0])))
        [(url, fields, _, _)] = self.multipart_calls
        self.assertTrue(url.endswith('/sendPhoto'))
        self.assertEqual((fields['message_thread_id'], fields['disable_notification']), (str(AUTO), 'true'))
        receipt = self.bridge.publish(self.request(key='album', text='two', attachments=self.files('photo', 'video')))
        self.assert_in_thread(receipt)
        self.assertEqual(receipt['message_ids'], ['90', '91'])
        [(url, fields, _)] = self.album_calls
        self.assertTrue(url.endswith('/sendMediaGroup'))
        self.assertEqual(fields['message_thread_id'], str(AUTO))

    def test_the_gc_callback_publishes_on_an_auto_conversation(self):
        port = self.start_callback_server()
        status, receipt = self.callback_post(port, dict(self.request(key='cb-auto'), session_id='mayor'))
        self.assertEqual(status, 200)
        self.assert_in_thread(receipt)
        [body] = self.sent('sendMessage')
        self.assertEqual(body['message_thread_id'], AUTO)

    def test_auto_off_a_shadowed_row_or_an_unknown_thread_is_refused_and_sends_nothing(self):
        bridges = {
            'auto on': self.bridge,
            'auto off': self.make_bridge(dict(self.config, auto_topics=AUTO_OFF), self.store),
            'no auto table': self.make_bridge({k: v for k, v in self.config.items() if k != 'auto_topics'},
                                              self.store),
        }
        cases = [('auto on', GONE), ('auto on', UNKNOWN), ('auto off', AUTO), ('auto off', FRESH),
                 ('no auto table', AUTO), ('no auto table', GONE)]
        for index, (name, thread_id) in enumerate(cases):
            with self.subTest(name, thread_id=thread_id):
                receipt = bridges[name].publish(self.request(key=f'refused-{index}', conversation=topic_ref(thread_id)))
                self.assert_refused(receipt, 'recipient_not_authorized')
                self.assertEqual(receipt['conversation'], self.bridge.conversation)
        self.assertEqual((self.calls, self.multipart_calls, self.ledger()), ([], [], {}))

    def test_a_row_shadowed_by_a_settings_topic_sends_as_that_topic(self):
        self.assert_in_thread(self.bridge.publish(self.request(conversation=topic_ref(OPS))), OPS)
        [body] = self.sent('sendMessage')
        self.assertEqual(body['message_thread_id'], OPS)

    def test_a_send_into_a_deleted_topic_is_rejected_once_and_never_resent_to_general(self):
        self.answers['sendMessage'] = [DELETED]
        receipt = self.bridge.publish(self.request(key='deleted'))
        self.assert_refused(receipt, 'telegram_rejected_not_sent_400')
        self.assertEqual(receipt['conversation'], topic_ref(AUTO))
        [body] = self.sent('sendMessage')
        self.assertEqual(body['message_thread_id'], AUTO)
        self.assertEqual(len(self.calls), 1)
        self.assertIn(AUTO, [row['thread_id'] for row in self.store.auto_topic_rows()])
        self.assertIn(AUTO, [topic['thread_id'] for topic in self.bridge.served_auto_topics()])

    def test_a_file_or_album_into_a_deleted_topic_is_rejected_once_with_no_text_fallback(self):
        self.multipart_answers, self.album_answers = [DELETED, DELETED], [DELETED, DELETED]
        for fmt in ('plain', 'html'):
            with self.subTest(fmt=fmt):
                del self.calls[:], self.multipart_calls[:], self.album_calls[:]
                text = 'a <b>shot</b>' if fmt == 'html' else 'a shot'
                receipts = [self.bridge.publish(self.request(key=f'deleted-photo-{fmt}', text=text, format=fmt,
                                                             attachment=self.files('photo')[0])),
                            self.bridge.publish(self.request(key=f'deleted-album-{fmt}', text=text, format=fmt,
                                                             attachments=self.files('photo', 'video')))]
                for receipt in receipts:
                    self.assert_refused(receipt, 'telegram_rejected_not_sent_400')
                    self.assertEqual(receipt['conversation'], topic_ref(AUTO))
                self.assertEqual([fields['message_thread_id'] for _, fields, _, _ in self.multipart_calls],
                                 [str(AUTO)])
                self.assertEqual([fields['message_thread_id'] for _, fields, _ in self.album_calls], [str(AUTO)])
                self.assertEqual(self.calls, [])
        self.assertIn(AUTO, [topic['thread_id'] for topic in self.bridge.served_auto_topics()])

    def test_a_pinned_slot_message_stays_in_general(self):
        self.assert_in_thread(self.bridge.publish(self.request()))
        self.assertTrue(self.bridge.pin('waiting', 'nothing waits')['delivered'])
        self.assertNotIn('message_thread_id', self.sent('sendMessage')[1])
        self.assertNotIn('message_thread_id', self.sent('pinChatMessage')[0])

    def test_general_and_settings_topics_send_byte_for_byte_as_with_auto_off(self):
        off_path = Path(self.tmp.name) / 'off.sqlite3'
        off_store = b.Store(off_path, 10)
        record_topics(off_store)
        off = self.make_bridge({k: v for k, v in self.config.items() if k != 'auto_topics'}, off_store)
        album = self.files('photo', 'photo')
        requests = [
            dict(text='plain'),
            dict(text='<b>html</b>', format='html'),
            dict(text='quiet', silent=True, reply_to_message_id='5'),
            dict(text='a photo', attachment=self.files('photo')[0]),
            dict(text='two', attachments=album),
            dict(text='Pick one?', keyboard=None),
        ]
        for conversation in (self.bridge.conversation, topic_ref(OPS)):
            for index, fields in enumerate(requests):
                key = f"{conversation['conversation_id']}-{index}"
                fields = dict(fields)
                if 'keyboard' in fields:
                    fields['keyboard'] = self.keyboard(key)
                request = dict(fields, conversation=conversation, idempotency_key=key)
                with self.subTest(key=key):
                    calls = []
                    receipts = []
                    for bridge in (off, self.bridge):
                        del self.calls[:], self.multipart_calls[:], self.album_calls[:]
                        receipts.append(bridge.publish(json.loads(json.dumps(request))))
                        calls.append((list(self.calls), list(self.multipart_calls), list(self.album_calls)))
                    self.assertEqual(calls[1], calls[0])
                    self.assertEqual(receipts[1], receipts[0])
                    self.assertTrue(receipts[1]['delivered'])
                    self.assertEqual(receipts[1]['conversation'], conversation)
        self.assertEqual(self.ledger(), self.ledger(off_path))


class AutoTopicQuotaTests(AutoTopicInboundCase):
    def test_the_quota_notice_for_a_message_in_an_auto_topic_goes_into_its_thread(self):
        health = Path(self.tmp.name) / 'health.json'
        self.config['health_file'] = str(health)
        health.write_text(json.dumps(dict(
            observed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            sessions=[dict(template='mayor', status='quota_wait', episode='ep-auto',
                           until=int(time.time() * 1000) + 3600 * 1000)])))
        self.bridge = self.make_bridge(self.config)
        self.drain(in_topic(self.update(10), NEW), in_topic(self.update(11), NEW))
        self.assertEqual(self.store.last_accepted_in(f'123:topic:{NEW}'), 11)
        notices = [body for url, body in self.calls
                   if url.endswith('/sendMessage') and 'quota exhausted' in body['text']]
        self.assertEqual([notice['message_thread_id'] for notice in notices], [NEW])


class AutoTopicAckCase(TopicAckCase, AutoTopicInboundCase):
    """The reactions and typing fixture with [auto_topics] on and the fake GC
    that binds auto topics."""

    def setUp(self):
        super().setUp()
        self.bridge = self.make_bridge(self.config_with(300, auto_topics=AUTO_ON))


class AutoTopicAckTests(AutoTopicAckCase):
    def test_react_conversation_id_targets_the_auto_conversations_last_accepted_message(self):
        self.drain(in_topic(self.update(10), NEW), self.update(11), in_topic(self.update(12), OPS))
        self.assertEqual(self.store.last_accepted_in(f'123:topic:{NEW}'), 10)
        self.assertEqual(self.react('--reaction', 'done', '--conversation-id', f'123:topic:{NEW}'), 10)
        self.assertEqual(self.react('--reaction', 'done', '--conversation-id', CHAT_ID), 11)
        self.assertEqual([body['message_id'] for url, body in self.calls if url.endswith('/setMessageReaction')],
                         [10, 11])

    def test_react_on_an_unknown_thread_or_with_auto_off_refuses_as_today(self):
        self.drain(in_topic(self.update(10), NEW))
        self.bridge = self.make_bridge(self.config_with(300, auto_topics=AUTO_OFF))
        for conversation_id in (f'123:topic:{NEW}', f'123:topic:{NEW + 50}'):
            with self.subTest(conversation_id), self.assertRaises(b.TransportError) as raised:
                self.react('--reaction', 'done', '--conversation-id', conversation_id)
            self.assertEqual(str(raised.exception), UNRESOLVED)
        self.assertEqual(self.reactions(), [])

    def test_typing_after_a_message_in_an_auto_topic_shows_in_its_thread(self):
        self.drain(in_topic(self.update(10), NEW))
        self.work(1)
        self.assertEqual(self.actions_at(), [(0, typing_in(NEW))])


class AutoTopicCliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.dict(os.environ))
        os.environ.pop('GC_SESSION_ID', None)
        self.settings = write_settings(self.root)
        self.base_settings = self.settings.read_text() + test_topics.topic_tables(('ops', None), ('lab', None))
        self.write_settings(AUTO_ON)
        state = self.root / 'state'
        state.mkdir(mode=0o700)
        self.ledger = state / 'telegram-bridge.sqlite3'
        store = b.Store(self.ledger, 6)
        store.record_topic('ops', OPS, 'created')
        store.record_topic('old', GONE, 'created')
        add_auto_rows(store)
        self.body = self.root / 'body.txt'
        self.body.write_text('<b>done</b>')
        self.inputs = []

    def write_settings(self, auto):
        table = '' if auto is None else f"\n[auto_topics]\nenabled = {str(auto['enabled']).lower()}\n"
        self.settings.write_text(self.base_settings + table)

    def fake_run(self, argv, input='', **kwargs):
        self.inputs.append(input)
        if '/extmsg/outbound' in input:
            payload = dict(Receipt=dict(Delivered=True, MessageID='55'))
        elif '/sendMessage' in input:
            payload = dict(ok=True, result=dict(message_id=42, chat=dict(id=123)))
        else:
            raise AssertionError('unexpected curl invocation')
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload) + '\n200 0.010000 0.020000 0', '')

    def bodies(self):
        return [json.loads(json.loads(line[len('data = '):]))
                for config in self.inputs for line in config.splitlines() if line.startswith('data = ')]

    def cli(self, command, *argv, key='cli-1'):
        with patch.object(b.subprocess, 'run', self.fake_run), patch('sys.stdout', new_callable=io.StringIO) as out:
            run_main(self, 'bridge.py', '--settings', str(self.settings), command,
                     '--body-file', str(self.body), '--idempotency-key', key, *argv)
        return out.getvalue()

    def test_send_and_reply_current_relay_through_gc_on_the_auto_conversation(self):
        for index, command in enumerate(('send', 'reply-current')):
            out = self.cli(command, '--conversation-id', AUTO_ID, key=f'relay-{index}')
            self.assertEqual(out, 'Telegram delivery confirmed; message ID 55\n')
        self.assertEqual([body['conversation'] for body in self.bodies()], [topic_ref(AUTO)] * 2)
        self.assertTrue(all('/extmsg/outbound"' in config for config in self.inputs))

    def test_a_direct_send_into_an_auto_topic_carries_its_thread(self):
        for index, (command, thread_id) in enumerate((('send', AUTO), ('reply-current', AUTO),
                                                      ('reply-current', FRESH))):
            out = self.cli(command, '--format', 'html', '--conversation-id', f'123:topic:{thread_id}',
                           key=f'html-{index}')
            self.assertEqual(out, 'Telegram delivery confirmed; message ID 42\n')
        self.assertEqual([body['message_thread_id'] for body in self.bodies()], [AUTO, AUTO, FRESH])
        self.assertFalse(any('/extmsg/' in config for config in self.inputs))
        with closing(sqlite3.connect(self.ledger)) as db:
            receipts = [json.loads(receipt) for (receipt,) in db.execute('SELECT receipt FROM deliveries')]
        self.assertEqual([receipt['delivered'] for receipt in receipts], [True, True, True])

    def test_an_id_no_served_auto_topic_has_or_an_auto_display_name_is_refused_before_anything_is_sent(self):
        cases = [(AUTO_ON, ('--conversation-id', f'123:topic:{GONE}'), UNRESOLVED),
                 (AUTO_ON, ('--conversation-id', f'123:topic:{UNKNOWN}'), UNRESOLVED),
                 (AUTO_ON, ('--conversation-id', f'123:topic:0{AUTO}'), UNRESOLVED),
                 (AUTO_ON, ('--topic', 'Trip'), 'topic Trip is not configured or not created (run gc telegram topics)'),
                 (AUTO_OFF, ('--conversation-id', AUTO_ID), UNRESOLVED),
                 (None, ('--conversation-id', AUTO_ID), UNRESOLVED)]
        for auto, argv, message in cases:
            self.write_settings(auto)
            with self.subTest(auto=auto, argv=argv):
                with patch.object(b.subprocess, 'run', side_effect=AssertionError('nothing may be sent')), \
                        self.assertRaises(b.TransportError) as raised:
                    run_main(self, 'bridge.py', '--settings', str(self.settings), 'send',
                             '--body-file', str(self.body), '--idempotency-key', 'k', *argv)
                self.assertEqual(str(raised.exception), message)


if __name__ == '__main__':
    unittest.main()
