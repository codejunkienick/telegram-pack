"""Inbound in topics the paired user creates ([auto_topics], OPERATIONS.md,
"Topics the user creates"): a new thread is recorded, named from Telegram's
service data, bound to the agent through the GC API and relayed on its own
conversation under its topic line, falling back to the paired chat's,
labelled not bound, whenever no binding is confirmed. General, the settings
topics and a file without [auto_topics] relay exactly as before. Fake
Telegram, GC and transcriber; temporary directories; no network."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
import testports  # noqa: E402
import test_topic_inbound  # noqa: E402
from test_commands import CommandCase  # noqa: E402
from test_topic_inbound import OPS, WEB, TopicInboundCase, in_topic  # noqa: E402
from test_topics import GET_ME, MODE_OFF, USERS_ALLOWED  # noqa: E402

AUTO_ON = {'enabled': True}
AUTO_OFF = {'enabled': False}
NEW, OTHER = 601, 602  # threads the paired user created; no row of either kind yet
FIRST_AUTO = 600  # the fake GC binds conversations of threads from here on statefully
RECORD_FIELDS = dict(SessionID='', Status='active')


def service(uid, thread_id, key, **fields):
    """A forum topic service message (no text) in `thread_id`."""
    update = in_topic(dict(update_id=uid, message=dict(message_id=uid, chat=dict(id=123, type='private'),
                                                       **{'from': dict(id=123, is_bot=False)})), thread_id)
    update['message'][key] = fields
    return update


class AutoTopicInboundCase(TopicInboundCase):
    """The topic inbound fixture with [auto_topics] on and a fake GC that
    binds like GC: /bind records each body and answers a SessionBindingRecord
    (or the failure bind_answers names for the conversation), and /inbound on
    an auto topic's conversation routes nowhere until a bind on it succeeded."""

    def setUp(self):
        super().setUp()
        self.config['auto_topics'] = AUTO_ON
        self.bridge = self.make_bridge(self.config)
        self.bindings = {}  # conversation id -> the agent a confirmed bind named
        self.bind_answers = {}  # conversation id -> how /bind answers; anything else binds
        self.bind_headers = []

    def transport(self, url, body, *args, **kwargs):
        if url.endswith('/extmsg/bind'):
            self.calls.append((url, body))
            self.bind_headers.append(kwargs.get('headers'))
            conversation = body['conversation']['conversation_id']
            how = self.bind_answers.get(conversation, 'bound')
            if how == 'not_sent':
                raise b.RequestNotSent('network request not sent', 7, 'connect')
            if how == 'unconfirmed':
                raise b.RequestUnconfirmed('network request failed; outcome unconfirmed', 52, 'after-send')
            if type(how) is int:
                return how, dict(detail='refused')
            if how == 'malformed':
                return 200, ['bound']
            record = dict(RECORD_FIELDS, ID='binding-1', AgentName=body['agent_name'],
                          Conversation=body['conversation'])
            if how == 'other_agent':
                record['AgentName'] = 'helper'
            elif how == 'session':
                record['SessionID'] = 'session-1'
            elif how == 'ended':
                record['Status'] = 'ended'
            elif how == 'unrouted':
                pass  # confirmed, but GC still routes the conversation nowhere
            else:
                self.bindings[conversation] = body['agent_name']
            return 200, record
        if url.endswith('/inbound'):
            conversation = body['message']['conversation']['conversation_id']
            thread = conversation.rpartition(':topic:')[2]
            if thread.isdigit() and int(thread) >= FIRST_AUTO and conversation not in self.answers:
                self.calls.append((url, body))
                if conversation not in self.bindings:
                    return 200, dict(TargetAgentName='', TargetSessionID='')
                result = self.accepted(body['message'])
                result['TargetAgentName'] = self.bindings[conversation]
                return 200, result
        return super().transport(url, body, *args, **kwargs)

    def binds(self):
        """The body of every GC bind, in order."""
        return [body for url, body in self.calls if url.endswith('/extmsg/bind')]

    def gc_calls(self):
        """('bind', conversation id) or ('inbound', conversation id) of every GC call, in order."""
        return [(url.rsplit('/', 1)[1],
                 (body.get('conversation') or body['message']['conversation'])['conversation_id'])
                for url, body in self.calls if '/extmsg/' in url]

    def row(self, thread_id):
        return next((row for row in self.store.auto_topic_rows() if row['thread_id'] == thread_id), None)

    def bind_body(self, thread_id, agent='mayor'):
        return dict(conversation=self.topic_ref(thread_id), agent_name=agent)


