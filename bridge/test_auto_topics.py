"""Topics the paired user creates ([auto_topics], OPERATIONS.md, "Topics"):
the ledger's auto_topics table, their display names, which of them are served
and the `topics` command's lines, with the fake Telegram, temporary
directories and no network."""
from contextlib import closing
import sqlite3
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
from test_bridge import BridgeTestCase  # noqa: E402
from test_topics import GET_ME, USERS_ALLOWED, bind  # noqa: E402

AUTO_ON = {'enabled': True}
AUTO_OFF = {'enabled': False}


def row(thread_id, name):
    return dict(thread_id=thread_id, name=name)


class AutoTopicLedgerTests(BridgeTestCase):
    """The auto_topics table and its Store methods."""

    def dump(self):
        with closing(sqlite3.connect(self.path)) as db:
            tables = dict(db.execute("SELECT name, sql FROM sqlite_master WHERE type='table'"))
            rows = {name: db.execute(f'SELECT * FROM {name}').fetchall() for name in tables}
        return tables, rows

    def test_a_fresh_ledger_has_the_table(self):
        with closing(sqlite3.connect(self.path)) as db:
            columns = [row[1:6] for row in db.execute('PRAGMA table_info(auto_topics)')]
        self.assertEqual(columns, [('thread_id', 'INTEGER', 0, None, 1), ('name', 'TEXT', 0, None, 0),
                                   ('name_implicit', 'INTEGER', 1, '0', 0), ('agent', 'TEXT', 1, None, 0),
                                   ('detected_at', 'TEXT', 1, None, 0), ('bound_at', 'TEXT', 0, None, 0)])

    def test_an_old_ledger_without_the_table_opens_and_gains_it(self):
        self.store.record_topic('ops', 501, 'created')
        self.store.advance(42)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TABLE auto_topics')
        tables, rows = self.dump()
        store = b.Store(self.path, 10)
        after_tables, after_rows = self.dump()
        self.assertEqual({k: v for k, v in after_tables.items() if k != 'auto_topics'}, tables)
        self.assertEqual({k: v for k, v in after_rows.items() if k != 'auto_topics'}, rows)
        self.assertEqual(after_rows['auto_topics'], [])
        self.assertEqual((store.offset(), store.auto_topic_rows()), (42, []))
        b.Store(self.path, 10)
        self.assertEqual(self.dump(), (after_tables, after_rows))

    def test_insert_if_absent_is_idempotent_and_keeps_detected_at(self):
        self.assertTrue(self.store.add_auto_topic(601, 'mayor'))
        first = self.store.auto_topic_rows()
        self.assertEqual(first, [dict(thread_id=601, name=None, name_implicit=False, agent='mayor',
                                      detected_at=first[0]['detected_at'], bound_at=None)])
        self.assertTrue(first[0]['detected_at'].endswith('+00:00'))
        self.assertFalse(self.store.add_auto_topic(601, 'helper'))
        self.assertEqual(self.store.auto_topic_rows(), first)

    def test_name_implicit_and_bound_at_are_set_and_cleared(self):
        self.store.add_auto_topic(601, 'mayor')
        self.assertTrue(self.store.set_auto_topic_name(601, 'New topic', True))
        self.assertEqual([(r['name'], r['name_implicit']) for r in self.store.auto_topic_rows()], [('New topic', True)])
        self.assertTrue(self.store.set_auto_topic_name(601, 'Ops', False))
        self.assertEqual([(r['name'], r['name_implicit']) for r in self.store.auto_topic_rows()], [('Ops', False)])
        self.assertTrue(self.store.set_auto_topic_bound(601, True))
        bound_at = self.store.auto_topic_rows()[0]['bound_at']
        self.assertTrue(bound_at.endswith('+00:00'))
        self.assertTrue(self.store.set_auto_topic_bound(601, False))
        self.assertIsNone(self.store.auto_topic_rows()[0]['bound_at'])
        self.assertFalse(self.store.set_auto_topic_name(602, 'x', False))
        self.assertFalse(self.store.set_auto_topic_bound(602, True))
        self.assertEqual([r['thread_id'] for r in self.store.auto_topic_rows()], [601])

    def test_rows_come_in_rowid_order_which_is_the_thread_ids(self):
        for thread_id in (705, 601, 650):
            self.store.add_auto_topic(thread_id, 'mayor')
        self.store.set_auto_topic_name(601, 'later name', False)
        self.assertEqual([r['thread_id'] for r in self.store.auto_topic_rows()], [601, 650, 705])


