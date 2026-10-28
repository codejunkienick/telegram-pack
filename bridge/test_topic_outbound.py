"""Outbound in topics of the paired private chat (OPERATIONS.md, "Topics"): a
send on a configured, recorded topic's conversation carries the topic's
message_thread_id on every Telegram request, fallbacks and albums included;
an ask sent in a topic relays its answer on that topic's conversation; the
quota notice follows the triggering message; send and reply-current take
--conversation-id <chat_id>:topic:<thread_id> and --topic <name>. Fake
Telegram and GC, temporary directories; no network."""
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
import test_bridge  # noqa: E402
import test_topics  # noqa: E402
from test_bridge import BridgeTestCase, run_main, write_fake_curl, write_settings  # noqa: E402
from test_topic_inbound import OPS, TOPICS, in_topic, record_topics  # noqa: E402

PACK = Path(__file__).resolve().parents[1]
HTML_REJECTED = 400, dict(ok=False, error_code=400, description="Bad Request: can't parse entities: x")
REJECTED = 400, dict(ok=False, error_code=400, description='Bad Request: wrong file identifier')
UNRECORDED = 8  # no ledger row; "lab" is configured without one
GONE = 13  # a ledger row whose name left the settings


def topic_ref(thread_id, chat='123'):
    return dict(scope_id='test-city', provider='telegram', account_id='test_bot',
                conversation_id=f'{chat}:topic:{thread_id}', parent_conversation_id=chat, kind='thread')


class TopicOutboundCase(BridgeTestCase):
    """The base fixture with TP2's three [[topic]] tables ("ops" recorded as
    thread 7), one fake transport for Telegram and GC, and an album one."""

    # BridgeTests' callback server helpers and AskTests' ask helpers
    # (imported by module, so discovery runs those classes once, in test_bridge).
    start_callback_server = test_bridge.BridgeTests.start_callback_server
    callback_post = test_bridge.BridgeTests.callback_post
    ask_id = test_bridge.AskTests.ask_id
    keyboard = test_bridge.AskTests.keyboard
    press = test_bridge.AskTests.press

    def setUp(self):
        super().setUp()
        self.config['topics'] = TOPICS
        record_topics(self.store)
        self.answers = {}  # Telegram method -> answers used in order before the default
        self.multipart_answers = []
        self.album_calls = []
        self.album_answers = []
        self.unbound = set()  # conversation ids GC routes nowhere
        self.bridge = self.make_bridge(self.config, self.store)

    def make_bridge(self, config, store):
        bridge = b.Bridge(config, store, 'SECRET', 'CAPABILITY', request=self.transport,
                          request_multipart=self.multipart_transport, request_download=self.download_transport,
                          request_media_group=self.album_transport)
        bridge.sleep = lambda delay: None
        return bridge

    def transport(self, url, body, *args, **kwargs):
        self.calls.append((url, body))
        if '/extmsg/' in url:
            if body['message']['conversation']['conversation_id'] in self.unbound:
                return 200, dict(TargetAgentName='', TargetSessionID='')
            return 200, self.accepted(body['message'])
        method = url.rsplit('/', 1)[1]
        if self.answers.get(method):
            return self.answers[method].pop(0)
        if method == 'sendMessage':
            return 200, dict(ok=True, result=dict(message_id=77, chat=dict(id=123)))
        if method in ('editMessageText', 'editMessageCaption'):
            return 200, dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
        if method in ('answerCallbackQuery', 'pinChatMessage'):
            return 200, dict(ok=True, result=True)
        raise AssertionError('unexpected Telegram method ' + method)

    def multipart_transport(self, url, fields, file_field, file_path, timeout=20):
        self.multipart_calls.append((url, dict(fields), file_field, file_path))
        if self.multipart_answers:
            return self.multipart_answers.pop(0)
        return 200, dict(ok=True, result=dict(message_id=88, chat=dict(id=123)))

    def album_transport(self, url, fields, files, timeout=20):
        self.album_calls.append((url, dict(fields), list(files)))
        if self.album_answers:
            return self.album_answers.pop(0)
        return 200, dict(ok=True, result=[dict(message_id=90 + index, chat=dict(id=123))
                                          for index in range(len(files))])

    def request(self, key='topic-1', text='hello', conversation=None, **fields):
        return dict(conversation=conversation or topic_ref(OPS), idempotency_key=key, text=text, **fields)

    def sent(self, method):
        return [body for url, body in self.calls if url.endswith('/' + method)]

    def relays(self):
        """(conversation, provider id, text) of every GC relay, in order."""
        return [(body['message']['conversation'], body['message']['provider_message_id'], body['message']['text'])
                for url, body in self.calls if url.endswith('/extmsg/inbound')]

    def ledger(self, path=None):
        with closing(sqlite3.connect(path or self.path)) as db:
            return dict(db.execute('SELECT key, fingerprint FROM deliveries'))

    def assert_refused(self, receipt, reason):
        self.assertFalse(receipt['delivered'])
        self.assertEqual(receipt['metadata']['reason'], reason)

    def files(self, *kinds):
        names = dict(photo='shot{}.png', video='clip{}.mp4', document='trace{}.zip', voice='note{}.ogg',
                     video_note='circle{}.mp4')
        files = []
        for kind in kinds:
            self.serial = getattr(self, 'serial', 0) + 1
            files.append(dict(path=self.attachment_file(names[kind].format(self.serial)), kind=kind))
        return files