class AutoTopicRelayTests(AutoTopicInboundCase):
    def test_a_message_in_a_new_thread_is_bound_then_relayed_on_its_own_conversation(self):
        log = self.drain(in_topic(self.update(10), NEW))
        self.assertEqual(self.binds(), [self.bind_body(NEW)])
        self.assertEqual(self.calls[0][0], self.bridge.base + '/v0/city/test-city/extmsg/bind')
        self.assertEqual(self.bind_headers, [{'X-GC-Request': '1'}])
        self.assertEqual(self.relays(), [(self.topic_ref(NEW), '10', None, f'[topic: thread {NEW}]\nhello')])
        self.assertEqual(self.store.offset(), 11)
        row = self.row(NEW)
        self.assertEqual((row['name'], row['agent']), (None, 'mayor'))
        self.assertIsNotNone(row['bound_at'])
        self.assertEqual(log, '')
        self.assertEqual(self.store.last_accepted_in(f'123:topic:{NEW}'), 10)

    def test_a_bound_topic_relays_with_no_second_bind(self):
        self.drain(in_topic(self.update(10), NEW), in_topic(self.update(11), NEW, reply_to_message=dict(message_id=10)))
        self.assertEqual(self.gc_calls(), [('bind', f'123:topic:{NEW}'), ('inbound', f'123:topic:{NEW}'),
                                           ('inbound', f'123:topic:{NEW}')])
        self.assertEqual(self.relays()[1], (self.topic_ref(NEW), '11', '10', f'[topic: thread {NEW}]\nhello'))
        self.assertEqual(self.store.offset(), 12)

    def test_a_reply_to_the_topic_root_is_no_reply(self):
        self.drain(in_topic(self.update(10), NEW, reply_to_message=dict(message_id=NEW)))
        self.assertEqual(self.relays(), [(self.topic_ref(NEW), '10', None, f'[topic: thread {NEW}]\nhello')])

    def test_the_creation_message_records_names_and_binds_and_relays_nothing(self):
        log = self.drain(service(10, NEW, 'forum_topic_created', name='Weekly plan', icon_color=7322096))
        self.assertEqual(self.binds(), [self.bind_body(NEW)])
        self.assertEqual(self.relays(), [])
        self.assertEqual(self.store.offset(), 11)
        self.assertEqual(log, '')
        row = self.row(NEW)
        self.assertEqual((row['name'], row['name_implicit']), ('Weekly plan', False))
        self.assertIsNotNone(row['bound_at'])
        self.drain(in_topic(self.update(11), NEW))
        self.assertEqual(len(self.binds()), 1)
        self.assertEqual(self.relays(), [(self.topic_ref(NEW), '11', None, '[topic: Weekly plan]\nhello')])

    def test_an_implicit_name_is_recorded_as_implicit(self):
        self.drain(service(10, NEW, 'forum_topic_created', name='hello', is_name_implicit=True),
                   service(11, OTHER, 'forum_topic_created', name='Plan', is_name_implicit='yes'))
        self.assertEqual([(r['name'], r['name_implicit']) for r in self.store.auto_topic_rows()],
                         [('hello', True), ('Plan', False)])

    def test_a_creation_message_without_a_name_records_the_thread_unnamed(self):
        self.drain(service(10, NEW, 'forum_topic_created', icon_color=7322096))
        self.assertIsNone(self.row(NEW)['name'])
        self.drain(in_topic(self.update(11), NEW))
        self.assertEqual(self.relays()[0][3], f'[topic: thread {NEW}]\nhello')

    def test_a_reply_carrying_the_creation_names_a_row_without_a_name(self):
        root = dict(message_id=NEW, forum_topic_created=dict(name='Plan', icon_color=7322096))
        self.drain(in_topic(self.update(10), NEW, reply_to_message=root))
        self.assertEqual(self.relays(), [(self.topic_ref(NEW), '10', None, '[topic: Plan]\nhello')])
        # A row that already has a name keeps it.
        self.drain(service(11, NEW, 'forum_topic_edited', name='Roadmap'))
        self.drain(in_topic(self.update(12), NEW, reply_to_message=root))
        self.assertEqual(self.relays()[1][3], '[topic: Roadmap]\nhello')
        self.assertEqual(self.row(NEW)['name'], 'Roadmap')

    def test_a_name_with_delimiters_relays_sanitized_and_a_settings_name_is_suffixed(self):
        self.drain(service(10, NEW, 'forum_topic_created', name='a [b|c] d'),
                   service(11, OTHER, 'forum_topic_created', name='OPS'),
                   in_topic(self.update(12), NEW), in_topic(self.update(13), OTHER))
        self.assertEqual([text for *_, text in self.relays()],
                         ['[topic: a b c d]\nhello', f'[topic: OPS #{OTHER}]\nhello'])

    def test_an_edit_renames_and_an_icon_only_edit_does_not(self):
        self.drain(service(10, NEW, 'forum_topic_created', name='New topic', is_name_implicit=True),
                   service(11, NEW, 'forum_topic_edited', name='Roadmap'),
                   in_topic(self.update(12), NEW),
                   service(13, NEW, 'forum_topic_edited', icon_custom_emoji_id='5312536423851630001'),
                   in_topic(self.update(14), NEW))
        self.assertEqual([text for *_, text in self.relays()], ['[topic: Roadmap]\nhello'] * 2)
        self.assertEqual((self.row(NEW)['name'], self.row(NEW)['name_implicit']), ('Roadmap', False))
        self.assertEqual(self.store.offset(), 15)

    def test_an_edit_in_a_settings_topic_a_leftover_row_or_an_unknown_thread_changes_nothing(self):
        self.drain(service(10, OPS, 'forum_topic_edited', name='Renamed'),
                   service(11, 13, 'forum_topic_edited', name='Renamed'),
                   service(12, NEW, 'forum_topic_edited', name='Renamed'),
                   service(13, OPS, 'forum_topic_created', name='ops'),
                   service(14, 13, 'forum_topic_created', name='gone'))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.auto_topic_rows(), [])
        self.assertEqual(self.store.offset(), 15)

    def test_a_first_update_that_relays_nothing_records_and_binds_its_topic(self):
        sticker = in_topic(self.update(11), OTHER)
        del sticker['message']['text']
        sticker['message']['sticker'] = dict(file_id='sticker-1')
        start = in_topic(self.update(10), NEW)
        start['message']['text'] = '/start'
        log = self.drain(start, sticker,
                         service(12, NEW + 2, 'forum_topic_closed'))
        self.assertEqual(self.binds(), [self.bind_body(NEW), self.bind_body(OTHER), self.bind_body(NEW + 2)])
        self.assertEqual(self.relays(), [])
        self.assertEqual([(row['thread_id'], row['name']) for row in self.store.auto_topic_rows()],
                         [(NEW, None), (OTHER, None), (NEW + 2, None)])
        self.assertTrue(all(row['bound_at'] for row in self.store.auto_topic_rows()))
        self.assertEqual((log, self.store.offset()), ('', 13))
        self.drain(in_topic(self.update(13), NEW))
        self.assertEqual(len(self.binds()), 3)
        self.assertEqual(self.relays(), [(self.topic_ref(NEW), '13', None, f'[topic: thread {NEW}]\nhello')])

    def test_a_service_message_from_anyone_else_changes_nothing(self):
        update = service(10, NEW, 'forum_topic_created', name='Plan')
        update['message']['from'] = dict(id=124, is_bot=False)
        self.drain(update)
        self.assertEqual((self.calls, self.store.auto_topic_rows()), ([], []))