class AutoTopicNameTests(unittest.TestCase):
    """auto_topic_names: a pure function of the rows and the settings names."""

    def name(self, name, thread_id=601, settings=()):
        return b.auto_topic_names([row(thread_id, name)], settings)[thread_id]

    def test_a_plain_name_is_kept(self):
        self.assertEqual(self.name('Weekly plan'), 'Weekly plan')

    def test_delimiters_controls_and_line_separators_become_spaces(self):
        self.assertEqual(self.name('[a|b]'), 'a b')
        self.assertEqual(self.name('a\x07b\u2028c\u2029d\x85e\nf'), 'a b c d e f')

    def test_whitespace_collapses_and_is_stripped(self):
        self.assertEqual(self.name('  a \t\u00a0 b  '), 'a b')

    def test_a_long_name_is_cut_to_128_characters(self):
        self.assertEqual(self.name('x' * 200), 'x' * 128)
        self.assertEqual(self.name('x' * 127 + ' yz'), 'x' * 127)

    def test_an_empty_or_unknown_name_is_the_thread(self):
        for name in ('', None, ' [|] ', '\x00\u2028'):
            with self.subTest(name=name):
                self.assertEqual(self.name(name), 'thread 601')

    def test_a_name_a_settings_topic_has_ignoring_case_is_suffixed(self):
        self.assertEqual(self.name('OPS', settings=('ops', 'web')), 'OPS #601')
        self.assertEqual(self.name('STRASSE', settings=('Straße',)), 'STRASSE #601')
        self.assertEqual(self.name('ops log', settings=('ops',)), 'ops log')

    def test_rows_with_one_name_are_each_suffixed_in_either_order(self):
        rows = [row(601, 'Plan'), row(602, 'plan'), row(603, 'Other')]
        expected = {601: 'Plan #601', 602: 'plan #602', 603: 'Other'}
        self.assertEqual(b.auto_topic_names(rows, ()), expected)
        self.assertEqual(b.auto_topic_names(rows[::-1], ()), expected)
        self.assertEqual(b.auto_topic_names([row(601, None), row(602, 'thread 601')], ()),
                         {601: 'thread 601 #601', 602: 'thread 601 #602'})

    def test_every_result_is_unique_ignoring_case_and_has_no_delimiter(self):
        rows = [row(601, 'x'), row(602, 'X'), row(603, 'x #601'), row(604, 'ops #604'), row(605, 'a|b')]
        settings = ('OPS #604', 'ops #604 #604', 'A b', 'a b #605')
        for order in (rows, rows[::-1], rows[2:] + rows[:2]):
            with self.subTest(order=[r['thread_id'] for r in order]):
                names = b.auto_topic_names(order, settings)
                folded = [name.casefold() for name in names.values()] + [name.casefold() for name in settings]
                self.assertEqual(len(set(folded)), len(folded))
                self.assertFalse(any(char in '[]|' for name in names.values() for char in name))
                self.assertEqual(names, b.auto_topic_names(rows, settings))
        self.assertEqual(names[601], 'x #601 #601')
        self.assertEqual(names[603], 'x #601 #603')
        self.assertEqual(names[604], 'ops #604 #604 #604')
        self.assertEqual(names[605], 'a b #605 #605')