class TopicPublishTests(TopicOutboundCase):
    def test_a_send_on_a_topic_conversation_carries_its_thread(self):
        receipt = self.bridge.publish(self.request(reply_to_message_id='41'))
        self.assertTrue(receipt['delivered'])
        self.assertEqual((receipt['message_id'], receipt['conversation']), ('77', topic_ref(OPS)))
        self.assertEqual(self.calls, [('https://api.telegram.org/botSECRET/sendMessage', dict(
            chat_id=123, text='hello', link_preview_options={'is_disabled': True}, message_thread_id=OPS,
            reply_parameters={'message_id': 41}))])
        self.assertIs(type(self.calls[0][1]['message_thread_id']), int)
        # The conversation is in the identity: the fingerprint is the request's.
        self.assertEqual(self.ledger()['topic-1'], hashlib.sha256(json.dumps(
            self.request(reply_to_message_id='41'), sort_keys=True).encode()).hexdigest())
        self.assertEqual(self.bridge.publish(self.request(reply_to_message_id='41')), receipt)
        self.assertEqual(len(self.calls), 1)

    def test_the_paired_chat_sends_exactly_as_in_a_city_without_topics(self):
        base_path = Path(self.tmp.name) / 'base.sqlite3'
        base = self.make_bridge({k: v for k, v in self.config.items() if k != 'topics'}, b.Store(base_path, 10))
        album = self.files('photo', 'photo')
        requests = [
            dict(text='plain'),
            dict(text='<b>html</b>', format='html'),
            dict(text='quiet', silent=True, reply_to_message_id='5'),
            dict(text='a photo', attachment=self.files('photo')[0]),
            dict(text='two', attachments=album),
            dict(text='Pick one?', keyboard=None),
        ]
        for index, fields in enumerate(requests):
            key = f'general-{index}'
            if 'keyboard' in fields:
                fields['keyboard'] = self.keyboard(key)
            request = dict(fields, conversation=self.bridge.conversation, idempotency_key=key)
            with self.subTest(fields=fields):
                calls = []
                receipts = []
                for bridge in (base, self.bridge):
                    del self.calls[:], self.multipart_calls[:], self.album_calls[:]
                    receipts.append(bridge.publish(json.loads(json.dumps(request))))
                    calls.append((list(self.calls), list(self.multipart_calls), list(self.album_calls)))
                self.assertEqual(calls[1], calls[0])
                self.assertEqual(receipts[1], receipts[0])
                self.assertEqual(receipts[1]['conversation'], self.bridge.conversation)
                self.assertTrue(receipts[1]['delivered'])
                self.assertNotIn('message_thread_id', json.dumps(calls[1]))
        self.assertEqual(self.ledger(), self.ledger(base_path))
        for ask_id in (self.ask_id('general-5'),):
            self.assertIsNone(self.store.get_ask(ask_id)['conversation_id'])

    def test_a_conversation_no_topic_serves_is_refused_and_sends_nothing(self):
        cases = {
            'a thread with no ledger row': topic_ref(UNRECORDED),
            'a recorded thread whose name left the settings': topic_ref(GONE),
            'another chat': topic_ref(OPS, chat='124'),
            'a dm kind': dict(topic_ref(OPS), kind='dm'),
            'a room kind': dict(topic_ref(OPS), kind='room'),
            'no parent': {k: v for k, v in topic_ref(OPS).items() if k != 'parent_conversation_id'},
            'another parent': dict(topic_ref(OPS), parent_conversation_id='124'),
            'another account': dict(topic_ref(OPS), account_id='other_bot'),
            'thread id 0': topic_ref(0),
            'a padded thread id': topic_ref('07'),
            'the paired chat as a thread': dict(self.bridge.conversation, kind='thread'),
            'not an object': '123:topic:7',
        }
        for index, (name, conversation) in enumerate(cases.items()):
            with self.subTest(name):
                receipt = self.bridge.publish(self.request(key=f'refused-{index}', conversation=conversation))
                self.assert_refused(receipt, 'recipient_not_authorized')
                self.assertEqual(receipt['conversation'], self.bridge.conversation)
        unconfigured = self.make_bridge({k: v for k, v in self.config.items() if k != 'topics'}, self.store)
        self.assert_refused(unconfigured.publish(self.request(key='no-topics')), 'recipient_not_authorized')
        self.assertEqual((self.calls, self.multipart_calls, self.ledger()), ([], [], {}))

    def test_the_html_parse_rejection_fallback_stays_in_the_topic(self):
        self.answers['sendMessage'] = [HTML_REJECTED]
        receipt = self.bridge.publish(self.request(text='<b>x</b> &amp; y', format='html'))
        self.assertTrue(receipt['delivered'])
        self.assertEqual(receipt['format_fallback_reason'], 'telegram_html_parse_rejected_400')
        first, second = self.sent('sendMessage')
        self.assertEqual((first['parse_mode'], first['message_thread_id']), ('HTML', OPS))
        self.assertEqual((second['text'], second['message_thread_id']), ('x & y', OPS))
        self.assertNotIn('parse_mode', second)

    def test_every_attachment_kind_carries_the_thread_as_a_multipart_field(self):
        for kind, method in (('photo', 'sendPhoto'), ('document', 'sendDocument'), ('voice', 'sendVoice'),
                             ('video_note', 'sendVideoNote'), ('video', 'sendVideo')):
            with self.subTest(kind):
                del self.multipart_calls[:]
                text = '' if kind == 'video_note' else 'a file'
                receipt = self.bridge.publish(self.request(key='file-' + kind, text=text,
                                                           attachment=self.files(kind)[0]))
                self.assertTrue(receipt['delivered'])
                self.assertEqual(receipt['conversation'], topic_ref(OPS))
                [(url, fields, _, _)] = self.multipart_calls
                self.assertTrue(url.endswith('/' + method))
                self.assertEqual(fields['message_thread_id'], str(OPS))

    def test_the_html_caption_fallback_and_the_attachment_text_fallback_stay_in_the_topic(self):
        self.multipart_answers = [HTML_REJECTED]
        receipt = self.bridge.publish(self.request(key='html-photo', text='<b>shot</b>', format='html',
                                                   attachment=self.files('photo')[0]))
        self.assertEqual(receipt['format_fallback_reason'], 'telegram_html_parse_rejected_400')
        self.assertEqual([fields['message_thread_id'] for _, fields, _, _ in self.multipart_calls], ['7', '7'])
        self.multipart_answers = [REJECTED]
        receipt = self.bridge.publish(self.request(key='rejected-doc', text='trace', silent=True,
                                                   attachment=self.files('document')[0]))
        self.assertTrue(receipt['delivered'])
        self.assertEqual(receipt['fallback_reason'], 'telegram_rejected_not_sent_400')
        [fallback] = self.sent('sendMessage')
        self.assertEqual(fallback['text'], 'trace\n[attachment rejected: telegram_rejected_not_sent_400]')
        self.assertEqual((fallback['message_thread_id'], fallback['disable_notification']), (OPS, True))

    def test_an_album_and_its_text_fallback_stay_in_the_topic(self):
        receipt = self.bridge.publish(self.request(key='album', text='two screens',
                                                   attachments=self.files('photo', 'video')))
        self.assertTrue(receipt['delivered'])
        self.assertEqual((receipt['message_ids'], receipt['conversation']), (['90', '91'], topic_ref(OPS)))
        [(url, fields, _)] = self.album_calls
        self.assertTrue(url.endswith('/sendMediaGroup'))
        self.assertEqual(fields['message_thread_id'], str(OPS))
        self.album_answers = [HTML_REJECTED, REJECTED]
        receipt = self.bridge.publish(self.request(key='album-2', text='<b>two</b>', format='html',
                                                   attachments=self.files('document', 'document')))
        self.assertTrue(receipt['delivered'])
        self.assertEqual(receipt['fallback_reason'], 'telegram_rejected_not_sent_400')
        self.assertEqual([fields['message_thread_id'] for _, fields, _ in self.album_calls[1:]], ['7', '7'])
        [fallback] = self.sent('sendMessage')
        self.assertEqual((fallback['text'], fallback['message_thread_id']),
                         ('two\n[album rejected: telegram_rejected_not_sent_400]', OPS))

    def test_a_silent_send_carries_both_fields(self):
        self.assertTrue(self.bridge.publish(self.request(silent=True))['delivered'])
        [body] = self.sent('sendMessage')
        self.assertEqual((body['disable_notification'], body['message_thread_id']), (True, OPS))
        self.assertTrue(self.bridge.publish(self.request(key='quiet-photo', silent=True,
                                                         attachment=self.files('photo')[0]))['delivered'])
        [(_, fields, _, _)] = self.multipart_calls
        self.assertEqual((fields['disable_notification'], fields['message_thread_id']), ('true', '7'))

    def test_one_key_in_two_conversations_is_a_conflict(self):
        self.assertTrue(self.bridge.publish(self.request(key='shared', conversation=self.bridge.conversation))[
            'delivered'])
        receipt = self.bridge.publish(self.request(key='shared'))
        self.assert_refused(receipt, 'idempotency_key_conflict')
        self.assertEqual(receipt['conversation'], topic_ref(OPS))
        self.assert_refused(self.bridge.publish(self.request(key='shared', text='other')),
                            'idempotency_key_conflict')
        self.assertEqual(len(self.sent('sendMessage')), 1)
        self.assertNotIn('message_thread_id', self.sent('sendMessage')[0])

    def test_the_gc_callback_publishes_on_a_topic_conversation(self):
        port = self.start_callback_server()
        status, receipt = self.callback_post(port, dict(self.request(key='cb-topic'), session_id='mayor'))
        self.assertEqual(status, 200)
        self.assertTrue(receipt['delivered'])
        self.assertEqual(receipt['conversation'], topic_ref(OPS))
        [body] = self.sent('sendMessage')
        self.assertEqual(body['message_thread_id'], OPS)
        status, receipt = self.callback_post(port, self.request(key='cb-gone', conversation=topic_ref(GONE)))
        self.assertEqual(status, 200)
        self.assert_refused(receipt, 'recipient_not_authorized')
        self.assertEqual(len(self.sent('sendMessage')), 1)

    def test_a_pinned_slot_message_stays_in_general(self):
        self.assertTrue(self.bridge.publish(self.request())['delivered'])
        receipt = self.bridge.pin('waiting', 'nothing waits')
        self.assertTrue(receipt['delivered'], receipt)
        slot_send = self.sent('sendMessage')[1]
        self.assertEqual(slot_send['text'], 'nothing waits')
        self.assertNotIn('message_thread_id', slot_send)
        [pin] = self.sent('pinChatMessage')
        self.assertNotIn('message_thread_id', pin)