class AutoTopicBindFailureTests(AutoTopicInboundCase):
    FAILURES = ((409, 'gc_conflict'), (400, 'gc_rejected_400'), (500, 'gc_rejected_500'),
                ('other_agent', 'gc_unconfirmed'), ('session', 'gc_unconfirmed'), ('ended', 'gc_unconfirmed'),
                ('malformed', 'gc_unconfirmed'), ('unconfirmed', 'gc_unconfirmed'), ('not_sent', 'gc_unreachable'))

    def test_an_unconfirmed_bind_relays_on_the_paired_chat_labelled_and_the_next_message_binds_again(self):
        for index, (how, category) in enumerate(self.FAILURES):
            thread, uid = NEW + index, 100 + 2 * index
            with self.subTest(how=how):
                del self.calls[:]
                conversation = f'123:topic:{thread}'
                self.bind_answers[conversation] = how
                log = self.drain(in_topic(self.update(uid), thread, reply_to_message=dict(message_id=5)))
                self.assertEqual(self.binds(), [self.bind_body(thread)])
                self.assertEqual(self.relays(), [(self.bridge.conversation, str(uid), '5',
                                                  f'[topic: thread {thread} | not bound: reply lands in the main chat]'
                                                  '\nhello')])
                self.assertEqual(log, f'auto topic thread {thread} not bound ({category})\ntopic thread {thread} '
                                      f'not bound (msg {uid}); relayed to the main conversation\n')
                self.assertEqual(self.store.offset(), uid + 1)
                self.assertIsNone(self.row(thread)['bound_at'])
                self.assertEqual(self.store.last_accepted_in('123'), uid)
                del self.calls[:], self.bind_answers[conversation]
                self.assertEqual(self.drain(in_topic(self.update(uid + 1), thread)), '')
                self.assertEqual(self.gc_calls(), [('bind', conversation), ('inbound', conversation)])
                self.assertIsNotNone(self.row(thread)['bound_at'])

    def test_a_failed_bind_on_the_creation_message_is_logged_and_the_cursor_moves(self):
        self.bind_answers[f'123:topic:{NEW}'] = 409
        log = self.drain(service(10, NEW, 'forum_topic_created', name='Plan'))
        self.assertEqual((len(self.binds()), self.relays()), (1, []))
        self.assertEqual(log, f'auto topic thread {NEW} not bound (gc_conflict)\n')
        self.assertEqual(self.store.offset(), 11)
        del self.bind_answers[f'123:topic:{NEW}']
        self.drain(in_topic(self.update(11), NEW))
        self.assertEqual(len(self.binds()), 2)
        self.assertEqual(self.relays(), [(self.topic_ref(NEW), '11', None, '[topic: Plan]\nhello')])

    def test_a_failed_fallback_relay_retains_the_cursor(self):
        self.bind_answers[f'123:topic:{NEW}'] = 409
        self.answers['123'] = 'error'
        with self.assertRaises(b.TransportError):
            self.drain(in_topic(self.update(10), NEW))
        self.assertEqual(self.gc_calls(), [('bind', f'123:topic:{NEW}'), ('inbound', '123')])
        self.assertEqual(self.store.offset(), 10)