class AutoTopicsTests(BridgeTestCase):
    """Bridge.served_auto_topics and the `topics` command against a fake Telegram."""

    TOPICS = (dict(name='ops', agent='mayor'),)

    def setUp(self):
        super().setUp()
        self.get_me = GET_ME
        self.next_id = 500

    def make_bridge(self, auto=AUTO_ON, topics=TOPICS):
        config = dict(self.config, topics=topics)
        if auto is not None:
            config['auto_topics'] = auto
        return b.Bridge(config, self.store, 'SECRET', 'CAPABILITY', request=self.topic_transport,
                        sleep=lambda delay: None)

    def topic_transport(self, url, body, *args, **kwargs):
        method = url.rsplit('/', 1)[1]
        self.calls.append((method, body))
        if method == 'getMe':
            return self.get_me
        if method == 'createForumTopic':
            self.next_id += 1
            return 200, {'ok': True, 'result': {'message_thread_id': self.next_id, 'name': body['name']}}
        raise AssertionError('unexpected Telegram method ' + method)

    def add(self, thread_id, name=None, bound=False, agent='mayor'):
        self.store.add_auto_topic(thread_id, agent)
        if name is not None:
            self.store.set_auto_topic_name(thread_id, name, False)
        if bound:
            self.store.set_auto_topic_bound(thread_id, True)

    def run_topics(self, bridge, **flags):
        self.calls.clear()
        return bridge.topics(**flags)

    def test_served_rows_carry_their_display_name_and_conversation(self):
        self.add(701, 'Plan', bound=True)
        self.add(702)
        served = self.make_bridge().served_auto_topics()
        self.assertEqual([(t['thread_id'], t['display_name'], t['agent'], t['bound_at'] is not None) for t in served],
                         [(701, 'Plan', 'mayor', True), (702, 'thread 702', 'mayor', False)])
        self.assertEqual(served[0]['conversation'], dict(scope_id='test-city', provider='telegram',
                                                         account_id='test_bot', conversation_id='123:topic:701',
                                                         parent_conversation_id='123', kind='thread'))

    def test_a_thread_a_topics_row_holds_is_not_served(self):
        self.add(701, 'ops')
        self.add(702, 'Left')
        self.add(703, 'OPS')
        self.store.record_topic('ops', 701, 'recorded')
        self.store.record_topic('gone', 702, 'created')  # a name not in the settings
        served = self.make_bridge().served_auto_topics()
        self.assertEqual([(t['thread_id'], t['display_name']) for t in served], [(703, 'OPS #703')])

    def test_auto_off_or_absent_serves_nothing(self):
        self.add(701, 'Plan', bound=True)
        for auto in (AUTO_OFF, None):
            with self.subTest(auto=auto):
                bridge = self.make_bridge(auto=auto)
                self.assertFalse(bridge.auto_topics)
                self.assertEqual(bridge.served_auto_topics(), [])
        self.assertEqual(b.Bridge(dict(self.config, auto_topics=AUTO_ON), None, 'SECRET', None).served_auto_topics(),
                         [])

    def test_users_allowed_prints_the_auto_line_in_place_of_the_warning(self):
        self.get_me = USERS_ALLOWED
        lines, complete = self.run_topics(self.make_bridge())
        self.assertEqual((lines, complete), (['users may create topics: the bridge serves each one (auto_topics)',
                                              'topic ops: thread 501 (created)', bind(501)], True))
        self.assertEqual([method for method, _ in self.calls], ['getMe', 'createForumTopic'])

    def test_auto_off_or_absent_prints_todays_lines(self):
        for auto in (AUTO_OFF, None):
            for get_me, first in ((USERS_ALLOWED, [b.Bridge.TOPIC_USERS_WARNING]), (GET_ME, [])):
                with self.subTest(auto=auto, get_me=get_me[1]['result']['allows_users_to_create_topics']):
                    self.store = b.Store(Path(self.tmp.name) / f'{auto}-{bool(first)}.sqlite3', 10)
                    self.get_me, self.next_id = get_me, 500
                    lines = self.run_topics(self.make_bridge(auto=auto))
                    self.assertEqual(lines, (first + ['topic ops: thread 501 (created)', bind(501)], True))
                    self.assertEqual(self.run_topics(self.make_bridge(auto=auto)),
                                     (['topic ops: thread 501 (recorded earlier)', bind(501)], True))
                    self.assertEqual(self.calls, [])

    def test_auto_on_without_users_allowed_prints_no_getme_line(self):
        lines, _ = self.run_topics(self.make_bridge())
        self.assertEqual(lines, ['topic ops: thread 501 (created)', bind(501)])

    def test_served_rows_are_listed_last_bound_or_not_bound_yet(self):
        self.store.record_topic('ops', 501, 'created')
        self.store.record_topic('gone', 502, 'created')
        self.add(705, 'Plan', bound=True, agent='helper')
        self.add(701)
        self.add(703, 'ops')
        self.add(502, 'shadowed')
        self.assertEqual(self.run_topics(self.make_bridge()), ([
            'topic ops: thread 501 (recorded earlier)', bind(501),
            'topic gone: thread 502 (not in settings; its messages reach the agent as not configured)',
            'auto topic thread 701: thread 701 (not bound yet)',
            'auto topic ops #703: thread 703 (not bound yet)',
            'auto topic Plan: thread 705 (bound to helper)'], True))
        self.assertEqual(self.calls, [])

    def test_auto_rows_never_change_the_exit_status(self):
        self.add(701, 'Plan')
        self.assertEqual(self.run_topics(self.make_bridge(topics=())),
                         (['no [[topic]] tables in the settings', 'auto topic Plan: thread 701 (not bound yet)'], True))
        self.assertEqual(self.calls, [])
        # A settings topic without a thread id still fails the run, served
        # auto topics or not; with every one recorded it succeeds.
        bridge = self.make_bridge(topics=self.TOPICS + (dict(name='web', agent='mayor'),))
        self.assertEqual(self.run_topics(bridge, record='ops', thread_id=501), ([
            'topic ops: thread 501 (recorded by hand)', bind(501),
            'topic web: not created (not attempted); run gc telegram topics to create it',
            'auto topic Plan: thread 701 (not bound yet)'], False))
        self.assertEqual(self.run_topics(bridge, record='web', thread_id=502)[1], True)

    def test_auto_off_with_rows_prints_how_many_are_not_served(self):
        self.store.record_topic('ops', 501, 'created')
        self.add(701, 'Plan', bound=True)
        self.add(702)
        self.add(501, 'shadowed')
        for auto in (AUTO_OFF, None):
            with self.subTest(auto=auto):
                self.assertEqual(self.run_topics(self.make_bridge(auto=auto)), ([
                    'topic ops: thread 501 (recorded earlier)', bind(501),
                    'auto topics: 2 recorded, not served (auto_topics off)'], True))
        self.assertEqual(self.calls, [])

    def test_record_onto_an_auto_thread_adopts_it_and_drops_it_from_the_listing(self):
        self.add(701, 'Plan', bound=True)
        self.add(702, 'Other')
        bridge = self.make_bridge(topics=(dict(name='ops', agent='mayor'), dict(name='plan', agent='helper')))
        self.assertEqual(self.run_topics(bridge, record='plan', thread_id=701), ([
            'topic ops: not created (not attempted); run gc telegram topics to create it',
            'topic plan: thread 701 (recorded by hand)', bind(701, 'helper'),
            'auto topic Other: thread 702 (not bound yet)'], False))
        self.assertEqual(self.calls, [])
        self.assertEqual([r['thread_id'] for r in self.store.auto_topic_rows()], [701, 702])
        self.assertEqual([t['thread_id'] for t in bridge.served_auto_topics()], [702])


if __name__ == '__main__':
    unittest.main()
