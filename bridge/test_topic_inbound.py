"""Inbound in topics of the paired private chat (OPERATIONS.md, "Topics"): a
message written in a topic relays on that topic's GC conversation under its
topic line, falls back to the paired chat's when GC routes the topic nowhere
or no topic is configured for the thread, and voice transcripts and city
commands follow the thread. Fake Telegram, GC and transcriber; temporary
directories; no network."""
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
from test_bridge import BridgeTestCase  # noqa: E402
from test_commands import CommandCase, answer  # noqa: E402

TOPICS = (dict(name='ops', agent='mayor'), dict(name='web', agent='ops-agent'), dict(name='lab', agent='mayor'))
OPS, WEB = 7, 9  # the recorded thread ids; "lab" is configured but has no ledger row
SERVICE_KEYS = ('forum_topic_created', 'forum_topic_edited', 'forum_topic_closed', 'forum_topic_reopened')


def record_topics(store):
    store.record_topic('ops', OPS, 'created')
    store.record_topic('web', WEB, 'recorded')
    store.record_topic('gone', 13, 'created')  # a row whose name left the settings


def in_topic(update, thread_id, **fields):
    message = update.get('message') or update['callback_query']['message']
    message.update(is_topic_message=True, message_thread_id=thread_id, **fields)
    return update


class TopicInboundCase(BridgeTestCase):
    """The base fixture with three [[topic]] tables, two recorded, and a fake
    GC whose answer to an inbound relay depends on the conversation."""

    def setUp(self):
        super().setUp()
        self.config['topics'] = TOPICS
        record_topics(self.store)
        self.bridge = self.make_bridge(self.config)
        # conversation_id -> how GC answers a relay on it; anything else is accepted
        # for the agent its binding names (the paired chat and "ops": mayor).
        self.answers = {}
        self.routes = {'123:topic:9': 'ops-agent'}

    def make_bridge(self, config, store=None):
        return b.Bridge(config, store or self.store, 'SECRET', 'CAPABILITY', request=self.transport,
                        request_multipart=self.multipart_transport, request_download=self.download_transport)

    def transport(self, url, body, *args, **kwargs):
        if not url.endswith('/inbound'):
            return super().transport(url, body, *args, **kwargs)
        self.calls.append((url, body))
        message = body['message']
        how = self.answers.get(message['conversation']['conversation_id'], 'accepted')
        if how == 'unbound':
            return 200, dict(TargetAgentName='', TargetSessionID='')
        if how == 'error':
            return 500, dict(error='internal')
        result = self.accepted(message)
        result['TargetAgentName'] = self.routes.get(message['conversation']['conversation_id'], 'mayor')
        if how == 'other_agent':
            result['TargetAgentName'] = 'helper'
        elif how == 'hydrating':
            del result['TranscriptEntry']
        elif how == 'other_conversation':
            result['TranscriptEntry']['Conversation'] = self.bridge.conversation
        return 200, result

    def relays(self):
        """(conversation, provider id, reply id, text) of every GC relay, in order."""
        return [(body['message']['conversation'], body['message']['provider_message_id'],
                 body['message'].get('reply_to_message_id'), body['message']['text'])
                for url, body in self.calls if url.endswith('/inbound')]

    def drain(self, *updates):
        with patch('sys.stderr', new_callable=io.StringIO) as err:
            self.bridge.drain(list(updates))
        return err.getvalue()

    def topic_ref(self, thread_id):
        return dict(scope_id='test-city', provider='telegram', account_id='test_bot',
                    conversation_id=f'123:topic:{thread_id}', parent_conversation_id='123', kind='thread')