class AutoTopicUnboundTests(AutoTopicInboundCase):
    """GC routes a topic the bridge recorded bound nowhere: its binding was
    removed by hand."""

    def setUp(self):
        super().setUp()
        self.store.add_auto_topic(NEW, 'mayor')
        self.store.set_auto_topic_bound(NEW, True)  # no binding in the fake GC

    def test_the_bridge_binds_once_more_and_relays_on_the_topic(self):
        log = self.drain(in_topic(self.update(10), NEW))
        conversation = f'123:topic:{NEW}'
        self.assertEqual(self.gc_calls(),
                         [('inbound', conversation), ('bind', conversation), ('inbound', conversation)])
        self.assertEqual(self.relays(), [(self.topic_ref(NEW), '10', None, f'[topic: thread {NEW}]\nhello')] * 2)
        self.assertIsNotNone(self.row(NEW)['bound_at'])
        self.assertEqual((log, self.store.offset()), ('', 11))

    def test_a_failed_rebind_relays_on_the_paired_chat_labelled(self):
        self.bind_answers[f'123:topic:{NEW}'] = 409
        log = self.drain(in_topic(self.update(10), NEW))
        self.assertEqual(self.relays(), [
            (self.topic_ref(NEW), '10', None, f'[topic: thread {NEW}]\nhello'),
            (self.bridge.conversation, '10', None, f'[topic: thread {NEW} | not bound: reply lands in the main chat]'
                                                   '\nhello')])
        self.assertEqual(log, f'auto topic thread {NEW} not bound (gc_conflict)\n'
                              f'topic thread {NEW} not bound (msg 10); relayed to the main conversation\n')
        self.assertIsNone(self.row(NEW)['bound_at'])
        self.assertEqual(self.store.offset(), 11)

    def test_a_topic_gc_still_routes_nowhere_after_the_rebind_relays_on_the_paired_chat(self):
        self.bind_answers[f'123:topic:{NEW}'] = 'unrouted'
        log = self.drain(in_topic(self.update(10), NEW))
        conversation = f'123:topic:{NEW}'
        self.assertEqual(self.gc_calls(), [('inbound', conversation), ('bind', conversation), ('inbound', conversation),
                                           ('inbound', '123')])
        self.assertEqual(len({(pid, reply) for _, pid, reply, _ in self.relays()}), 1)  # one provider and reply id
        self.assertEqual(log, f'topic thread {NEW} not bound (msg 10); relayed to the main conversation\n')
        self.assertIsNone(self.row(NEW)['bound_at'])
        self.assertEqual(self.store.offset(), 11)

    def test_every_other_answer_retains_the_cursor_and_never_reaches_the_paired_chat(self):
        self.bindings[f'123:topic:{NEW}'] = 'mayor'
        for how in ('other_agent', 'hydrating', 'error', 'other_conversation'):
            with self.subTest(how):
                del self.calls[:]
                self.answers[f'123:topic:{NEW}'] = how
                with self.assertRaises(b.TransportError):
                    self.drain(in_topic(self.update(), NEW))
                self.assertEqual(self.gc_calls(), [('inbound', f'123:topic:{NEW}')])
                self.assertEqual(self.store.offset(), 10)
                self.assertIsNotNone(self.row(NEW)['bound_at'])

    def test_a_row_keeps_the_agent_it_was_recorded_for(self):
        # The settings' agent when the bridge first saw the thread; a row
        # recorded under another binds and relays for that one.
        self.store.add_auto_topic(OTHER, 'helper')
        self.drain(in_topic(self.update(10), OTHER))
        self.assertEqual(self.binds(), [self.bind_body(OTHER, 'helper')])
        self.assertEqual(self.relays(), [(self.topic_ref(OTHER), '10', None, f'[topic: thread {OTHER}]\nhello')])


