"""Reactions and typing in topics of the paired private chat (OPERATIONS.md,
"Reactions" and "Typing"): the accept point records each message under the
conversation GC accepted it on, `react --conversation-id/--topic` targets
that conversation's last accepted message, and typing shows in the thread
the user wrote in. Fake Telegram and GC, an injected clock; temporary
directories; no network."""
from contextlib import closing
import io
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
from test_topic_inbound import TopicInboundCase, in_topic  # noqa: E402

START = 1000.0
OPS, WEB, LAB = 7, 9, 11  # ops and web are bound; lab is recorded, but GC routes it nowhere
OPS_ID, WEB_ID, LAB_ID, CHAT_ID = '123:topic:7', '123:topic:9', '123:topic:11', '123'
GENERAL = dict(chat_id=123, action='typing')


def typing_in(thread_id):
    return dict(chat_id=123, action='typing', message_thread_id=thread_id)


class TopicAckCase(TopicInboundCase):
    """The topic inbound fixture (ops and web bound, the paired chat
    accepted) plus lab recorded and unbound, with the typing clock of the
    typing tests: the worker runs in the test's thread, `wait` moves the
    clock on."""

    def setUp(self):
        super().setUp()
        self.store.record_topic('lab', LAB, 'created')
        self.answers[LAB_ID] = 'unbound'
        self.now = START
        self.actions = []
        self.hooks = {}
        self.ticks = 0
        self.bridge = self.make_bridge(self.config_with(300))

    def config_with(self, seconds, **config):
        if seconds is not None:
            config['typing'] = dict(seconds=seconds)
        return dict(self.config, **config)

    def make_bridge(self, config, store=None):
        return b.Bridge(config, store or self.store, 'SECRET', 'CAPABILITY', request=self.transport,
                        request_multipart=self.multipart_transport, request_download=self.download_transport,
                        sleep=lambda seconds: None, monotonic=lambda: self.now, typing_wait=self.wait)

    def transport(self, url, body, *args, **kwargs):
        if url.endswith('/sendChatAction'):
            self.actions.append((self.now, body))
            return 200, dict(ok=True, result=True)
        if url.endswith('/setMessageReaction'):
            self.calls.append((url, body))
            return 200, dict(ok=True, result=True)
        return super().transport(url, body, *args, **kwargs)

    def wait(self, timeout):
        self.assertEqual(timeout, b.TYPING_TICK)
        self.now += timeout
        hook = self.hooks.pop(self.now, None)
        if hook is not None:
            hook()
        self.ticks -= 1
        if self.ticks <= 0:
            self.bridge.stop.set()

    def work(self, ticks):
        """Run the worker in this thread for `ticks` ticks, then stop it."""
        self.ticks = ticks
        self.bridge._typing_loop()

    def actions_at(self):
        return [(moment - START, body) for moment, body in self.actions]

    def react(self, *argv):
        parser = b._build_parser()
        args = parser.parse_args(['react', *argv])
        b._validate_attachment_flags(parser, args)
        return b._react(self.bridge, args)

    def reactions(self):
        return [body for url, body in self.calls if url.endswith('/setMessageReaction')]