class TopicAskTests(TopicOutboundCase):
    def ask(self, key='ask-send-1', conversation=None, **fields):
        return self.request(key=key, text='Pick one?', conversation=conversation, keyboard=self.keyboard(key),
                            **fields)

    def topic_press(self, uid=41, key='ask-send-1', thread_id=OPS):
        return in_topic(self.press(uid=uid, key=key), thread_id)

    def test_a_topic_ask_records_its_conversation_and_relays_the_answer_there(self):
        receipt = self.bridge.publish(self.ask())
        self.assertTrue(receipt['delivered'])
        [send] = self.sent('sendMessage')
        self.assertEqual((send['message_thread_id'], send['reply_markup']),
                         (OPS, {'inline_keyboard': self.keyboard()}))
        self.assertEqual(self.store.get_ask(self.ask_id())['conversation_id'], '123:topic:7')
        self.bridge.drain([self.topic_press()])
        self.assertEqual(self.relays(), [(topic_ref(OPS), f'ask-{self.ask_id()}',
                                          '[topic: ops]\n[ask answer | choice: Yes | message: 77]\nquestion: Pick one?')])
        [edit] = self.sent('editMessageText')
        self.assertEqual(edit, dict(chat_id=123, text='Pick one?\n✓ Yes', link_preview_options={'is_disabled': True},
                                    message_id=77, reply_markup={'inline_keyboard': []}))
        self.assertNotIn('message_thread_id', self.sent('answerCallbackQuery')[0])
        self.assertTrue(self.store.get_ask(self.ask_id())['relayed'])
        self.assertEqual(self.store.offset(), 42)

    def test_a_photo_ask_in_a_topic_keeps_its_keyboard_and_thread(self):
        receipt = self.bridge.publish(self.ask(attachment=self.files('photo')[0]))
        self.assertTrue(receipt['delivered'])
        [(_, fields, _, _)] = self.multipart_calls
        self.assertEqual(fields['message_thread_id'], str(OPS))
        self.assertEqual(json.loads(fields['reply_markup']), {'inline_keyboard': self.keyboard()})
        self.bridge.drain([in_topic(self.press(message_id=88), OPS)])
        [(_, _, text)] = self.relays()
        self.assertTrue(text.startswith('[topic: ops]\n[ask answer | choice: Yes | message: 88]'))
        [edit] = self.sent('editMessageCaption')
        self.assertNotIn('message_thread_id', edit)

    def test_a_tap_on_an_unbound_topic_ask_relays_on_the_paired_chat_labelled_not_bound(self):
        self.assertTrue(self.bridge.publish(self.ask())['delivered'])
        self.unbound.add('123:topic:7')
        with patch('sys.stderr', new_callable=io.StringIO) as err:
            self.bridge.drain([self.topic_press()])
        provider = f'ask-{self.ask_id()}'
        answer = '[ask answer | choice: Yes | message: 77]\nquestion: Pick one?'
        self.assertEqual(self.relays(), [
            (topic_ref(OPS), provider, '[topic: ops]\n' + answer),
            (self.bridge.conversation, provider, '[topic: ops | not bound: reply lands in the main chat]\n' + answer)])
        self.assertEqual(err.getvalue(), 'topic thread 7 not bound (msg 77); relayed to the main conversation\n')
        self.assertEqual(len(self.sent('editMessageText')), 1)
        self.assertEqual(self.store.offset(), 42)

    def test_a_tap_after_the_topic_left_the_settings_relays_on_the_paired_chat_labelled_not_configured(self):
        self.assertTrue(self.bridge.publish(self.ask())['delivered'])
        self.bridge = self.make_bridge(dict(self.config, topics=TOPICS[1:]), self.store)
        with patch('sys.stderr', new_callable=io.StringIO) as err:
            self.bridge.drain([self.topic_press()])
        self.assertEqual(self.relays(), [(self.bridge.conversation, f'ask-{self.ask_id()}',
                                          '[topic: thread 7 | not configured: reply lands in the main chat]\n'
                                          '[ask answer | choice: Yes | message: 77]\nquestion: Pick one?')])
        self.assertEqual(err.getvalue(),
                         'topic thread 7 not configured (msg 77); relayed to the main conversation\n')
        self.assertEqual(len(self.sent('editMessageText')), 1)

    def test_a_general_ask_and_a_migrated_ledger_ask_answer_exactly_as_before(self):
        path = Path(self.tmp.name) / 'base.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            for sql in test_topics.TopicLedgerTests.BASE_SCHEMA:
                db.execute(sql)
            db.execute('INSERT INTO cursor VALUES (1, 10)')
            db.execute('INSERT INTO asks(ask_id, question, format, options, message_id) VALUES (?,?,?,?,?)',
                       (self.ask_id('old-ask'), 'Old question?', 'plain', json.dumps(self.keyboard('old-ask')[0]),
                        76))
        store = b.Store(path, 10)
        b.Store(path, 10)  # a second open adds nothing
        with closing(sqlite3.connect(path)) as db:
            columns = [row[1] for row in db.execute('PRAGMA table_info(asks)')]
        self.assertEqual(columns[-2:], ['decided', 'conversation_id'])
        record_topics(store)
        self.bridge = self.make_bridge(self.config, store)
        self.assertIsNone(store.get_ask(self.ask_id('old-ask'))['conversation_id'])
        self.assertTrue(self.bridge.publish(self.ask(key='new-ask', conversation=self.bridge.conversation))[
            'delivered'])
        self.assertIsNone(store.get_ask(self.ask_id('new-ask'))['conversation_id'])
        self.bridge.drain([self.press(uid=41, key='old-ask', message_id=76), self.press(uid=42, key='new-ask')])
        self.assertEqual(self.relays(), [
            (self.bridge.conversation, f"ask-{self.ask_id('old-ask')}",
             '[ask answer | choice: Yes | message: 76]\nquestion: Old question?'),
            (self.bridge.conversation, f"ask-{self.ask_id('new-ask')}",
             '[ask answer | choice: Yes | message: 77]\nquestion: Pick one?')])
        self.assertTrue(all('reply_to_message_id' not in body['message']
                            for url, body in self.calls if '/extmsg/' in url))

    def test_ask_close_closes_a_topic_ask_by_message_id(self):
        self.assertTrue(self.bridge.publish(self.ask())['delivered'])
        receipt = self.bridge.close_ask('Ship it Monday', message_id=77)
        self.assertTrue(receipt['delivered'])
        [edit] = self.sent('editMessageText')
        self.assertEqual((edit['message_id'], edit['text']), (77, 'Pick one?\n✓ Decided: Ship it Monday'))
        self.assertNotIn('message_thread_id', edit)
        self.assertEqual(self.relays(), [])
        self.bridge.drain([self.topic_press()])
        self.assertEqual(self.relays(), [])
        self.assertEqual(self.sent('answerCallbackQuery')[-1]['text'], b.Bridge.ASK_ALREADY_ANSWERED)