class GeneralTests(TopicInboundCase):
    def bodies(self, bridge, calls, update):
        del calls[:]
        bridge.drain([update])
        return [{k: v for k, v in body['message'].items() if k != 'received_at'} for _, body in calls]

    def test_a_general_message_relays_exactly_as_in_a_city_without_topics(self):
        base_config = {k: v for k, v in self.config.items() if k != 'topics'}
        base_store = b.Store(Path(self.tmp.name) / 'base.sqlite3', 10)
        cases = {
            'no thread': {},
            'not a topic message': dict(is_topic_message=False, message_thread_id=OPS),
            'thread without the flag': dict(message_thread_id=OPS),
            'flag not true': dict(is_topic_message=1, message_thread_id=OPS),
            'thread id 0': dict(is_topic_message=True, message_thread_id=0),
            'thread id a string': dict(is_topic_message=True, message_thread_id=str(OPS)),
            'thread id a bool': dict(is_topic_message=True, message_thread_id=True),
        }
        for uid, (name, fields) in enumerate(cases.items(), 10):
            with self.subTest(name):
                update = self.update(uid)
                update['message'].update(fields, reply_to_message=dict(message_id=OPS))
                expected = self.bodies(self.make_bridge(base_config, base_store), self.calls, json.loads(
                    json.dumps(update)))
                self.assertEqual(self.bodies(self.bridge, self.calls, update), expected)
                [body] = expected
                self.assertEqual(body['conversation'], self.bridge.conversation)
                self.assertEqual(body['text'], 'hello')
                self.assertEqual(body['reply_to_message_id'], str(OPS))  # only a topic's root reply is dropped
                self.assertEqual(self.store.offset(), uid + 1)

    def test_an_empty_target_on_the_paired_chat_still_retains_the_cursor(self):
        self.answers['123'] = 'unbound'
        with self.assertRaises(b.TransportError):
            self.drain(self.update())
        self.assertEqual(len(self.relays()), 1)
        self.assertEqual(self.store.offset(), 10)


class TopicRelayTests(TopicInboundCase):
    def test_a_text_in_a_bound_topic_relays_on_its_conversation_under_its_topic_line(self):
        log = self.drain(in_topic(self.update(), OPS))
        self.assertEqual(self.relays(), [(self.topic_ref(OPS), '10', None, '[topic: ops]\nhello')])
        self.assertEqual(self.store.offset(), 11)
        self.assertEqual(log, '')

    def test_a_topic_with_its_own_agent_is_accepted_only_when_gc_routes_to_that_agent(self):
        self.drain(in_topic(self.update(10), WEB))
        self.assertEqual(self.relays(), [(self.topic_ref(WEB), '10', None, '[topic: web]\nhello')])
        self.assertEqual(self.store.offset(), 11)
        self.routes['123:topic:9'] = 'mayor'
        with self.assertRaises(b.TransportError):
            self.drain(in_topic(self.update(11), WEB))
        self.assertEqual(self.store.offset(), 11)
        self.assertEqual([ref for ref, *_ in self.relays()], [self.topic_ref(WEB)] * 2)

    def test_an_unbound_topic_relays_again_on_the_paired_chat_labelled_and_advances(self):
        self.answers['123:topic:7'] = 'unbound'
        update = in_topic(self.update(), OPS, reply_to_message=dict(message_id=5))
        log = self.drain(update)
        self.assertEqual(self.relays(), [
            (self.topic_ref(OPS), '10', '5', '[topic: ops]\nhello'),
            (self.bridge.conversation, '10', '5', '[topic: ops | not bound: reply lands in the main chat]\nhello'),
        ])
        self.assertEqual(self.store.offset(), 11)
        self.assertEqual(log, 'topic thread 7 not bound (msg 10); relayed to the main conversation\n')

    def test_a_failed_fallback_relay_retains_the_cursor(self):
        self.answers.update({'123:topic:7': 'unbound', '123': 'error'})
        with self.assertRaises(b.TransportError):
            self.drain(in_topic(self.update(), OPS))
        self.assertEqual(len(self.relays()), 2)
        self.assertEqual(self.store.offset(), 10)

    def test_every_other_answer_in_a_topic_retains_the_cursor_and_never_reaches_the_paired_chat(self):
        for how in ('other_agent', 'hydrating', 'error', 'other_conversation'):
            with self.subTest(how):
                del self.calls[:]
                self.answers['123:topic:7'] = how
                with self.assertRaises(b.TransportError):
                    self.drain(in_topic(self.update(), OPS))
                self.assertEqual([ref for ref, *_ in self.relays()], [self.topic_ref(OPS)])
                self.assertEqual(self.store.offset(), 10)

    def test_a_thread_no_topic_is_configured_or_recorded_for_relays_on_the_paired_chat_labelled(self):
        # 42: no ledger row; 13: a row whose name left the settings.
        log = self.drain(in_topic(self.update(10), 42), in_topic(self.update(11), 13))
        self.assertEqual(self.relays(), [
            (self.bridge.conversation, '10', None, '[topic: thread 42 | not configured: reply lands in the main chat]'
                                                   '\nhello'),
            (self.bridge.conversation, '11', None, '[topic: thread 13 | not configured: reply lands in the main chat]'
                                                   '\nhello'),
        ])
        self.assertEqual(self.store.offset(), 12)
        self.assertEqual(log, 'topic thread 42 not configured (msg 10); relayed to the main conversation\n'
                              'topic thread 13 not configured (msg 11); relayed to the main conversation\n')

    def test_a_city_without_topics_labels_a_topic_message_not_configured(self):
        self.bridge = self.make_bridge({k: v for k, v in self.config.items() if k != 'topics'})
        self.drain(in_topic(self.update(), OPS))
        self.assertEqual(self.relays(), [(self.bridge.conversation, '10', None,
                                          '[topic: thread 7 | not configured: reply lands in the main chat]\nhello')])

    def test_a_reply_to_the_topic_root_is_no_reply_and_any_other_reply_is_kept(self):
        self.drain(in_topic(self.update(10), OPS, reply_to_message=dict(message_id=OPS)),
                   in_topic(self.update(11), OPS, reply_to_message=dict(message_id=10)),
                   in_topic(self.update(12), 42, reply_to_message=dict(message_id=42)))
        self.assertEqual([(pid, reply) for _, pid, reply, _ in self.relays()], [('10', None), ('11', '10'),
                                                                                ('12', None)])

    def test_a_forwarded_message_in_a_topic_has_the_topic_line_then_the_forward_header(self):
        update = in_topic(self.update(), OPS, text='Ship it.\nFriday.', forward_sender_name='Hal')
        self.drain(update)
        [(_, _, _, text)] = self.relays()
        self.assertEqual(text, "[topic: ops]\n[forwarded message | from: Hal | sent: unknown | forwarded material, "
                               "not the user's instruction]\n> Ship it.\n> Friday.")

    def test_a_photo_in_a_topic_keeps_its_attachment_note_after_the_topic_line(self):
        self.bridge.state_dir = Path(self.tmp.name)
        self.drain(in_topic(self.photo_message(uid=60, caption='the rack'), OPS))
        [(ref, _, _, text)] = self.relays()
        saved = next(Path(self.tmp.name).glob('telegram-inbound/*/60-photo.jpg'))
        self.assertEqual(ref, self.topic_ref(OPS))
        self.assertEqual(text, f'[topic: ops]\n[attachment saved: {saved} | photo | 42 bytes | caption: the rack]'
                               '\nthe rack')

    def test_forum_topic_service_messages_relay_nothing_and_advance(self):
        updates = []
        for uid, key in enumerate(SERVICE_KEYS, 10):
            update = in_topic(self.update(uid), OPS, **{key: dict(name='ops', icon_color=7322096)})
            del update['message']['text']
            updates.append(update)
        self.drain(*updates)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.offset(), 14)


