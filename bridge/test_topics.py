"""Topics of the paired private chat (OPERATIONS.md, "Topics"): the [[topic]]
settings, the ledger's topics tables, the lookups and the `topics` command,
with the fake Telegram, temporary directories and no network."""
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
from test_bridge import MENU, BridgeTestCase, run_main, write_fake_curl, write_settings  # noqa: E402

PACK = Path(__file__).resolve().parents[1]
GET_ME = 200, {'ok': True, 'result': {'id': 999, 'is_bot': True, 'has_topics_enabled': True,
                                      'allows_users_to_create_topics': False}}
MODE_OFF = 200, {'ok': True, 'result': {'id': 999, 'is_bot': True, 'has_topics_enabled': False,
                                        'allows_users_to_create_topics': False}}
USERS_ALLOWED = 200, {'ok': True, 'result': {'id': 999, 'is_bot': True, 'has_topics_enabled': True,
                                             'allows_users_to_create_topics': True}}
REJECTED = 400, {'ok': False, 'error_code': 400, 'description': 'Bad Request: chat not found'}
UNCONFIRMED = 502, {'ok': False, 'error_code': 502}
AMBIGUOUS = 'ambiguous_delivery_requires_manual_reconciliation'


def not_sent():
    return b.RequestNotSent('network request not sent', 7, 'connect')


def bind(thread_id, agent='mayor'):
    return (f'bind: gc extmsg bind --provider telegram --account-id test_bot --conversation-id 123:topic:{thread_id} '
            f'--parent-conversation-id 123 --kind thread --agent {agent} --json')


def topic_tables(*entries):
    """[[topic]] TOML for (name, agent or None) pairs."""
    return ''.join(f'\n[[topic]]\nname = {json.dumps(name)}\n' + (f'agent = "{agent}"\n' if agent else '')
                   for name, agent in entries)


class TopicSettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def load(self, tables, **replace):
        path = write_settings(self.root, **replace)
        path.write_text(path.read_text() + tables)
        return b.load_settings(path)

    def refuses(self, tables, message):
        with self.assertRaises(b.SettingsError) as raised:
            self.load(tables)
        self.assertEqual(str(raised.exception), message)

    def test_topics_load_in_file_order_with_the_settings_agent_as_default(self):
        settings = self.load(topic_tables(('ops', None), ('web', 'helper'), ('x' * 128, None)))
        self.assertEqual(settings.config['topics'], (dict(name='ops', agent='mayor'), dict(name='web', agent='helper'),
                                                     dict(name='x' * 128, agent='mayor')))
        settings = self.load(topic_tables(('ops', None), ('Ops log', 'mayor')), agent='"helper"')
        self.assertEqual(settings.config['topics'], (dict(name='ops', agent='helper'),
                                                     dict(name='Ops log', agent='mayor')))
        self.assertEqual(b.Bridge(settings.config, None, 'SECRET', None).topic_settings, settings.config['topics'])

    def test_a_file_without_topics_loads_to_the_config_it_loaded_to_before(self):
        config = self.load('').config
        expected = dict(city='test-city', account_id='test_bot', gc_base_url='http://127.0.0.1:8372',
                        user_display_name='Pat', timezone='Asia/Tokyo', menu=MENU, bot_id=999, user_id=123,
                        chat_id=123, unit='test-city-telegram.service',
                        protected_paths=config['protected_paths'])
        self.assertEqual(config, expected)
        self.assertEqual(len(config['protected_paths']), 3)
        with_topics = self.load(topic_tables(('ops', None))).config
        self.assertEqual({k: v for k, v in with_topics.items() if k != 'topics'}, config)
        path = write_settings(self.root)
        path.write_text('topic = []\n' + path.read_text())
        self.assertEqual(b.load_settings(path).config, config)

    def test_each_bad_entry_refuses_with_a_line_naming_it(self):
        cases = [
            (topic_tables(('', None)), 'topic entry 1: name must be a string of 1-128 characters'),
            (topic_tables(('ops', None), ('x' * 129, None)), 'topic entry 2: name must be a string of 1-128 characters'),
            ('\n[[topic]]\nagent = "mayor"\n', 'topic entry 1: name must be a string of 1-128 characters'),
            ('\n[[topic]]\nname = 7\n', 'topic entry 1: name must be a string of 1-128 characters'),
            (topic_tables((' ops', None)), 'topic entry 1: name has leading or trailing whitespace'),
            (topic_tables(('ops\t', None)), 'topic entry 1: name has leading or trailing whitespace'),
            (topic_tables(('o\x07ps', None)), 'topic entry 1: name has a control or line separator character'),
            (topic_tables(('o\nps', None)), 'topic entry 1: name has a control or line separator character'),
            (topic_tables(('o\x85ps', None)), 'topic entry 1: name has a control or line separator character'),
            (topic_tables(('o\u2028ps', None)), 'topic entry 1: name has a control or line separator character'),
            (topic_tables(('o\u2029ps', None)), 'topic entry 1: name has a control or line separator character'),
            (topic_tables(('[ops', None)), 'topic entry 1: name has [, ] or |'),
            (topic_tables(('ops]', None)), 'topic entry 1: name has [, ] or |'),
            (topic_tables(('ops|web', None)), 'topic entry 1: name has [, ] or |'),
            (topic_tables(('Ops', None), ('web', None), ('OPS', None)),
             'topic entry 3: name repeats topic entry 1 (names differ in more than case)'),
            (topic_tables(('Straße', None), ('STRASSE', None)),
             'topic entry 2: name repeats topic entry 1 (names differ in more than case)'),
            (topic_tables(('ops', 'not an agent')), 'topic entry 1: agent is missing or malformed'),
            ('\n[[topic]]\nname = "ops"\nagent = 5\n', 'topic entry 1: agent is missing or malformed'),
            ('\n[[topic]]\nname = "ops"\nthread_id = 5\n', 'topic entry 1: unknown key thread_id'),
            (topic_tables(*((f't{n}', None) for n in range(51))), 'topic entry 51: more than 50 [[topic]] tables'),
            ('topic = "ops"\n', 'topic must be a list of [[topic]] tables'),
            ('topic = { name = "ops" }\n', 'topic must be a list of [[topic]] tables'),
            ('topic = ["ops"]\n', 'topic entry 1: not a [[topic]] table'),
        ]
        for tables, message in cases:
            with self.subTest(message=message, tables=tables[:40]):
                if tables.startswith('topic ='):
                    path = write_settings(self.root)
                    path.write_text(tables + path.read_text())
                    with self.assertRaises(b.SettingsError) as raised:
                        b.load_settings(path)
                    self.assertEqual(str(raised.exception), 'settings: ' + message)
                else:
                    self.refuses(tables, 'settings: ' + message)
        self.assertEqual(len(self.load(topic_tables(*((f't{n}', None) for n in range(50)))).config['topics']), 50)

    def test_a_malformed_settings_agent_is_named_before_any_topic(self):
        with self.assertRaisesRegex(b.SettingsError, '^settings: agent is missing or malformed$'):
            self.load(topic_tables(('ops', None)), agent='"not an agent"')