class AcceptedPerConversationTests(TopicAckCase):
    def test_messages_in_two_topics_and_general_each_record_their_conversation(self):
        self.drain(in_topic(self.update(10), OPS), in_topic(self.update(11), WEB), self.update(12))
        self.assertEqual(self.store.offset(), 13)
        self.assertEqual(self.store.last_accepted_in(OPS_ID), 10)
        self.assertEqual(self.store.last_accepted_in(WEB_ID), 11)
        self.assertEqual(self.store.last_accepted_in(CHAT_ID), 12)
        self.assertEqual(self.store.last_accepted(), 12)  # the newest in any conversation, as before

    def test_a_replayed_older_update_never_lowers_a_conversations_message_id(self):
        self.drain(in_topic(self.update(10), OPS), in_topic(self.update(11), OPS), in_topic(self.update(12), WEB))
        self.bridge.inbound(in_topic(self.update(10), OPS))
        self.assertEqual(self.store.last_accepted_in(OPS_ID), 11)
        self.assertEqual(self.store.last_accepted(), 12)

    def test_an_unbound_or_unconfigured_topic_records_the_paired_chats_conversation(self):
        log = self.drain(in_topic(self.update(10), LAB), in_topic(self.update(11), 13))
        self.assertEqual([relay[0]['conversation_id'] for relay in self.relays()], [LAB_ID, CHAT_ID, CHAT_ID])
        self.assertEqual(self.store.last_accepted_in(CHAT_ID), 11)
        self.assertIsNone(self.store.last_accepted_in(LAB_ID))
        self.assertIn('not bound', log)

    def test_a_ledger_without_the_table_opens_unchanged_and_gains_it(self):
        self.store.record_accepted(41, '2026-10-27T07:00:00+00:00')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TABLE accepted_conversations')  # the ledger as the base wrote it
        with closing(sqlite3.connect(self.path)) as db:
            before = db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
        store = b.Store(self.path, 10)
        with closing(sqlite3.connect(self.path)) as db:
            after = db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
        self.assertEqual([row for row in after if row[0] not in ('accepted_conversations',)
                          and not row[0].startswith('sqlite_autoindex_accepted_conversations')], before)
        self.assertIn(('accepted_conversations', 'CREATE TABLE accepted_conversations (conversation_id TEXT PRIMARY KEY, '
                       'message_id INTEGER NOT NULL, accepted_at TEXT NOT NULL)'), after)
        self.assertEqual(store.last_accepted(), 41)
        self.assertIsNone(store.last_accepted_in(CHAT_ID))


class ReactInConversationTests(TopicAckCase):
    def setUp(self):
        super().setUp()
        self.drain(in_topic(self.update(10), OPS), self.update(11), in_topic(self.update(12), WEB))

    def done(self, mid):
        return dict(chat_id=123, message_id=mid, reaction=[dict(type='emoji', emoji='\U0001F44C')])

    def test_conversation_id_or_topic_target_that_conversations_last_message(self):
        self.assertEqual(self.react('--reaction', 'done', '--conversation-id', OPS_ID), 10)
        self.assertEqual(self.react('--reaction', 'done', '--topic', 'ops'), 10)
        self.assertEqual(self.react('--reaction', 'done', '--conversation-id', CHAT_ID), 11)
        self.assertEqual(self.reactions(), [self.done(10), self.done(10), self.done(11)])

    def test_without_a_flag_it_targets_the_newest_message_of_the_chat(self):
        self.assertEqual(self.react('--reaction', 'done'), 12)
        self.assertEqual(self.react('--reaction', 'done', '--message-id', '5'), 5)
        self.assertEqual(self.reactions(), [self.done(12), self.done(5)])

    def test_a_conversation_with_nothing_accepted_refuses_before_telegram(self):
        self.store.record_topic('more', 15, 'created')
        self.bridge = self.make_bridge(dict(self.config_with(300), topics=self.config['topics'] + (
            dict(name='more', agent='mayor'),)))
        for argv in (('--conversation-id', '123:topic:15'), ('--topic', 'more'), ('--topic', 'lab')):
            with self.subTest(argv=argv), self.assertRaisesRegex(
                    b.TransportError, '^no accepted message to react to in that conversation$'):
                self.react('--reaction', 'done', *argv)
        self.assertEqual(self.reactions(), [])

    def test_an_id_or_name_no_configured_topic_has_refuses_as_send_does(self):
        cases = [(('--conversation-id', '123:topic:99'),
                  'conversation ID is not the paired private chat or a configured topic'),
                 (('--conversation-id', '456'), 'conversation ID is not the paired private chat or a configured topic'),
                 (('--conversation-id', ''), 'conversation ID is not the paired private chat or a configured topic'),
                 (('--topic', 'nope'), 'topic nope is not configured or not created (run gc telegram topics)')]
        for argv, message in cases:
            with self.subTest(argv=argv), self.assertRaises(b.TransportError) as raised:
                self.react('--reaction', 'done', *argv)
            self.assertEqual(str(raised.exception), message)
        self.assertEqual(self.reactions(), [])

    def test_two_target_flags_refuse_through_the_parser(self):
        for argv in (('--message-id', '5', '--conversation-id', OPS_ID), ('--message-id', '5', '--topic', 'ops'),
                     ('--conversation-id', OPS_ID, '--topic', 'ops'), ('--message-id', '5', '--conversation-id', '')):
            with self.subTest(argv=argv), patch('sys.stderr', new_callable=io.StringIO) as err, \
                    self.assertRaises(SystemExit) as raised:
                self.react('--reaction', 'done', *argv)
            self.assertEqual(raised.exception.code, 2)
            self.assertIn('error:', err.getvalue())
        self.assertEqual(self.reactions(), [])