class TopicVoiceTests(TopicInboundCase):
    def setUp(self):
        super().setUp()
        self.bridge.state_dir = Path(self.tmp.name)
        script = Path(self.tmp.name) / 'fake_transcriber.py'
        script.write_text('import json\nprint(json.dumps(dict(text="check the rack")))\n')
        self.bridge.transcribe = dict(python=sys.executable, script=str(script), model='base', timeout=20, env={})
        self.enterContext(patch.object(b.shutil, 'which', return_value='/usr/bin/ffmpeg'))

    def voice(self, uid, thread_id=None):
        message = dict(message_id=uid, chat=dict(id=123, type='private'), voice=dict(
            file_id='voicefile', file_unique_id='v1', file_size=500, duration=7), **{'from': dict(id=123, is_bot=False)})
        update = dict(update_id=uid, message=message)
        return in_topic(update, thread_id) if thread_id is not None else update

    def pending(self, uid):
        [path] = Path(self.tmp.name).glob(f'telegram-inbound/*/{uid}-transcript.pending')
        return path

    def restarted(self):
        """A bridge started after this one; returns how many pending files it
        queued, once its worker has relayed them."""
        bridge = self.make_bridge(self.config)
        bridge.state_dir, bridge.transcribe = self.bridge.state_dir, self.bridge.transcribe
        with patch('sys.stderr', new_callable=io.StringIO):
            queued = bridge.reconcile_pending()
            bridge.transcribe_queue.join()
        return queued

    def test_a_voice_note_in_a_topic_records_its_thread_and_its_transcript_follows_the_topic(self):
        with patch.object(self.bridge, '_enqueue_transcription'):
            self.drain(self.voice(70, OPS))
        self.assertEqual(json.loads(self.pending(70).read_text())['thread_id'], OPS)
        self.assertEqual(self.restarted(), 1)
        (note_ref, note_id, _, note), (ref, pid, _, transcript) = self.relays()
        audio = next(Path(self.tmp.name).glob('telegram-inbound/*/70-voice.ogg'))
        self.assertEqual((note_ref, note_id), (self.topic_ref(OPS), '70'))
        self.assertEqual(note, f'[topic: ops]\n[attachment saved: {audio} | voice 0:07 | transcribing]')
        self.assertEqual((ref, pid), (self.topic_ref(OPS), '70-transcript'))
        self.assertEqual(transcript, f'[topic: ops]\n[voice 0:07 transcript (msg 70): check the rack] (audio: {audio})')
        self.assertFalse(list(Path(self.tmp.name).glob('telegram-inbound/*/*.pending')))

    def test_a_transcript_resolves_its_thread_when_it_relays(self):
        with patch.object(self.bridge, '_enqueue_transcription'):
            self.drain(self.voice(71, 42))
        self.assertEqual(self.relays()[0][0], self.bridge.conversation)  # thread 42: not configured yet
        self.store.record_topic('lab', 42, 'recorded')
        self.assertEqual(self.restarted(), 1)
        ref, pid, _, text = self.relays()[1]
        self.assertEqual((ref, pid), (self.topic_ref(42), '71-transcript'))
        self.assertTrue(text.startswith('[topic: lab]\n[voice 0:07 transcript (msg 71): check the rack] (audio: '))

    def test_an_unbound_topic_gets_its_transcript_on_the_paired_chat_labelled(self):
        self.answers['123:topic:7'] = 'unbound'
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            self.bridge.drain([self.voice(72, OPS)])
            self.bridge.transcribe_queue.join()
        relays = self.relays()
        self.assertEqual([(ref['conversation_id'], pid) for ref, pid, _, _ in relays],
                         [('123:topic:7', '72'), ('123', '72'), ('123:topic:7', '72-transcript'),
                          ('123', '72-transcript')])
        self.assertTrue(relays[3][3].startswith('[topic: ops | not bound: reply lands in the main chat]\n'
                                                '[voice 0:07 transcript (msg 72): '))
        self.assertEqual([line for line in log.getvalue().splitlines() if line.startswith('topic')],
                         ['topic thread 7 not bound (msg 72); relayed to the main conversation'] * 2)

    def test_a_pending_file_without_a_thread_relays_on_the_paired_chat(self):
        with patch.object(self.bridge, '_enqueue_transcription'):
            self.drain(self.voice(73))
        self.assertNotIn('thread_id', json.loads(self.pending(73).read_text()))
        self.assertEqual(self.restarted(), 1)
        ref, pid, _, text = self.relays()[1]
        self.assertEqual((ref, pid), (self.bridge.conversation, '73-transcript'))
        self.assertTrue(text.startswith('[voice 0:07 transcript (msg 73): check the rack] (audio: '))

    def test_a_malformed_thread_in_a_pending_file_is_refused(self):
        with patch.object(self.bridge, '_enqueue_transcription'):
            self.drain(self.voice(74, OPS))
        path = self.pending(74)
        for thread in (0, -1, '7', True, None):
            with self.subTest(thread=thread):
                state = json.loads(path.read_text())
                state['thread_id'] = thread
                path.write_text(json.dumps(state))
                bridge = self.make_bridge(self.config)
                bridge.state_dir, bridge.transcribe = self.bridge.state_dir, self.bridge.transcribe
                with patch('sys.stderr', new_callable=io.StringIO) as log:
                    self.assertEqual(bridge.reconcile_pending(), 0)
                self.assertEqual(log.getvalue(), 'voice pending file refused (msg 74)\n')