class TopicLedgerTests(unittest.TestCase):
    # The ledger as the base wrote it, before the topics tables existed.
    BASE_SCHEMA = (
        'CREATE TABLE cursor (id INTEGER PRIMARY KEY, offset INTEGER NOT NULL)',
        'CREATE TABLE deliveries (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, receipt TEXT NOT NULL)',
        'CREATE TABLE asks (ask_id TEXT PRIMARY KEY, question TEXT NOT NULL, format TEXT NOT NULL, '
        'options TEXT NOT NULL, message_id INTEGER, answered INTEGER NOT NULL DEFAULT 0, answer TEXT, '
        "update_id INTEGER, relayed INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'text', "
        'decided INTEGER NOT NULL DEFAULT 0)',
        'CREATE TABLE accepted (id INTEGER PRIMARY KEY CHECK (id = 1), message_id INTEGER NOT NULL, '
        'accepted_at TEXT NOT NULL)',
        'CREATE TABLE pins (slot TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 1, message_id INTEGER, '
        'revision INTEGER NOT NULL DEFAULT 0, fingerprint TEXT)',
    )

    def setUp(self):
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'telegram-bridge.sqlite3'

    def dump(self):
        with closing(sqlite3.connect(self.path)) as db:
            tables = dict(db.execute("SELECT name, sql FROM sqlite_master WHERE type='table'"))
            rows = {name: db.execute(f'SELECT * FROM {name}').fetchall() for name in tables}
        return tables, rows

    def test_a_fresh_ledger_gets_both_tables(self):
        b.Store(self.path, 10)
        with closing(sqlite3.connect(self.path)) as db:
            columns = {table: [row[1:4] + (row[5],) for row in db.execute(f'PRAGMA table_info({table})')]
                       for table in ('topics', 'topic_attempts')}
            unique = [db.execute(f'PRAGMA index_info({index[1]})').fetchall()
                      for index in db.execute('PRAGMA index_list(topics)') if index[2]]
        self.assertEqual(columns['topics'], [('name', 'TEXT', 0, 1), ('thread_id', 'INTEGER', 1, 0),
                                             ('source', 'TEXT', 1, 0), ('recorded_at', 'TEXT', 1, 0)])
        self.assertEqual(columns['topic_attempts'], [('name', 'TEXT', 0, 1), ('attempt', 'INTEGER', 1, 0)])
        self.assertEqual(sorted(info[0][2] for info in unique), ['name', 'thread_id'])

    def test_an_existing_base_ledger_opens_unchanged(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            for sql in self.BASE_SCHEMA:
                db.execute(sql)
            db.execute('INSERT INTO cursor VALUES (1, 42)')
            db.execute("INSERT INTO deliveries VALUES ('k1', 'f1', '{\"delivered\": true}')")
            db.execute("INSERT INTO asks(ask_id, question, format, options) VALUES ('a1', 'Q?', 'plain', '[]')")
            db.execute("INSERT INTO accepted VALUES (1, 77, '2026-10-27T00:00:00+00:00')")
            db.execute("INSERT INTO pins(slot, message_id) VALUES ('waiting', 501)")
        tables, rows = self.dump()
        store = b.Store(self.path, 10)
        after_tables, after_rows = self.dump()
        # The asks table gains only its last column, conversation_id, which
        # every recorded ask reads as NULL (the paired chat).
        tables['asks'] = tables['asks'][:-1] + ', conversation_id TEXT)'
        rows['asks'] = [row + (None,) for row in rows['asks']]
        added = ('topics', 'topic_attempts', 'accepted_conversations')
        self.assertEqual({k: v for k, v in after_tables.items() if k not in added}, tables)
        self.assertEqual({k: v for k, v in after_rows.items() if k not in added}, rows)
        self.assertEqual([after_rows[k] for k in added], [[], [], []])
        self.assertEqual(store.offset(), 42)
        b.Store(self.path, 10)
        self.assertEqual(self.dump(), (after_tables, after_rows))

    def test_record_refuses_a_recorded_name_or_a_held_thread_id(self):
        store = b.Store(self.path, 10)
        self.assertIsNone(store.record_topic('ops', 501, 'created'))
        self.assertEqual(store.record_topic('ops', 502, 'recorded'), 'topic_already_recorded')
        self.assertEqual(store.record_topic('web', 501, 'recorded'), 'thread_id_already_recorded')
        self.assertIsNone(store.record_topic('web', 502, 'recorded'))
        with self.assertRaises(ValueError):
            store.record_topic('docs', 503, 'guessed')
        rows = store.topic_rows()
        self.assertEqual([(r['name'], r['thread_id'], r['source']) for r in rows],
                         [('ops', 501, 'created'), ('web', 502, 'recorded')])
        self.assertTrue(all(r['recorded_at'].endswith('+00:00') for r in rows))

    def test_an_attempt_starts_at_one_and_rises_by_one(self):
        store = b.Store(self.path, 10)
        self.assertEqual(store.topic_attempt('ops'), 1)
        self.assertEqual((store.raise_topic_attempt('ops'), store.raise_topic_attempt('ops')), (2, 3))
        self.assertEqual((store.topic_attempt('ops'), store.topic_attempt('web')), (3, 1))


class TopicsTests(BridgeTestCase):
    """Bridge.topics and the lookups against a fake Telegram."""

    TOPICS = (dict(name='ops', agent='mayor'), dict(name='web', agent='helper'))

    def setUp(self):
        super().setUp()
        self.config['topics'] = self.TOPICS
        self.answers = {}  # method -> answers used in order before the default
        self.next_id = 500
        self.bridge = self.make_bridge()

    def fresh(self, name, topics):
        """A new ledger and bridge for one subtest."""
        self.path = Path(self.tmp.name) / f'{name}.sqlite3'
        self.store = b.Store(self.path, 10)
        self.bridge = self.make_bridge(topics=topics)
        self.next_id = 500

    def make_bridge(self, store=None, request=None, topics=None):
        config = dict(self.config, topics=self.TOPICS if topics is None else topics)
        return b.Bridge(config, store or self.store, 'SECRET', 'CAPABILITY', request=request or self.topic_transport,
                        sleep=lambda delay: None)

    def topic_transport(self, url, body, *args, **kwargs):
        method = url.rsplit('/', 1)[1]
        self.calls.append((method, body))
        if self.answers.get(method):
            answer = self.answers[method].pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if method == 'getMe':
            return GET_ME
        if method == 'createForumTopic':
            self.next_id += 1
            return 200, {'ok': True, 'result': {'message_thread_id': self.next_id, 'name': body['name'],
                                                'icon_color': 7322096}}
        raise AssertionError('unexpected Telegram method ' + method)

    def methods(self):
        return [method for method, _ in self.calls]

    def ledger(self):
        with closing(sqlite3.connect(self.path)) as db:
            return {key: json.loads(receipt) for key, receipt in db.execute('SELECT key, receipt FROM deliveries')}

    def rows(self):
        return [(row['name'], row['thread_id'], row['source']) for row in self.store.topic_rows()]

    def run_topics(self, **flags):
        self.calls.clear()
        return self.bridge.topics(**flags)

    def test_topics_creates_each_once_in_settings_order_and_prints_bind_lines(self):
        self.assertEqual(self.run_topics(), (['topic ops: thread 501 (created)', bind(501),
                                              'topic web: thread 502 (created)', bind(502, 'helper')], True))
        self.assertEqual(self.calls, [('getMe', {}), ('createForumTopic', {'chat_id': 123, 'name': 'ops'}),
                                      ('createForumTopic', {'chat_id': 123, 'name': 'web'})])
        self.assertEqual(self.rows(), [('ops', 501, 'created'), ('web', 502, 'created')])
        receipt = self.ledger()['topic:1:ops']
        self.assertEqual((receipt['delivered'], receipt['message_thread_id']), (True, 501))
        self.assertEqual(sorted(self.ledger()), ['topic:1:ops', 'topic:1:web'])

    def test_a_rerun_sends_nothing_and_prints_recorded_earlier(self):
        self.run_topics()
        self.assertEqual(self.run_topics(), (['topic ops: thread 501 (recorded earlier)', bind(501),
                                              'topic web: thread 502 (recorded earlier)', bind(502, 'helper')], True))
        self.assertEqual(self.calls, [])

    def test_a_new_settings_topic_alone_is_created(self):
        self.run_topics()
        self.bridge = self.make_bridge(topics=self.TOPICS + (dict(name='docs', agent='mayor'),))
        lines, complete = self.run_topics()
        self.assertTrue(complete)
        self.assertEqual(lines[-2:], ['topic docs: thread 503 (created)', bind(503)])
        self.assertEqual(self.calls, [('getMe', {}), ('createForumTopic', {'chat_id': 123, 'name': 'docs'})])

    def test_topic_mode_off_creates_nothing(self):
        self.answers['getMe'] = [MODE_OFF]
        with self.assertRaisesRegex(b.TransportError, '^topic mode is off for this bot; switch it on with BotFather$'):
            self.run_topics()
        self.assertEqual(self.methods(), ['getMe'])
        self.assertEqual((self.rows(), self.ledger()), ([], {}))

    def test_another_bot_or_a_failed_get_me_creates_nothing(self):
        for answer in ((200, {'ok': True, 'result': {'id': 1000, 'is_bot': True, 'has_topics_enabled': True}}),
                       (401, {'ok': False, 'error_code': 401}), not_sent()):
            with self.subTest(answer=answer):
                self.answers['getMe'] = [answer]
                with self.assertRaises(b.TransportError):
                    self.run_topics()
                self.assertEqual(self.methods(), ['getMe'])
        self.assertEqual((self.rows(), self.ledger()), ([], {}))

    def test_users_allowed_to_create_topics_warns_and_creates(self):
        self.answers['getMe'] = [USERS_ALLOWED]
        lines, complete = self.run_topics()
        self.assertEqual(lines[0], 'WARN users may create and delete topics in this chat (BotFather setting)')
        self.assertEqual(lines[1:3], ['topic ops: thread 501 (created)', bind(501)])
        self.assertTrue(complete)

    def test_a_rejected_create_fails_and_a_rerun_does_not_send(self):
        self.answers['createForumTopic'] = [REJECTED]
        lines, complete = self.run_topics()
        self.assertFalse(complete)
        self.assertEqual(lines[0], 'topic ops: not created (telegram_rejected_not_sent_400); '
                                   'fix the cause, then run gc telegram topics --retry ops')
        self.assertEqual(lines[1:], ['topic web: thread 501 (created)', bind(501, 'helper')])
        self.assertEqual(self.run_topics(), (lines[:1] + ['topic web: thread 501 (recorded earlier)', lines[2]], False))
        self.assertEqual(self.calls, [])

    def test_an_unconfirmed_create_fails_with_the_recovery_and_a_rerun_does_not_send(self):
        recovery = ('topic ops: not created (ambiguous_delivery_requires_manual_reconciliation); check the chat: '
                    'if topic ops is there, write in it, take the thread id from the "[topic: thread <id> | not '
                    'configured" line the agent receives and run gc telegram topics --record ops --thread-id <id>; '
                    'if it is not, run gc telegram topics --retry ops')
        answers = (UNCONFIRMED, b.RequestUnconfirmed('timed out', 28, 'after-send'),
                   (200, {'ok': True, 'result': {'message_thread_id': 501, 'name': 'Ops'}}),
                   (200, {'ok': True, 'result': {'message_thread_id': 0, 'name': 'ops'}}),
                   (200, {'ok': True, 'result': True}))
        for answer in answers:
            with self.subTest(answer=answer):
                self.fresh(f'unconfirmed-{answers.index(answer)}', self.TOPICS[:1])
                self.answers['createForumTopic'] = [answer]
                self.assertEqual(self.run_topics(), ([recovery], False))
                self.assertEqual(self.methods(), ['getMe', 'createForumTopic'])
                self.assertEqual(self.run_topics(), ([recovery], False))
                self.assertEqual(self.calls, [])
                self.assertEqual(self.store.topic_rows(), [])

    def test_a_create_not_sent_is_sent_by_a_plain_rerun_under_the_same_key(self):
        self.answers['createForumTopic'] = [not_sent(), not_sent(), not_sent()]
        lines, complete = self.run_topics()
        self.assertEqual(lines, ['topic ops: not created (transport_unavailable_not_sent); '
                                 'run gc telegram topics again to send it',
                                 'topic web: not created (not attempted: Telegram could not be reached); '
                                 'run gc telegram topics again'])
        self.assertFalse(complete)
        self.assertEqual(self.methods(), ['getMe'] + ['createForumTopic'] * 3)
        self.assertEqual(list(self.ledger()), ['topic:1:ops'])
        self.assertEqual(self.run_topics(), (['topic ops: thread 501 (created)', bind(501),
                                              'topic web: thread 502 (created)', bind(502, 'helper')], True))
        self.assertEqual(sorted(self.ledger()), ['topic:1:ops', 'topic:1:web'])

    def test_retry_after_an_ambiguous_or_rejected_create_sends_under_attempt_two(self):
        for answer in (UNCONFIRMED, REJECTED):
            with self.subTest(answer=answer):
                self.fresh(f'retry-{answer[0]}', self.TOPICS[:1])
                self.answers['createForumTopic'] = [answer]
                self.run_topics()
                self.assertEqual(self.run_topics(retry='ops'), (['topic ops: thread 501 (created)', bind(501)], True))
                self.assertEqual(self.calls, [('getMe', {}), ('createForumTopic', {'chat_id': 123, 'name': 'ops'})])
                self.assertEqual(sorted(self.ledger()), ['topic:1:ops', 'topic:2:ops'])
                self.assertFalse(self.ledger()['topic:1:ops']['delivered'])
                self.assertEqual(self.store.topic_attempt('ops'), 2)

    def test_retry_is_refused_unless_the_create_was_ambiguous_or_rejected(self):
        self.answers['createForumTopic'] = [not_sent()] * 3
        self.run_topics()
        cases = [('ops', '^topics --retry: the create of topic ops was not sent; send again: run topics without '
                         '--retry$'),
                 ('docs', "^topics --retry: no \\[\\[topic\\]\\] named 'docs' in the settings$"),
                 ('web', '^topics --retry: topic web has no create to retry; run topics without --retry$')]
        for name, message in cases:
            with self.subTest(name=name), self.assertRaisesRegex(b.TransportError, message):
                self.run_topics(retry=name)
            self.assertEqual(self.calls, [])
        self.run_topics()
        with self.assertRaisesRegex(b.TransportError, '^topics --retry: topic ops already has thread 501$'):
            self.run_topics(retry='ops')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.topic_attempt('ops'), 1)

    def test_retry_with_topic_mode_off_keeps_the_attempt(self):
        self.answers['createForumTopic'] = [UNCONFIRMED]
        self.run_topics()
        self.answers['getMe'] = [MODE_OFF]
        with self.assertRaisesRegex(b.TransportError, 'topic mode is off'):
            self.run_topics(retry='ops')
        self.assertEqual(self.store.topic_attempt('ops'), 1)

    def test_record_writes_a_row_with_no_telegram_call(self):
        lines, complete = self.run_topics(record='web', thread_id=777)
        self.assertEqual(self.calls, [])
        self.assertEqual(lines, ['topic ops: not created (not attempted); run gc telegram topics to create it',
                                 'topic web: thread 777 (recorded by hand)', bind(777, 'helper')])
        self.assertFalse(complete)
        self.assertEqual(self.rows(), [('web', 777, 'recorded')])
        self.assertEqual(self.ledger(), {})
        self.assertEqual(self.run_topics(), (['topic ops: thread 501 (created)', bind(501),
                                              'topic web: thread 777 (recorded earlier)', bind(777, 'helper')], True))
        self.assertEqual(self.methods(), ['getMe', 'createForumTopic'])

    def test_record_after_an_ambiguous_create_completes_the_topic(self):
        self.answers['createForumTopic'] = [UNCONFIRMED]
        self.run_topics()
        self.assertEqual(self.run_topics(record='ops', thread_id=640)[1], True)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rows(), [('web', 501, 'created'), ('ops', 640, 'recorded')])

    def test_record_refuses_a_used_thread_id_a_recorded_name_and_an_unknown_name(self):
        self.run_topics(record='web', thread_id=777)
        cases = [(dict(record='ops', thread_id=777), '^topics --record: thread 777 is already recorded for topic web$'),
                 (dict(record='web', thread_id=778), '^topics --record: topic web already has thread 777$'),
                 (dict(record='docs', thread_id=778), "^topics --record: no \\[\\[topic\\]\\] named 'docs' in the "
                                                      'settings$')]
        for flags, message in cases:
            with self.subTest(flags=flags), self.assertRaisesRegex(b.TransportError, message):
                self.run_topics(**flags)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rows(), [('web', 777, 'recorded')])

    def test_a_confirmed_create_without_its_row_is_healed_without_a_send(self):
        self.run_topics()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM topics WHERE name='ops'")
        self.assertEqual(self.run_topics(), (['topic ops: thread 501 (created)', bind(501),
                                              'topic web: thread 502 (recorded earlier)', bind(502, 'helper')], True))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rows(), [('web', 502, 'created'), ('ops', 501, 'created')])

    def test_a_row_whose_name_left_the_settings_prints_its_line(self):
        self.run_topics()
        self.bridge = self.make_bridge(topics=self.TOPICS[1:])
        self.assertEqual(self.run_topics(), (['topic web: thread 502 (recorded earlier)', bind(502, 'helper'),
                                              'topic ops: thread 501 (not in settings; its messages reach the '
                                              'agent as not configured)'], True))
        self.bridge = self.make_bridge(topics=())
        self.assertEqual(self.run_topics()[0][0], 'no [[topic]] tables in the settings')
        self.assertEqual(self.calls, [])

    def test_the_topics_lock_is_owner_only_in_the_state_directory(self):
        self.run_topics()
        lock = Path(self.tmp.name) / 'telegram-topics.lock'
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

    def test_two_concurrent_runs_create_each_topic_once(self):
        gate = threading.Lock()

        def slow_transport(url, body, *args, **kwargs):
            if url.endswith('/createForumTopic'):
                time.sleep(0.1)  # the other run must wait on the topics lock meanwhile
            with gate:
                return self.topic_transport(url, body)
        bridges = [self.make_bridge(store=b.Store(self.path, 10), request=slow_transport) for _ in range(2)]
        results, start = [None, None], threading.Barrier(2)

        def run(index):
            start.wait()
            results[index] = bridges[index].topics()
        threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(self.methods(), ['getMe', 'createForumTopic', 'createForumTopic'])
        self.assertEqual(sorted(result[0][0] for result in results),
                         ['topic ops: thread 501 (created)', 'topic ops: thread 501 (recorded earlier)'])
        self.assertEqual(self.rows(), [('ops', 501, 'created'), ('web', 502, 'created')])

    def test_conversation_for_id_answers_the_paired_chat_and_recorded_topics(self):
        self.store.record_topic('ops', 501, 'created')
        self.store.record_topic('gone', 503, 'created')
        paired = self.bridge.conversation_for_id('123')
        self.assertIs(paired, self.bridge.conversation)
        self.assertEqual(json.dumps(paired), json.dumps(dict(scope_id='test-city', provider='telegram',
                                                             account_id='test_bot', conversation_id='123', kind='dm')))
        topic = dict(scope_id='test-city', provider='telegram', account_id='test_bot', conversation_id='123:topic:501',
                     parent_conversation_id='123', kind='thread')
        self.assertEqual(self.bridge.conversation_for_id('123:topic:501'), topic)
        self.assertEqual(self.bridge.topic_conversation(501), topic)
        for unknown in ('123:topic:502', '123:topic:503', '124', '124:topic:501', '123:topic:0', '123:topic:x',
                        '123:topic:0501', '123:topic:-501', '123:topic:', '123:topic:501:1', '123:topic:' + '9' * 30,
                        '', None, 123):
            with self.subTest(conversation_id=unknown):
                self.assertIsNone(self.bridge.conversation_for_id(unknown))

    def test_a_configured_topic_without_a_row_has_no_conversation(self):
        self.assertIsNone(self.bridge.conversation_for_id('123:topic:501'))
        self.store.record_topic('web', 502, 'recorded')
        self.assertIsNone(self.bridge.topic_by_name('ops'))
        self.assertEqual(self.bridge.conversation_for_id('123:topic:502')['conversation_id'], '123:topic:502')

    def test_topics_by_name_and_by_thread_join_the_settings_and_the_ledger(self):
        self.store.record_topic('web', 502, 'recorded')
        self.store.record_topic('gone', 503, 'created')
        web = dict(name='web', agent='helper', thread_id=502, conversation=self.bridge.topic_conversation(502))
        self.assertEqual(self.bridge.topic_by_name('web'), web)
        self.assertEqual(self.bridge.topic_by_thread(502), web)
        for found in (self.bridge.topic_by_name('ops'), self.bridge.topic_by_name('gone'),
                      self.bridge.topic_by_name('WEB'), self.bridge.topic_by_thread(503),
                      self.bridge.topic_by_thread(501)):
            self.assertIsNone(found)
        # Another process records a topic: the next lookup sees it.
        b.Store(self.path, 10).record_topic('ops', 501, 'recorded')
        self.assertEqual(self.bridge.topic_by_thread(501)['name'], 'ops')

    def test_a_bridge_without_topics_answers_only_the_paired_chat(self):
        bridge = b.Bridge(self.config | {'topics': ()}, self.store, 'SECRET', 'CAPABILITY')
        self.store.record_topic('ops', 501, 'created')
        self.assertIs(bridge.conversation_for_id('123'), bridge.conversation)
        self.assertIsNone(bridge.conversation_for_id('123:topic:501'))
        self.assertEqual(bridge.topic_settings, ())


class TopicsCliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.settings = write_settings(self.root)
        self.settings.write_text(self.settings.read_text() + topic_tables(('ops', None), ('web', None)))
        self.next_id = 500

    def answer(self, url, body):
        if url.endswith('/getMe'):
            return GET_ME
        if url.endswith('/createForumTopic'):
            self.next_id += 1
            return 200, {'ok': True, 'result': {'message_thread_id': self.next_id, 'name': body['name']}}
        raise AssertionError('unexpected curl invocation ' + url)

    def cli(self, *argv):
        """Run main() topics with the temporary settings and curl faked
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
                run_main(self, 'bridge.py', '--settings', str(self.settings), 'topics', *argv)
            finally:
                self.err, self.out = err.getvalue(), out.getvalue()
        return self.out

    def test_topics_prints_each_line(self):
        self.assertEqual(self.cli(), f'topic ops: thread 501 (created)\n{bind(501)}\n'
                                     f'topic web: thread 502 (created)\n{bind(502)}\n')
        self.assertEqual([method for method, _ in self.requests], ['getMe', 'createForumTopic', 'createForumTopic'])
        self.assertEqual(self.cli(), f'topic ops: thread 501 (recorded earlier)\n{bind(501)}\n'
                                     f'topic web: thread 502 (recorded earlier)\n{bind(502)}\n')
        self.assertEqual(self.requests, [])
        self.assertTrue((self.root / 'state' / 'telegram-topics.lock').exists())

    def test_a_topic_without_a_thread_id_prints_every_line_and_exits_1(self):
        self.answer = lambda url, body: GET_ME if url.endswith('/getMe') else (502, {'ok': False, 'error_code': 502})
        with self.assertRaisesRegex(b.TransportError, r'^not every \[\[topic\]\] has a thread id$'):
            self.cli()
        self.assertEqual([line.split(' (')[0] for line in self.out.splitlines()],
                         ['topic ops: not created', 'topic web: not created'])
        del self.answer
        with self.assertRaises(b.TransportError):  # web still has no thread id
            self.cli('--record', 'ops', '--thread-id', '640')
        self.assertEqual(self.out.splitlines()[:2], ['topic ops: thread 640 (recorded by hand)', bind(640)])
        self.assertEqual(self.requests, [])
        self.assertIn('topic web: thread 501 (created)', self.cli('--retry', 'web'))
        self.assertEqual([method for method, _ in self.requests], ['getMe', 'createForumTopic'])

    def test_flag_errors_exit_through_the_parser(self):
        cases = [['topics', '--record', 'ops'],
                 ['topics', '--thread-id', '5'],
                 ['topics', '--retry', 'ops', '--thread-id', '5'],
                 ['topics', '--record', 'ops', '--thread-id', '5', '--retry', 'ops'],
                 ['topics', '--record', 'ops', '--thread-id', '0'],
                 ['topics', '--record', 'ops', '--thread-id', 'x'],
                 ['topics', '--record', 'ops', '--thread-id', '-5'],
                 ['send', '--body-file', 'b', '--idempotency-key', 'k', '--retry', 'ops'],
                 ['pin', '--slot', 'waiting', '--unpin', '--thread-id', '5'],
                 ['run', '--record', 'ops', '--thread-id', '5']]
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
                        'if "/getMe" in config:\n'
                        '    result = {"id": 999, "is_bot": True, "has_topics_enabled": True}\n'
                        'else:\n'
                        '    result = {"message_thread_id": 501, "name": "ops"}\n'
                        'print(json.dumps({"ok": True, "result": result}))\n'
                        'print("200 0.010000 0.020000 0", end="")\n')
        env = {k: v for k, v in os.environ.items() if not k.startswith('GC_')}
        env.update(PATH=f'{bin_dir}:{env["PATH"]}', TELEGRAM_BRIDGE_SETTINGS=str(self.settings))
        command = ['sh', str(PACK / 'commands' / 'topics' / 'run.sh')]
        result = subprocess.run(command + ['--record', 'web', '--thread-id', '777'],
                                env=env, cwd=self.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, 'topic ops: not created (not attempted); run gc telegram topics to create it\n'
                                        f'topic web: thread 777 (recorded by hand)\n{bind(777)}\n')
        self.assertEqual(list(self.root.glob('curl-*')), [])
        result = subprocess.run(command, env=env, cwd=self.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f'topic ops: thread 501 (created)\n{bind(501)}\n'
                                        f'topic web: thread 777 (recorded earlier)\n{bind(777)}\n')
        configs = sorted((p.read_text() for p in self.root.glob('curl-*')), key=lambda c: '/getMe' not in c)
        self.assertEqual(len(configs), 2)
        self.assertIn('/createForumTopic"', configs[1])
        self.assertIn('\\"name\\": \\"ops\\"', configs[1])


if __name__ == '__main__':
    unittest.main()