class TypingInTopicsTests(TopicAckCase):
    def test_typing_shows_in_the_topic_written_in_and_general_keeps_its_body(self):
        self.drain(in_topic(self.update(10), OPS))
        self.work(1)
        self.bridge.stop.clear()
        self.drain(self.update(11))
        self.work(1)
        self.assertEqual(self.actions_at(), [(0, typing_in(OPS)), (1, GENERAL)])

    def test_an_unbound_topics_message_still_shows_typing_in_its_own_thread(self):
        self.drain(in_topic(self.update(10), LAB))
        self.assertEqual(self.store.last_accepted_in(CHAT_ID), 10)
        self.work(1)
        self.assertEqual(self.actions_at(), [(0, typing_in(LAB))])

    def test_windows_in_two_topics_run_at_once_and_one_send_closes_both(self):
        self.drain(in_topic(self.update(10), OPS))
        self.hooks[START + 2] = lambda: self.drain(in_topic(self.update(11), WEB))
        self.hooks[START + 7] = lambda: self.assertTrue(self.bridge.publish(dict(
            self.publish(key='reply-1'), conversation=self.topic_ref(OPS)))['delivered'])
        self.work(20)
        self.assertEqual(self.actions_at(), [(0, typing_in(OPS)), (2, typing_in(WEB)), (4, typing_in(OPS)),
                                             (6, typing_in(WEB))])
        self.assertEqual(self.bridge.typing_windows, {})

    def test_each_window_extends_and_ends_on_its_own(self):
        self.bridge = self.make_bridge(self.config_with(10))
        self.drain(in_topic(self.update(10), OPS))
        self.hooks[START + 6] = lambda: self.drain(in_topic(self.update(11), WEB))
        self.work(25)
        ops = [moment for moment, body in self.actions_at() if body == typing_in(OPS)]
        web = [moment for moment, body in self.actions_at() if body == typing_in(WEB)]
        self.assertEqual((ops, web), ([0, 4, 8], [6, 10, 14]))
        self.assertEqual(self.bridge.typing_windows, {})

    def test_an_accept_reaction_row_closes_no_window(self):
        self.bridge = self.make_bridge(self.config_with(300, reactions=dict(b.REACTIONS_DEFAULTS, on_accept=True)))
        self.drain(in_topic(self.update(10), OPS), self.update(11))
        self.bridge.reaction_queue.join()
        self.hooks[START + 3] = lambda: (self.drain(in_topic(self.update(12), WEB)), self.bridge.reaction_queue.join())
        self.work(6)
        with closing(sqlite3.connect(self.path)) as db:
            rows = [row[0] for row in db.execute('SELECT key FROM deliveries ORDER BY rowid')]
        self.assertEqual(rows, ['reaction:10:accept', 'reaction:11:accept', 'reaction:12:accept'])
        self.assertEqual(self.actions_at(), [(0, typing_in(OPS)), (0, GENERAL), (3, typing_in(WEB)),
                                             (4, typing_in(OPS)), (4, GENERAL)])

    def test_stop_ends_every_window(self):
        self.drain(in_topic(self.update(10), OPS), in_topic(self.update(11), WEB), self.update(12))
        self.assertEqual(set(self.bridge.typing_windows), {OPS, WEB, None})
        self.work(2)
        self.assertEqual(self.bridge.typing_windows, {})
        self.assertEqual(len(self.actions), 3)

    def test_seconds_0_sends_no_chat_action_in_any_thread(self):
        self.bridge = self.make_bridge(self.config_with(0))
        self.drain(in_topic(self.update(10), OPS), in_topic(self.update(11), LAB), self.update(12))
        self.assertEqual(self.bridge.typing_windows, {})
        self.work(10)
        self.assertEqual(self.actions, [])
        self.assertEqual(self.store.offset(), 13)

    def test_a_failing_action_logs_once_per_window_with_its_thread(self):
        def transport(url, body, *args, **kwargs):
            if url.endswith('/sendChatAction'):
                self.actions.append((self.now, body))
                raise b.RequestNotSent('network request not sent', 7, 'connect')
            return TopicAckCase.transport(self, url, body, *args, **kwargs)
        self.bridge = b.Bridge(self.config_with(10), self.store, 'SECRET', 'CAPABILITY', request=transport,
                               sleep=lambda seconds: None, monotonic=lambda: self.now, typing_wait=self.wait)
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            self.bridge.drain([in_topic(self.update(10), OPS), self.update(11)])
            self.work(15)
        self.assertEqual(len(self.actions), 6)
        self.assertEqual(log.getvalue().count(f'typing not shown (thread {OPS}): transport_unavailable'), 1)
        self.assertEqual(log.getvalue().count('typing not shown: transport_unavailable'), 1)
        self.assertNotIn('hello', log.getvalue())

    def test_a_look_that_fails_unexpectedly_ends_its_window_alone(self):
        send_typing = self.bridge._send_typing

        def failing_in_ops(window):
            if window['thread'] == OPS:
                raise RuntimeError('unexpected')
            send_typing(window)
        self.bridge._send_typing = failing_in_ops
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            self.bridge.drain([in_topic(self.update(10), OPS), in_topic(self.update(11), WEB)])
            self.hooks[START + 2] = lambda: self.assertEqual(set(self.bridge.typing_windows), {WEB})
            self.work(9)
        self.assertEqual(self.actions_at(), [(0, typing_in(WEB)), (4, typing_in(WEB)), (8, typing_in(WEB))])
        self.assertEqual(log.getvalue().count('typing not shown'), 1)
        self.assertIn(f'typing not shown (thread {OPS}): typing_failed', log.getvalue())

    def test_an_extended_window_keeps_its_one_failure_log_across_an_action_in_flight(self):
        def transport(url, body, *args, **kwargs):
            if url.endswith('/sendChatAction'):
                self.actions.append((self.now, body))
                if len(self.actions) == 1:
                    self.bridge.drain([in_topic(self.update(11), OPS)])
                raise b.RequestNotSent('network request not sent', 7, 'connect')
            return TopicAckCase.transport(self, url, body, *args, **kwargs)
        self.bridge = b.Bridge(self.config_with(10), self.store, 'SECRET', 'CAPABILITY', request=transport,
                               sleep=lambda seconds: None, monotonic=lambda: self.now, typing_wait=self.wait)
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            self.bridge.drain([in_topic(self.update(10), OPS)])
            self.work(15)
        self.assertGreater(len(self.actions), 2)
        self.assertEqual(log.getvalue().count(f'typing not shown (thread {OPS}): transport_unavailable'), 1)


    def test_a_message_accepted_while_the_worker_ends_its_window_keeps_typing(self):
        self.bridge = self.make_bridge(self.config_with(10))
        self.drain(in_topic(self.update(10), OPS))
        end_typing = self.bridge._end_typing

        def accept_before_the_end(*args):
            # the worker has seen the window's end; update 11, accepted just
            # before it, extends the window before the worker deletes it
            self.now = START + 9.5
            self.drain(in_topic(self.update(11), OPS))
            self.now = START + 10
            self.bridge._end_typing = end_typing
            end_typing(*args)
        self.hooks[START + 10] = lambda: setattr(self.bridge, '_end_typing', accept_before_the_end)
        self.work(25)
        self.assertEqual([moment for moment, body in self.actions_at()], [0, 4, 8, 11, 15, 19])
        self.assertEqual(self.bridge.typing_windows, {})

if __name__ == '__main__':
    unittest.main()