class AutoTopicIdentityTests(AutoTopicInboundCase):
    """Where [auto_topics] changes nothing."""

    UPDATES = (
        ('general', lambda case: case.update(10)),
        ('settings topic', lambda case: in_topic(case.update(11), OPS)),
        ('settings topic GC routes nowhere', lambda case: in_topic(case.update(12), WEB)),
        ('settings topic, reply', lambda case: in_topic(case.update(13), OPS, reply_to_message=dict(message_id=3))),
        ('settings topic, root reply carrying the creation', lambda case: in_topic(
            case.update(14), OPS, reply_to_message=dict(message_id=OPS, forum_topic_created=dict(name='x')))),
        ('settings topic, creation message', lambda case: service(15, OPS, 'forum_topic_created', name='ops')),
        ('leftover topics row', lambda case: in_topic(case.update(16), 13)),
        ('leftover topics row, creation message', lambda case: service(17, 13, 'forum_topic_created', name='gone')),
    )

    def run_with(self, auto, store_name):
        """Requests (without received_at), log and offset of UPDATES on a
        bridge with this [auto_topics] (None: no table) and its own ledger."""
        config = {k: v for k, v in self.config.items() if k != 'auto_topics'}
        if auto is not None:
            config['auto_topics'] = auto
        store = b.Store(Path(self.tmp.name) / store_name, 10)
        test_topic_inbound.record_topics(store)
        self.bridge = self.make_bridge(config, store)
        del self.calls[:]
        self.answers[f'123:topic:{WEB}'] = 'unbound'
        log = self.drain(*(make(self) for _, make in self.UPDATES))
        requests = [(url, {k: v for k, v in body['message'].items() if k != 'received_at'})
                    for url, body in self.calls]
        return requests, log, store.offset(), store.auto_topic_rows()

    def test_general_settings_topics_and_leftover_rows_relay_identically_with_auto_on(self):
        requests, log, offset, rows = self.run_with(None, 'none.sqlite3')
        self.assertEqual(len(requests), 7)
        self.assertEqual(offset, 18)
        self.assertIn(f'topic thread {WEB} not bound (msg 12)', log)
        self.assertEqual(rows, [])
        for auto in (AUTO_OFF, AUTO_ON):
            with self.subTest(auto=auto):
                self.assertEqual(self.run_with(auto, f'{auto["enabled"]}.sqlite3'), (requests, log, offset, rows))

    def test_auto_off_leaves_rows_unserved_and_binds_nothing(self):
        self.store.add_auto_topic(NEW, 'mayor')
        self.store.set_auto_topic_name(NEW, 'Plan', False)
        rows = self.store.auto_topic_rows()
        for uid, auto in ((10, AUTO_OFF), (20, None)):
            with self.subTest(auto=auto):
                config = {k: v for k, v in self.config.items() if k != 'auto_topics'}
                if auto is not None:
                    config['auto_topics'] = auto
                self.bridge = self.make_bridge(config)
                del self.calls[:]
                log = self.drain(service(uid, OTHER, 'forum_topic_created', name='Other'),
                                 service(uid + 1, NEW, 'forum_topic_edited', name='Renamed'),
                                 in_topic(self.update(uid + 2), NEW, reply_to_message=dict(
                                     message_id=NEW, forum_topic_created=dict(name='x'))),
                                 in_topic(self.update(uid + 3), OTHER))
                self.assertEqual(self.gc_calls(), [('inbound', '123'), ('inbound', '123')])
                self.assertEqual([text for *_, text in self.relays()], [
                    f'[topic: thread {NEW} | not configured: reply lands in the main chat]\nhello',
                    f'[topic: thread {OTHER} | not configured: reply lands in the main chat]\nhello'])
                self.assertEqual(log, f'topic thread {NEW} not configured (msg {uid + 2}); relayed to the main '
                                      f'conversation\ntopic thread {OTHER} not configured (msg {uid + 3}); relayed '
                                      'to the main conversation\n')
                self.assertEqual(self.store.offset(), uid + 4)
                self.assertEqual(self.store.auto_topic_rows(), rows)