class TopicQuotaTests(TopicOutboundCase):
    def waiting(self, episode):
        health = Path(self.tmp.name) / 'health.json'
        self.config['health_file'] = str(health)
        health.write_text(json.dumps(dict(
            observed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            sessions=[dict(template='mayor', status='quota_wait', episode=episode,
                           until=int(time.time() * 1000) + 3600 * 1000)])))

    def notices(self):
        return [body for body in self.sent('sendMessage') if 'quota exhausted' in body['text']]

    def test_the_notice_follows_a_topic_message_once_per_episode(self):
        self.waiting('ep-topic')
        self.bridge.drain([in_topic(self.update(10), OPS), self.update(11), in_topic(self.update(12), OPS)])
        [notice] = self.notices()
        self.assertEqual(notice['message_thread_id'], OPS)
        self.assertEqual(self.store.offset(), 13)

    def test_the_notice_after_a_general_or_fallback_message_goes_to_general(self):
        for episode, update in (('ep-general', self.update(10)),
                                ('ep-unconfigured', in_topic(self.update(11), UNRECORDED))):
            with self.subTest(episode), patch('sys.stderr', new_callable=io.StringIO):
                del self.calls[:]
                self.waiting(episode)
                self.bridge.drain([update])
                [notice] = self.notices()
                self.assertNotIn('message_thread_id', notice)
        self.unbound.add('123:topic:7')
        self.waiting('ep-unbound')
        del self.calls[:]
        with patch('sys.stderr', new_callable=io.StringIO):
            self.bridge.drain([in_topic(self.update(12), OPS)])
        [notice] = self.notices()
        self.assertNotIn('message_thread_id', notice)


class TopicCliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        # --session defaults to $GC_SESSION_ID, else the settings' agent.
        self.enterContext(patch.dict(os.environ))
        os.environ.pop('GC_SESSION_ID', None)
        self.settings = write_settings(self.root)
        self.settings.write_text(self.settings.read_text() + test_topics.topic_tables(('ops', None), ('lab', None)))
        state = self.root / 'state'
        state.mkdir(mode=0o700)
        b.Store(state / 'telegram-bridge.sqlite3', 6).record_topic('ops', OPS, 'created')
        self.body = self.root / 'body.txt'
        self.body.write_text('<b>done</b>')
        self.inputs = []

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

    def cli(self, *argv, key='cli-1'):
        with patch.object(b.subprocess, 'run', self.fake_run), patch('sys.stdout', new_callable=io.StringIO) as out:
            run_main(self, 'bridge.py', '--settings', str(self.settings), 'reply-current',
                     '--body-file', str(self.body), '--idempotency-key', key, *argv)
        return out.getvalue()

    def test_a_topic_conversation_id_relays_to_gc_on_the_topic_conversation(self):
        out = self.cli('--conversation-id', '123:topic:7')
        self.assertEqual(out, 'Telegram delivery confirmed; message ID 55\n')
        self.assertEqual(self.bodies(), [dict(session_id='mayor', conversation=topic_ref(OPS), text='<b>done</b>',
                                              idempotency_key='cli-1')])

    def test_the_topic_flag_resolves_the_same_conversation(self):
        self.cli('--topic', 'ops')
        self.assertEqual(self.bodies()[0]['conversation'], topic_ref(OPS))

    def test_the_paired_chat_id_and_no_flag_keep_the_paired_conversation(self):
        self.cli('--conversation-id', '123', key='k1')
        self.cli(key='k2')
        self.assertEqual([body['conversation'] for body in self.bodies()],
                         [b.Bridge(b.load_settings(self.settings).config, None, 'S', None).conversation] * 2)

    def test_an_html_reply_in_a_topic_publishes_directly_with_its_thread(self):
        for index, argv in enumerate((('--conversation-id', '123:topic:7'), ('--topic', 'ops'))):
            out = self.cli('--format', 'html', *argv, key=f'html-{index}')
            self.assertEqual(out, 'Telegram delivery confirmed; message ID 42\n')
        self.assertEqual([body['message_thread_id'] for body in self.bodies()], [OPS, OPS])
        self.assertFalse(any('/extmsg/' in config for config in self.inputs))
        with closing(sqlite3.connect(self.root / 'state' / 'telegram-bridge.sqlite3')) as db:
            receipts = [json.loads(receipt) for _, receipt in db.execute('SELECT key, receipt FROM deliveries')]
        self.assertEqual([receipt['delivered'] for receipt in receipts], [True, True])

    def test_a_conversation_or_topic_no_topic_serves_is_refused_before_anything_is_sent(self):
        cases = [(('--topic', 'nope'), 'topic nope is not configured or not created (run gc telegram topics)'),
                 (('--topic', 'lab'), 'topic lab is not configured or not created (run gc telegram topics)'),
                 (('--topic', 'OPS'), 'topic OPS is not configured or not created (run gc telegram topics)')]
        for conversation_id in ('123:topic:0', '123:topic:8', '124', '124:topic:7', '123:topic:07', '123:topic:',
                                '123:thread:7', '123:topic:7:x'):
            cases.append((('--conversation-id', conversation_id),
                          'conversation ID is not the paired private chat or a configured topic'))
        for argv, message in cases:
            with self.subTest(argv=argv):
                with patch.object(b.subprocess, 'run', side_effect=AssertionError('nothing may be sent')), \
                        self.assertRaises(b.TransportError) as raised:
                    run_main(self, 'bridge.py', '--settings', str(self.settings), 'send',
                             '--body-file', str(self.body), '--idempotency-key', 'k', *argv)
                self.assertEqual(str(raised.exception), message)

    def test_both_flags_or_topic_on_another_command_exit_through_the_parser(self):
        cases = [['reply-current', '--body-file', 'b', '--idempotency-key', 'k', '--topic', 'ops',
                  '--conversation-id', '123'],
                 ['send', '--body-file', 'b', '--idempotency-key', 'k', '--topic', 'ops',
                  '--conversation-id', '123:topic:7'],
                 ['pin', '--slot', 'waiting', '--body-file', 'b', '--topic', 'ops'],
                 ['ask-close', '--message-id', '5', '--decision', 'x', '--topic', 'ops'],
                 ['topics', '--topic', 'ops'],
                 ['run', '--topic', 'ops']]
        for argv in cases:
            with self.subTest(argv=argv), \
                    patch.object(b, 'load_settings', side_effect=AssertionError('settings are never read')), \
                    patch('sys.stderr', new_callable=io.StringIO) as err, \
                    self.assertRaises(SystemExit) as raised:
                run_main(self, 'bridge.py', *argv)
            self.assertEqual(raised.exception.code, 2)
            self.assertIn('error:', err.getvalue())

    def test_gc_telegram_reply_current_passes_the_topic_flag(self):
        bin_dir = self.root / 'bin'
        write_fake_curl(bin_dir, f'#!{sys.executable}\nimport json, sys, uuid\nfrom pathlib import Path\n'
                        'config = sys.stdin.read()\n'
                        f'Path({str(self.root)!r}, "curl-" + uuid.uuid4().hex).write_text(config)\n'
                        'print(json.dumps({"Receipt": {"Delivered": True, "MessageID": "55"}}))\n'
                        'print("200 0.010000 0.020000 0", end="")\n')
        env = {k: v for k, v in os.environ.items() if not k.startswith('GC_')}
        env.update(PATH=f'{bin_dir}:{env["PATH"]}', TELEGRAM_BRIDGE_SETTINGS=str(self.settings))
        result = subprocess.run(['sh', str(PACK / 'commands' / 'reply-current' / 'run.sh'), '--topic', 'ops',
                                 '--body-file', str(self.body), '--idempotency-key', 'pack-topic'],
                                env=env, cwd=self.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'Telegram delivery confirmed; message ID 55\n')
        [config] = [path.read_text() for path in self.root.glob('curl-*')]
        self.inputs = [config]
        self.assertIn('/extmsg/outbound"', config)
        self.assertEqual(self.bodies(), [dict(session_id='mayor', conversation=topic_ref(OPS), text='<b>done</b>',
                                              idempotency_key='pack-topic')])


if __name__ == '__main__':
    unittest.main()