class TopicCommandTests(CommandCase):
    def setUp(self):
        super().setUp()
        record_topics(self.store)
        self.bridge = self.make_bridge(topics=TOPICS)

    def fingerprints(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            return dict(db.execute('SELECT key, fingerprint FROM deliveries').fetchall())

    @staticmethod
    def fingerprint(identity):
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def test_a_command_in_a_topic_sends_into_its_thread_and_edits_by_message_id(self):
        keyboard = [[dict(text='Again', callback_data='deploy:again')]]
        self.set_handler(answer(dict(type='send', text='ok'), dict(type='edit', message_id=501, text='done'),
                                dict(type='send', text='<b>go</b>', format='html', keyboard=keyboard)))
        for message in (in_topic(self.message('/deploy', 40), OPS), in_topic(self.message('/deploy', 42), 42)):
            del self.telegram.calls[:]
            self.drain(message)
            thread = message['message']['message_thread_id']
            self.assertEqual(self.telegram.calls, [
                ('sendMessage', dict(chat_id=123, text='ok', link_preview_options={'is_disabled': True},
                                     message_thread_id=thread)),
                ('editMessageText', dict(chat_id=123, text='done', link_preview_options={'is_disabled': True},
                                         message_id=501)),
                ('sendMessage', dict(chat_id=123, text='<b>go</b>', link_preview_options={'is_disabled': True},
                                     reply_markup={'inline_keyboard': keyboard}, message_thread_id=thread,
                                     parse_mode='HTML')),
            ])
        self.assertNotIn('thread', self.inputs()[0])  # the handler input is unchanged
        self.assertEqual(set(self.inputs()[0]), set(self.inputs()[1]))
        prints = self.fingerprints()
        self.assertEqual(prints['command:40:0'], self.fingerprint(dict(type='send', text='ok', format='plain',
                                                                       message_thread_id=OPS)))
        self.assertEqual(prints['command:40:1'], self.fingerprint(dict(type='edit', text='done', format='plain',
                                                                       message_id=501)))
        self.assertEqual(prints['command:42:0'], self.fingerprint(dict(type='send', text='ok', format='plain',
                                                                       message_thread_id=42)))

    def test_a_failure_notice_in_a_topic_goes_into_its_thread(self):
        self.set_handler('raise SystemExit(3)')
        self.drain(in_topic(self.message('/deploy'), OPS))
        self.assertEqual(self.telegram.calls, [('sendMessage', dict(
            chat_id=123, text='command deploy failed', link_preview_options={'is_disabled': True},
            message_thread_id=OPS))])
        self.assertEqual(self.fingerprints()['command:40:notice'], self.fingerprint(dict(
            type='send', text='command deploy failed', format='plain', message_thread_id=OPS)))

    def test_a_press_on_a_message_in_a_topic_sends_into_its_thread(self):
        self.set_handler(answer(dict(type='send', text='noted'), dict(type='edit', message_id=501, text='done'),
                                dict(type='answer_callback', text='Done')))
        self.drain(in_topic(self.callback('deploy:yes'), WEB))
        self.assertEqual(self.telegram.calls, [
            ('sendMessage', dict(chat_id=123, text='noted', link_preview_options={'is_disabled': True},
                                 message_thread_id=WEB)),
            ('editMessageText', dict(chat_id=123, text='done', link_preview_options={'is_disabled': True},
                                     message_id=501)),
            ('answerCallbackQuery', dict(callback_query_id='query-41', text='Done')),
        ])
        self.set_handler('raise SystemExit(3)')
        del self.telegram.calls[:]
        self.drain(in_topic(self.callback('deploy:yes', uid=43), WEB))
        self.assertEqual(self.telegram.calls, [
            ('sendMessage', dict(chat_id=123, text='command deploy failed', link_preview_options={'is_disabled': True},
                                 message_thread_id=WEB)),
            ('answerCallbackQuery', dict(callback_query_id='query-43')),
        ])

    def test_general_command_sends_keep_their_request_bodies_and_fingerprints(self):
        self.set_handler(answer(dict(type='send', text='ok')))
        general = self.message('/deploy', 40)
        general['message'].update(is_topic_message=False, message_thread_id=OPS)
        self.drain(general, self.callback('deploy:yes', uid=42))
        self.set_handler('raise SystemExit(3)')
        self.drain(self.message('/deploy', 44))
        self.assertEqual(self.telegram.of('sendMessage'), [
            dict(chat_id=123, text='ok', link_preview_options={'is_disabled': True}),
            dict(chat_id=123, text='ok', link_preview_options={'is_disabled': True}),
            dict(chat_id=123, text='command deploy failed', link_preview_options={'is_disabled': True}),
        ])
        prints = self.fingerprints()
        ok = self.fingerprint(dict(type='send', text='ok', format='plain'))
        self.assertEqual((prints['command:40:0'], prints['command:42:0']), (ok, ok))
        self.assertEqual(prints['command:44:notice'],
                         self.fingerprint(dict(type='send', text='command deploy failed', format='plain')))


if __name__ == '__main__':
    unittest.main()