class AutoTopicVoiceTests(AutoTopicInboundCase):
    # TopicVoiceTests' helpers (imported by module, so discovery runs that
    # class once, in test_topic_inbound).
    voice = test_topic_inbound.TopicVoiceTests.voice
    pending = test_topic_inbound.TopicVoiceTests.pending
    restarted = test_topic_inbound.TopicVoiceTests.restarted

    def setUp(self):
        super().setUp()
        self.bridge.state_dir = Path(self.tmp.name)
        script = Path(self.tmp.name) / 'fake_transcriber.py'
        script.write_text('import json\nprint(json.dumps(dict(text="check the rack")))\n')
        self.bridge.transcribe = dict(python=sys.executable, script=str(script), model='base', timeout=20, env={})
        self.enterContext(patch.object(b.shutil, 'which', return_value='/usr/bin/ffmpeg'))

    def test_a_voice_note_in_an_auto_topic_gets_its_transcript_on_the_topics_conversation(self):
        with patch.object(self.bridge, '_enqueue_transcription'):
            self.drain(service(69, NEW, 'forum_topic_created', name='Rack'), self.voice(70, NEW))
        self.assertEqual(json.loads(self.pending(70).read_text())['thread_id'], NEW)
        self.assertEqual(self.restarted(), 1)
        (note_ref, note_id, _, note), (ref, pid, _, transcript) = self.relays()
        self.assertEqual((note_ref, note_id, ref, pid),
                         (self.topic_ref(NEW), '70', self.topic_ref(NEW), '70-transcript'))
        self.assertTrue(note.startswith('[topic: Rack]\n[attachment saved: '))
        self.assertTrue(transcript.startswith('[topic: Rack]\n[voice 0:07 transcript (msg 70): check the rack]'))
        self.assertEqual(len(self.binds()), 1)


