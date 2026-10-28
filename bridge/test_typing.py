"""Typing: the [typing] settings and the "typing..." chat action the worker
sends after GC accepted a message until the next send, with the fake Telegram
and GC, an injected clock and an injected wait, so no test sleeps."""
from contextlib import closing
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
from test_bridge import BridgeTestCase, write_settings  # noqa: E402

TYPING = dict(chat_id=123, action='typing')
START = 1000.0


class TypingSettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def load(self, table=None):
        path = write_settings(self.root)
        if table is not None:
            path.write_text(path.read_text() + '\n[typing]\n' + table)
        return b.load_settings(path)

    def test_a_file_without_the_table_loads_as_before(self):
        settings = self.load()
        self.assertNotIn('typing', settings.config)
        self.assertEqual(b.Bridge(settings.config, None, 'SECRET', 'CAPABILITY').typing_seconds, 0)

    def test_an_empty_table_is_off(self):
        self.assertEqual(self.load('').config['typing'], dict(seconds=0))

    def test_seconds_from_0_to_900_load(self):
        for seconds in (0, 1, 300, 900):
            with self.subTest(seconds=seconds):
                settings = self.load(f'seconds = {seconds}\n')
                self.assertEqual(settings.config['typing'], dict(seconds=seconds))
                self.assertEqual(b.Bridge(settings.config, None, 'SECRET', 'CAPABILITY').typing_seconds, seconds)

    def test_a_bad_value_refuses_naming_the_key(self):
        for value in ('-1', '901', '"300"', 'true', '1.5'):
            with self.subTest(value=value), \
                    self.assertRaisesRegex(b.SettingsError, r'^settings: typing\.seconds must be an integer from 0 to 900$'):
                self.load(f'seconds = {value}\n')

    def test_an_unknown_key_or_a_non_table_refuses(self):
        with self.assertRaisesRegex(b.SettingsError, r'^settings: unknown key typing\.interval$'):
            self.load('seconds = 300\ninterval = 4\n')
        path = write_settings(self.root)
        path.write_text('typing = 300\n' + path.read_text())
        with self.assertRaisesRegex(b.SettingsError, 'typing must be a table'):
            b.load_settings(path)


class TypingTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.now = START
        self.actions = []
        self.action_answer = (200, dict(ok=True, result=True))
        self.hooks = {}
        self.ticks = 0
        self.bridge = self.make_bridge(300)

    def make_bridge(self, seconds, store=None, **config):
        if seconds is not None:
            config['typing'] = dict(seconds=seconds)
        return b.Bridge(dict(self.config, **config), store or self.store, 'SECRET', 'CAPABILITY',
                        request=self.transport, request_multipart=self.multipart_transport,
                        request_download=self.download_transport, sleep=lambda seconds: None,
                        monotonic=lambda: self.now, typing_wait=self.wait)

    def transport(self, url, body, *args, **kwargs):
        if url.endswith('/sendChatAction'):
            self.actions.append((self.now, body, args[0] if args else kwargs.get('timeout')))
            if isinstance(self.action_answer, Exception):
                raise self.action_answer
            return self.action_answer
        if url.endswith(('/setMessageReaction', '/answerCallbackQuery', '/pinChatMessage')):
            self.calls.append((url, body))
            return 200, dict(ok=True, result=True)
        if url.endswith('/editMessageText'):
            self.calls.append((url, body))
            return 200, dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
        return super().transport(url, body, *args, **kwargs)

    def wait(self, timeout):
        """The worker's wait: move the clock on, run what the test scheduled
        for the new time, and stop the worker after its last tick."""
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

    def action_times(self):
        return [moment - START for moment, _, _ in self.actions]

    def ledger_rows(self):
        with closing(sqlite3.connect(self.path)) as db:
            return [row[0] for row in db.execute('SELECT key FROM deliveries ORDER BY rowid')]

    def test_seconds_0_makes_no_chat_action_ever(self):
        self.bridge = self.make_bridge(0)
        self.bridge.drain([self.update(uid=10), self.update(uid=11)])
        self.assertIsNone(self.bridge.typing_window)
        self.work(20)
        self.assertEqual(self.actions, [])
        self.bridge = self.make_bridge(None)  # no [typing] table at all
        self.bridge.drain([self.update(uid=12)])
        self.work(20)
        self.assertEqual(self.actions, [])
        self.assertEqual(self.store.offset(), 13)

    def test_an_accepted_message_opens_a_window_and_the_first_action_goes_out_at_once(self):
        self.bridge.drain([self.update(uid=10)])
        self.assertEqual(self.store.offset(), 11)
        self.assertEqual(self.actions, [])  # the poll loop only opens the window
        self.assertEqual(self.bridge.typing_window['end'], START + 300)
        self.assertTrue(self.bridge.typing_wake.is_set())
        self.work(1)
        self.assertEqual(self.actions, [(START, TYPING, b.TYPING_TIMEOUT)])
        self.assertEqual(self.ledger_rows(), [])  # no reservation, no row
        self.assertIsNone(self.bridge.typing_window)  # stop ended it

    def test_actions_repeat_every_4_seconds_until_the_end(self):
        self.bridge = self.make_bridge(10)
        self.bridge.drain([self.update(uid=10)])
        self.work(15)
        self.assertEqual(self.action_times(), [0, 4, 8])
        self.assertIsNone(self.bridge.typing_window)

    def test_a_later_accepted_message_extends_the_window(self):
        self.bridge = self.make_bridge(10)
        self.bridge.drain([self.update(uid=10)])
        self.hooks[START + 6] = lambda: self.bridge.drain([self.update(uid=11)])
        self.work(25)
        self.assertEqual(self.action_times(), [0, 4, 6, 10, 14])
        self.assertIsNone(self.bridge.typing_window)
        self.assertEqual(self.store.offset(), 12)

    def test_a_daemon_send_closes_the_window(self):
        self.bridge.drain([self.update(uid=10)])
        self.hooks[START + 6] = lambda: self.assertTrue(self.bridge.publish(self.publish(key='reply-1'))['delivered'])
        self.work(20)
        self.assertEqual(self.action_times(), [0, 4])
        self.assertIsNone(self.bridge.typing_window)

    def test_a_cli_send_on_another_store_connection_closes_the_window(self):
        self.bridge.drain([self.update(uid=10)])
        cli = self.make_bridge(None, store=b.Store(self.path, 10))  # a CLI process: its own Bridge and Store
        self.hooks[START + 5] = lambda: self.assertTrue(cli.publish(self.publish(key='cli-1'))['delivered'])
        self.work(20)
        self.assertEqual(self.action_times(), [0, 4])
        self.assertIsNone(self.bridge.typing_window)

    def test_a_cli_pin_edit_closes_the_window(self):
        cli = self.make_bridge(None, store=b.Store(self.path, 10))
        self.assertTrue(cli.pin('waiting', 'Nothing waiting')['delivered'])
        self.bridge.drain([self.update(uid=10)])
        self.hooks[START + 5] = lambda: self.assertTrue(cli.pin('waiting', 'One waiting')['changed'])
        self.work(20)
        self.assertEqual(self.ledger_rows()[-1], 'pin:waiting:g1:r1')  # the edit's row alone
        self.assertEqual(self.action_times(), [0, 4])
        self.assertIsNone(self.bridge.typing_window)

    def test_an_accept_reaction_row_keeps_the_window_and_an_agent_reaction_closes_it(self):
        self.bridge = self.make_bridge(300, reactions=dict(b.REACTIONS_DEFAULTS, on_accept=True))
        self.bridge.drain([self.update(uid=10)])
        self.bridge.reaction_queue.join()
        self.hooks[START + 3] = lambda: (self.bridge.drain([self.update(uid=11)]), self.bridge.reaction_queue.join())
        self.work(10)
        self.assertEqual(self.ledger_rows(), ['reaction:10:accept', 'reaction:11:accept'])
        self.assertEqual(self.action_times(), [0, 3, 7])  # still open when stop came
        self.bridge.stop.clear()
        self.bridge.drain([self.update(uid=12)])
        self.bridge.reaction_queue.join()
        cli = self.make_bridge(None, store=b.Store(self.path, 10))
        args = b._build_parser().parse_args(['react', '--reaction', 'done'])
        self.hooks[self.now + 2] = lambda: b._react(cli, args)
        self.work(10)
        self.assertEqual(self.action_times(), [0, 3, 7, 10])
        self.assertIsNone(self.bridge.typing_window)

    def test_stop_ends_the_window(self):
        self.bridge.drain([self.update(uid=10)])
        self.work(3)
        self.assertTrue(self.bridge.stop.is_set())
        self.assertIsNone(self.bridge.typing_window)
        self.assertEqual(self.action_times(), [0])
        self.bridge._typing_loop()  # a stopped worker sends nothing more
        self.assertEqual(self.action_times(), [0])

    def test_the_quota_notice_closes_the_window(self):
        until = int((time.time() + 3600) * 1000)
        with patch.object(self.bridge, 'mayor_quota_episode', return_value=('episode-1', until)):
            self.bridge.drain([self.update(uid=10)])
        self.assertEqual(self.ledger_rows(), ['quota-exhausted-episode-1'])
        self.work(5)
        self.assertEqual(self.actions, [])
        self.assertIsNone(self.bridge.typing_window)

    def test_a_window_a_send_already_ended_begins_again_for_the_next_message(self):
        self.bridge.drain([self.update(uid=10)])
        first = self.bridge.typing_window
        self.assertTrue(self.bridge.publish(self.publish(key='reply-1'))['delivered'])
        self.bridge.drain([self.update(uid=11)])  # before the worker saw the reply
        self.assertIsNot(self.bridge.typing_window, first)
        self.assertEqual(self.bridge.typing_window['mark'], self.store.deliveries_mark())
        self.work(5)
        self.assertEqual(self.action_times(), [0, 4])

    def test_a_city_command_and_an_ask_answer_open_no_window(self):
        command = dict(name='deploy', description='Deploy', handler='/bin/true', menu=True,
                       callback_prefix='deploy:', timeout=10)
        self.bridge = self.make_bridge(300, commands=(command,))
        update = self.update(uid=10)
        update['message']['text'] = '/deploy main'
        with patch.object(self.bridge, 'run_command') as run_command:
            self.bridge.drain([update])
        run_command.assert_called_once()
        self.assertIsNone(self.bridge.typing_window)
        ask_id = b.Bridge._ask_id_for_key('ask-1')
        request = dict(self.publish(key='ask-1', text='Ship it?'),
                       keyboard=[[dict(text='Yes', callback_data=f'ask:{ask_id}:yes')]])
        self.assertTrue(self.bridge.publish(request)['delivered'])
        press = dict(update_id=41, callback_query=dict(
            id='query-41', data=f'ask:{ask_id}:yes', chat_instance='x', **{'from': dict(id=123, is_bot=False)},
            message=dict(message_id=77, chat=dict(id=123, type='private'))))
        self.bridge.drain([press])
        self.assertTrue(self.store.get_ask(ask_id)['relayed'])
        self.assertIsNone(self.bridge.typing_window)
        self.work(5)
        self.assertEqual(self.actions, [])

    def test_a_failing_action_logs_once_per_window_and_never_touches_the_offset_or_a_send(self):
        self.bridge = self.make_bridge(10)
        self.action_answer = b.RequestNotSent('network request not sent', 7, 'connect')
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            self.bridge.drain([self.update(uid=10)])
            self.work(15)
            self.assertEqual(self.action_times(), [0, 4, 8])  # one attempt each, never retried
            self.assertEqual(self.store.offset(), 11)
            self.assertTrue(self.bridge.publish(self.publish(key='reply-1'))['delivered'])
            self.bridge.stop.clear()
            self.bridge.drain([self.update(uid=11)])  # a new window logs again
            self.work(2)
        self.assertEqual(log.getvalue().count('typing not shown: transport_unavailable'), 2)
        self.assertNotIn('hello', log.getvalue())
        self.assertEqual(self.store.offset(), 12)
        self.assertEqual(self.ledger_rows(), ['reply-1'])

    def test_a_message_after_the_window_ended_begins_a_window_that_logs_its_own_failure(self):
        self.bridge = self.make_bridge(10)
        self.action_answer = b.RequestNotSent('network request not sent', 7, 'connect')
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            self.bridge.drain([self.update(uid=10)])
            self.bridge._typing_tick()
            self.now = START + 10  # the window's end; the worker has not looked yet
            self.bridge.drain([self.update(uid=11)])
            self.assertEqual(self.bridge.typing_window['end'], START + 20)
            self.assertFalse(self.bridge.typing_window['failed'])
            self.bridge._typing_tick()
        self.assertEqual(self.action_times(), [0, 10])
        self.assertEqual(log.getvalue().count('typing not shown: transport_unavailable'), 2)

    def test_a_tick_that_fails_ends_the_window_and_the_next_message_shows_typing(self):
        self.action_answer = ValueError('unexpected answer hello')
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            self.bridge.drain([self.update(uid=10)])
            self.work(10)
            self.assertEqual(self.action_times(), [0])
            self.action_answer = (200, dict(ok=True, result=True))
            self.bridge.stop.clear()
            self.bridge.drain([self.update(uid=11)])
            self.work(1)
        self.assertEqual(self.action_times(), [0, 10])
        self.assertEqual(log.getvalue().count('typing not shown: typing_failed'), 1)
        self.assertNotIn('hello', log.getvalue())
        self.assertEqual(self.store.offset(), 12)

    def test_a_worker_that_cannot_start_logs_once_and_the_bridge_runs_without_it(self):
        with patch.object(b.threading.Thread, 'start', side_effect=RuntimeError("can't start new thread")), \
                patch('sys.stderr', new_callable=io.StringIO) as log:
            self.bridge._start_typing()
            self.bridge.drain([self.update(uid=10)])
        self.assertIsNone(self.bridge.typing_worker)
        self.assertEqual(log.getvalue().count('typing not shown: typing_worker_unavailable'), 1)
        self.assertEqual(self.store.offset(), 11)
        self.assertEqual(self.actions, [])

    def test_a_rejected_action_ends_the_window(self):
        self.action_answer = (400, dict(ok=False, error_code=400, description='Bad Request: chat not found'))
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            self.bridge.drain([self.update(uid=10)])
            self.work(10)
        self.assertEqual(self.action_times(), [0])
        self.assertIsNone(self.bridge.typing_window)
        self.assertEqual(log.getvalue().count('typing not shown: telegram_rejected_400'), 1)
        self.assertNotIn('chat not found', log.getvalue())

    def test_an_unreadable_ledger_ends_the_window(self):
        self.bridge.drain([self.update(uid=10)])
        with patch.object(self.store, 'sent_since', side_effect=sqlite3.OperationalError('locked')), \
                patch('sys.stderr', new_callable=io.StringIO) as log:
            self.work(3)
            self.bridge.stop.clear()
            self.bridge.drain([self.update(uid=11)])  # the accept point never raises
            self.assertEqual(self.store.offset(), 12)
        self.assertEqual(self.actions, [])
        self.assertEqual(log.getvalue().count('typing not shown: ledger_unavailable'), 1)

    def test_run_starts_one_worker_only_with_seconds_above_0(self):
        for seconds, started in ((300, True), (0, False)):
            with self.subTest(seconds=seconds):
                bridge = b.Bridge(dict(self.config, typing=dict(seconds=seconds)), self.store, 'SECRET', 'CAPABILITY',
                                  port=0, request=self.transport)
                bridge.stop.set()
                with patch.object(bridge, 'probe_with_retry', return_value=True), \
                        patch.object(bridge, 'register'), patch.object(bridge, 'gc'), \
                        patch('sys.stdout', new_callable=io.StringIO):
                    bridge.run()
                if started:
                    self.assertEqual(bridge.typing_worker.name, 'typing')
                    bridge.typing_worker.join(10)
                    self.assertFalse(bridge.typing_worker.is_alive())  # stopped with bridge.stop
                else:
                    self.assertIsNone(bridge.typing_worker)
        self.assertEqual(self.actions, [])


if __name__ == '__main__':
    unittest.main()