class AutoTopicAskTests(AutoTopicInboundCase):
    def transport(self, url, body, *args, **kwargs):
        if url.endswith(('/answerCallbackQuery', '/editMessageText')):
            self.calls.append((url, body))
            if url.endswith('/answerCallbackQuery'):
                return 200, dict(ok=True, result=True)
            return 200, dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
        return super().transport(url, body, *args, **kwargs)

    def test_a_tap_on_an_ask_in_an_auto_topic_relays_on_its_conversation(self):
        ask_id = b.Bridge._ask_id_for_key('ask-1')
        options = [dict(text='Yes', callback_data=f'ask:{ask_id}:yes')]
        self.drain(service(40, NEW, 'forum_topic_created', name='Plan'))
        self.store.record_ask(ask_id, 'Pick one?', 'plain', options, conversation_id=f'123:topic:{NEW}')
        self.store.set_ask_message([ask_id], 77)
        press = in_topic(dict(update_id=41, callback_query=dict(
            id='query-41', data=f'ask:{ask_id}:yes', chat_instance='x', **{'from': dict(id=123, is_bot=False)},
            message=dict(message_id=77, chat=dict(id=123, type='private')))), NEW)
        self.drain(press)
        self.assertEqual(self.relays(), [(self.topic_ref(NEW), f'ask-{ask_id}', None, '[topic: Plan]\n'
                                          '[ask answer | choice: Yes | message: 77]\nquestion: Pick one?')])
        self.assertTrue(self.store.get_ask(ask_id)['relayed'])
        self.assertEqual(self.store.offset(), 42)


class AutoTopicCommandTests(CommandCase):
    def setUp(self):
        super().setUp()
        self.telegram.answers['bind'] = lambda body: (200, dict(
            RECORD_FIELDS, ID='binding-1', AgentName=body['agent_name'], Conversation=body['conversation']))
        self.bridge = self.make_bridge(auto_topics=AUTO_ON)

    def sent(self):
        return ('sendMessage', dict(chat_id=123, text='ok', link_preview_options={'is_disabled': True},
                                    message_thread_id=NEW))

    def test_a_command_in_an_auto_topic_sends_into_its_thread(self):
        self.store.add_auto_topic(NEW, 'mayor')
        self.store.set_auto_topic_bound(NEW, True)
        self.drain(in_topic(self.message('/deploy', 40), NEW))
        self.assertEqual(self.telegram.calls, [self.sent()])

    def test_a_first_command_in_a_new_thread_records_and_binds_its_topic_before_it_runs(self):
        log = self.drain(in_topic(self.message('/deploy', 40), NEW))
        bind = dict(conversation=self.bridge.topic_conversation(NEW), agent_name='mayor')
        self.assertEqual(self.telegram.calls, [('bind', bind), self.sent()])
        row, = self.store.auto_topic_rows()
        self.assertEqual((row['thread_id'], row['name'], row['agent']), (NEW, None, 'mayor'))
        self.assertIsNotNone(row['bound_at'])
        self.assertEqual([topic['thread_id'] for topic in self.bridge.served_auto_topics()], [NEW])
        self.assertNotIn('not bound', log)
        self.drain(in_topic(self.message('/deploy', 41), NEW))
        self.assertEqual(self.telegram.methods(), ['bind', 'sendMessage', 'sendMessage'])

    def test_a_command_still_runs_when_its_bind_fails_and_the_next_one_binds_again(self):
        self.telegram.answers['bind'] = (409, dict(detail='refused'))
        log = self.drain(in_topic(self.message('/deploy', 40), NEW), in_topic(self.message('/deploy', 41), NEW))
        self.assertEqual(self.telegram.methods(), ['bind', 'sendMessage', 'bind', 'sendMessage'])
        self.assertIsNone(self.store.auto_topic_rows()[0]['bound_at'])
        self.assertEqual([line for line in log.splitlines() if 'not bound' in line],
                         [f'auto topic thread {NEW} not bound (gc_conflict)'] * 2)
        self.assertEqual(self.store.offset(), 42)

    def test_a_command_in_a_thread_with_auto_off_records_and_binds_nothing(self):
        for uid, auto in ((40, None), (41, AUTO_OFF)):
            with self.subTest(auto=auto):
                self.telegram.calls.clear()
                self.bridge = self.make_bridge() if auto is None else self.make_bridge(auto_topics=auto)
                self.drain(in_topic(self.message('/deploy', uid), NEW))
                self.assertEqual(self.telegram.calls, [self.sent()])
                self.assertEqual(self.store.auto_topic_rows(), [])


class AutoTopicStartTests(AutoTopicInboundCase):
    def start(self, get_me, auto=AUTO_ON):
        """Run the daemon through its real probe and one empty poll; its stderr."""
        config = {k: v for k, v in self.config.items() if k != 'auto_topics'}
        if auto is not None:
            config['auto_topics'] = auto
        self.bridge = self.make_bridge(config)
        self.bridge.state_dir = Path(self.tmp.name)
        telegram = []

        def transport(url, body, *args, **kwargs):
            if '/bot' in url:
                telegram.append(url.rsplit('/', 1)[1])
            if url.endswith('/getMe'):
                return get_me
            if url.endswith('/getUpdates'):
                self.bridge.stop.set()
                return 200, dict(ok=True, result=[])
            if url.endswith('/extmsg/adapters'):
                return 200, {}
            return self.transport(url, body, *args, **kwargs)
        self.bridge.request = transport
        with patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO) as err:
            testports.run_bridge(self.bridge)
        self.assertEqual(telegram, ['getMe', 'getWebhookInfo', 'getUpdates'])  # no Telegram call of its own
        return err.getvalue().splitlines()

    def test_auto_on_without_users_creating_topics_logs_once_at_start(self):
        for get_me in (GET_ME, MODE_OFF, (200, dict(ok=True, result=dict(id=999, is_bot=True)))):
            with self.subTest(get_me=get_me[1]['result']):
                self.assertEqual(self.start(get_me).count(b.Bridge.AUTO_TOPICS_USERS_OFF), 1)
        self.assertEqual(b.Bridge.AUTO_TOPICS_USERS_OFF,
                         'auto topics on, but users may not create topics (BotFather setting)')

    def test_no_line_with_users_creating_topics_or_auto_off(self):
        for get_me, auto in ((USERS_ALLOWED, AUTO_ON), (GET_ME, AUTO_OFF), (GET_ME, None), (MODE_OFF, None)):
            with self.subTest(get_me=get_me[1]['result'], auto=auto):
                self.assertNotIn(b.Bridge.AUTO_TOPICS_USERS_OFF, self.start(get_me, auto))


if __name__ == '__main__':
    unittest.main()
