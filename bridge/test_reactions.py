"""Reactions: the [reactions] settings, the seen reaction the bridge sets on
the worker once GC durably accepted a message (on_accept), and the `react`
subcommand and `gc telegram react` command, with the fake Telegram and GC."""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
from test_bridge import BridgeTestCase, run_main, write_fake_curl, write_settings  # noqa: E402

PACK = Path(__file__).resolve().parents[1]
EYES, OK_HAND = '\U0001F440', '\U0001F44C'
REACTION_INVALID = (400, dict(ok=False, error_code=400, description='Bad Request: REACTION_INVALID'))


class ReactionSettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def load(self, table=None):
        path = write_settings(self.root)
        if table is not None:
            path.write_text(path.read_text() + '\n[reactions]\n' + table)
        return b.load_settings(path)

    def test_a_file_without_the_table_loads_as_before(self):
        settings = self.load()
        self.assertNotIn('reactions', settings.config)
        bridge = b.Bridge(settings.config, None, 'SECRET', 'CAPABILITY')
        self.assertEqual(bridge.reactions, dict(on_accept=False, seen=EYES, done=OK_HAND))

    def test_an_empty_table_takes_the_defaults(self):
        self.assertEqual(self.load('').config['reactions'], dict(on_accept=False, seen=EYES, done=OK_HAND))

    def test_every_key_can_be_set(self):
        reactions = self.load('on_accept = true\nseen = "\U0001FAE1"\ndone = "\U0001F4AF"\n').config['reactions']
        self.assertEqual(reactions, dict(on_accept=True, seen='\U0001FAE1', done='\U0001F4AF'))
        # A multi-codepoint emoji (a ZWJ sequence) passes the shape check.
        self.assertEqual(self.load('done = "\U0001F468‍\U0001F4BB"\n').config['reactions']['done'],
                         '\U0001F468‍\U0001F4BB')

    def test_a_bad_emoji_refuses_naming_its_key(self):
        for key in ('seen', 'done'):
            for value in ('""', '"ok"', '"\U0001F44D \U0001F44D"', '"a\U0001F44D"', '"1"', '"\\u0001"',
                          '"' + '\U0001F44D' * 17 + '"', '5', 'true'):
                with self.subTest(key=key, value=value), \
                        self.assertRaisesRegex(b.SettingsError, f'reactions.{key} must be one emoji'):
                    self.load(f'{key} = {value}\n')

    def test_on_accept_must_be_a_boolean(self):
        with self.assertRaisesRegex(b.SettingsError, 'reactions.on_accept must be true or false'):
            self.load('on_accept = "yes"\n')

    def test_an_unknown_key_or_a_non_table_refuses(self):
        with self.assertRaisesRegex(b.SettingsError, r'^settings: unknown key reactions\.typing$'):
            self.load('typing = true\n')
        path = write_settings(self.root)
        path.write_text('reactions = 1\n' + path.read_text())
        with self.assertRaisesRegex(b.SettingsError, 'reactions must be a table'):
            b.load_settings(path)


class AcceptReactionTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.reaction_answer = (200, dict(ok=True, result=True))
        self.reaction_threads = []
        self.bridge = self.make_bridge(on_accept=True)

    def make_bridge(self, **reactions):
        config = dict(self.config, reactions=dict(b.REACTIONS_DEFAULTS, **reactions)) if reactions else self.config
        return b.Bridge(config, self.store, 'SECRET', 'CAPABILITY', request=self.transport,
                        request_multipart=self.multipart_transport, request_download=self.download_transport,
                        sleep=lambda seconds: None)

    def transport(self, url, body, *args, **kwargs):
        if url.endswith('/setMessageReaction'):
            self.calls.append((url, body))
            self.reaction_threads.append(threading.current_thread().name)
            if isinstance(self.reaction_answer, Exception):
                raise self.reaction_answer
            return self.reaction_answer
        if url.endswith('/answerCallbackQuery'):
            self.calls.append((url, body))
            return 200, dict(ok=True, result=True)
        if url.endswith('/editMessageText'):
            self.calls.append((url, body))
            return 200, dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
        return super().transport(url, body, *args, **kwargs)

    def reactions(self):
        self.bridge.reaction_queue.join()
        return [body for url, body in self.calls if url.endswith('/setMessageReaction')]

    def seen(self, mid):
        return dict(chat_id=123, message_id=mid, reaction=[dict(type='emoji', emoji=EYES)])

    def test_an_accepted_text_message_gets_one_seen_reaction_off_the_poll_thread(self):
        self.bridge.drain([self.update(uid=10)])
        self.assertEqual(self.store.offset(), 11)
        self.assertEqual(self.reactions(), [self.seen(10)])
        self.assertEqual(self.reaction_threads, ['accept-reaction'])
        self.assertEqual(self.store.last_accepted(), 10)
        with closing(sqlite3.connect(self.path)) as db:
            receipt = json.loads(db.execute("SELECT receipt FROM deliveries WHERE key='reaction:10:accept'").fetchone()[0])
        self.assertTrue(receipt['delivered'])

    def test_a_replayed_update_sends_no_second_reaction(self):
        self.bridge.drain([self.update(uid=10)])
        self.reactions()
        self.bridge.inbound(self.update(uid=10))  # reprocessed after a retained cursor
        self.assertEqual(self.reactions(), [self.seen(10)])

    def test_a_not_sent_accept_reaction_is_never_retried_by_a_replay(self):
        self.reaction_answer = b.RequestNotSent('network request not sent', 7, 'connect')
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            self.bridge.drain([self.update(uid=10)])
            self.assertEqual(len(self.reactions()), b.SEND_ATTEMPTS)
            self.reaction_answer = (200, dict(ok=True, result=True))
            self.bridge.inbound(self.update(uid=10))
            self.assertEqual(len(self.reactions()), b.SEND_ATTEMPTS)
        self.assertIn(f'reaction not set (msg 10): {b.NOT_SENT_REASON}', log.getvalue())

    def test_media_and_a_forward_get_the_reaction_as_the_users_messages(self):
        self.bridge.state_dir = Path(self.tmp.name)
        forward = self.update(uid=11)
        forward['message']['forward_origin'] = dict(type='hidden_user', sender_user_name='Sam', date=1700000000)
        self.bridge.drain([self.photo_message(uid=10), forward])
        self.assertEqual(self.store.offset(), 12)
        self.assertEqual(self.reactions(), [self.seen(10), self.seen(11)])
        photo, forwarded = self.relayed_texts()
        self.assertIn('| photo |', photo)
        self.assertTrue(forwarded.startswith('[forwarded message | from: Sam'))
        self.assertEqual(self.store.last_accepted(), 11)

    def test_a_city_command_gets_no_reaction(self):
        command = dict(name='deploy', description='Deploy', handler='/bin/true', menu=True,
                       callback_prefix='deploy:', timeout=10)
        self.bridge = b.Bridge(dict(self.config, commands=(command,),
                                    reactions=dict(b.REACTIONS_DEFAULTS, on_accept=True)),
                               self.store, 'SECRET', 'CAPABILITY', request=self.transport)
        update = self.update(uid=10)
        update['message']['text'] = '/deploy main'
        with patch.object(self.bridge, 'run_command') as run_command:
            self.bridge.drain([update])
        run_command.assert_called_once()
        self.assertEqual(self.store.offset(), 11)
        self.assertEqual(self.reactions(), [])
        self.assertIsNone(self.store.last_accepted())

    def test_an_ask_answer_gets_no_reaction(self):
        ask_id = b.Bridge._ask_id_for_key('ask-1')
        request = dict(self.publish(key='ask-1', text='Ship it?'),
                       keyboard=[[dict(text='Yes', callback_data=f'ask:{ask_id}:yes')]])
        self.assertTrue(self.bridge.publish(request)['delivered'])
        press = dict(update_id=41, callback_query=dict(
            id='query-41', data=f'ask:{ask_id}:yes', chat_instance='x', **{'from': dict(id=123, is_bot=False)},
            message=dict(message_id=77, chat=dict(id=123, type='private'))))
        self.bridge.drain([press])
        self.assertTrue(self.store.get_ask(ask_id)['relayed'])
        self.assertEqual(self.reactions(), [])
        self.assertIsNone(self.store.last_accepted())

    def test_without_on_accept_no_reaction_call_at_all(self):
        self.bridge = self.make_bridge()
        self.bridge.drain([self.update(uid=10), self.update(uid=11)])
        self.assertEqual(self.reactions(), [])
        self.assertIsNone(self.bridge.reaction_worker)
        # The message is still recorded, so `react` can target it.
        self.assertEqual(self.store.last_accepted(), 11)

    def test_a_reaction_failure_never_retains_the_offset(self):
        self.reaction_answer = REACTION_INVALID
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            self.bridge.drain([self.update(uid=10)])
            self.assertEqual(self.store.offset(), 11)
            self.assertEqual(self.reactions(), [self.seen(10)])
        self.assertIn('reaction not set (msg 10): telegram_rejected_not_sent_400', log.getvalue())
        self.assertNotIn('hello', log.getvalue())
        with patch.object(self.store, 'record_accepted', side_effect=sqlite3.OperationalError('locked')), \
                patch('sys.stderr', new_callable=io.StringIO) as log:
            self.bridge.drain([self.update(uid=11)])
        self.assertEqual(self.store.offset(), 12)
        self.assertIn('accepted message not recorded (msg 11)', log.getvalue())

    def test_a_worker_that_cannot_start_only_logs_and_the_cursor_advances(self):
        with patch.object(b.threading.Thread, 'start', side_effect=RuntimeError("can't start new thread")), \
                patch('sys.stderr', new_callable=io.StringIO) as log:
            self.bridge.drain([self.update(uid=10)])
        self.assertEqual(self.store.offset(), 11)
        self.assertEqual(len(self.relayed_texts()), 1)
        self.assertEqual(self.store.last_accepted(), 10)
        self.assertIn('reaction not set (msg 10): reaction_worker_unavailable', log.getvalue())
        self.assertIsNone(self.bridge.reaction_worker)
        # The next accepted message starts the worker after all.
        self.bridge.drain([self.update(uid=11)])
        self.assertEqual(self.reactions(), [self.seen(11)])

    def done(self, mid):
        return dict(chat_id=123, message_id=mid, reaction=[dict(type='emoji', emoji=OK_HAND)])

    def test_a_done_set_before_the_seen_ran_is_never_replaced(self):
        cli = self.make_bridge()  # a react process: its own Bridge on the same ledger
        b._react(cli, b._build_parser().parse_args(['react', '--reaction', 'done', '--message-id', '10']))
        self.bridge.drain([self.update(uid=10)])
        self.bridge.inbound(self.update(uid=10))
        self.assertEqual(self.reactions(), [self.done(10)])
        self.assertEqual(self.store.last_accepted(), 10)

    def test_a_done_waits_for_a_seen_in_flight_and_lands_after_it(self):
        entered, release, finished = threading.Event(), threading.Event(), []
        transport = self.transport

        def blocking(url, body, *args, **kwargs):
            if url.endswith('/setMessageReaction') and not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(10))
            answer = transport(url, body, *args, **kwargs)
            if url.endswith('/setMessageReaction'):
                finished.append(body['reaction'][0]['emoji'])
            return answer
        self.bridge.request = blocking
        cli = self.make_bridge()  # its own publish lock, like a separate process
        cli.request = blocking
        self.bridge.drain([self.update(uid=10)])
        self.assertTrue(entered.wait(10))
        self.assertEqual(self.store.offset(), 11)
        args = b._build_parser().parse_args(['react', '--reaction', 'done'])
        react = threading.Thread(target=b._react, args=(cli, args))
        react.start()
        react.join(0.5)
        self.assertTrue(react.is_alive())  # held by the reaction lock, not sent
        release.set()
        react.join(10)
        self.assertFalse(react.is_alive())
        self.assertEqual(self.reactions(), [self.seen(10), self.done(10)])
        self.assertEqual(finished, [EYES, OK_HAND])

    def test_an_older_replay_never_moves_the_last_accepted_message_back(self):
        self.bridge = self.make_bridge()
        self.bridge.drain([self.update(uid=10), self.update(uid=11)])
        self.bridge.inbound(self.update(uid=10))
        self.assertEqual(self.store.last_accepted(), 11)


class ReactCommandTests(BridgeTestCase):
    """`react` through _react against the fake Telegram."""

    def setUp(self):
        super().setUp()
        self.bridge = b.Bridge(dict(self.config, reactions=dict(b.REACTIONS_DEFAULTS, done='\U0001F4AF')),
                               self.store, 'SECRET', 'CAPABILITY', request=self.transport, sleep=lambda seconds: None)
        self.answer = (200, dict(ok=True, result=True))

    def transport(self, url, body, *args, **kwargs):
        self.calls.append((url, body))
        return self.answer

    def react(self, *argv):
        args = b._build_parser().parse_args(['react', *argv])
        return b._react(self.bridge, args)

    def bodies(self):
        return [body for _, body in self.calls]

    def test_done_targets_the_last_accepted_message_and_message_id_overrides(self):
        self.store.record_accepted(41, '2026-10-27T07:00:00+00:00')
        self.assertEqual(self.react('--reaction', 'done'), 41)
        self.assertEqual(self.react('--reaction', 'seen', '--message-id', '7'), 7)
        self.assertEqual(self.react('--reaction', '\U0001F44D'), 41)
        self.assertEqual(self.bodies(), [
            dict(chat_id=123, message_id=41, reaction=[dict(type='emoji', emoji='\U0001F4AF')]),
            dict(chat_id=123, message_id=7, reaction=[dict(type='emoji', emoji=EYES)]),
            dict(chat_id=123, message_id=41, reaction=[dict(type='emoji', emoji='\U0001F44D')]),
        ])
        self.assertTrue(all(url.endswith('/setMessageReaction') for url, _ in self.calls))

    def test_none_sends_an_empty_list(self):
        self.react('--reaction', 'none', '--message-id', '9')
        self.assertEqual(self.bodies(), [dict(chat_id=123, message_id=9, reaction=[])])

    def test_with_nothing_accepted_it_refuses_before_telegram(self):
        with self.assertRaisesRegex(b.TransportError, '^no accepted message to react to$'):
            self.react('--reaction', 'done')
        self.assertEqual(self.calls, [])

    def test_two_calls_send_twice_under_their_own_keys(self):
        self.store.record_accepted(41, '2026-10-27T07:00:00+00:00')
        self.react('--reaction', 'done')
        self.react('--reaction', 'done')
        self.assertEqual(len(self.calls), 2)
        with closing(sqlite3.connect(self.path)) as db:
            keys = [key for (key,) in db.execute('SELECT key FROM deliveries')]
        self.assertEqual(len(set(keys)), 2)
        self.assertTrue(all(key.startswith('reaction:41:') for key in keys))

    def test_an_earlier_ambiguous_call_never_blocks_the_next(self):
        self.store.record_accepted(41, '2026-10-27T07:00:00+00:00')
        self.answer = b.TransportError('timeout')

        def transport(url, body, *args, **kwargs):
            self.calls.append((url, body))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
        self.bridge.request = transport
        with patch('sys.stderr', new_callable=io.StringIO), \
                self.assertRaisesRegex(b.TransportError, 'ambiguous_delivery_requires_manual_reconciliation'):
            self.react('--reaction', 'done')
        self.answer = (200, dict(ok=True, result=True))
        self.assertEqual(self.react('--reaction', 'done'), 41)
        self.assertEqual(len(self.calls), 2)

    def test_a_rejection_names_its_reason_and_prints_the_receipt_line(self):
        self.answer = REACTION_INVALID
        with patch('sys.stderr', new_callable=io.StringIO) as log, \
                self.assertRaisesRegex(b.TransportError, '^Telegram reaction not set: telegram_rejected_not_sent_400$'):
            self.react('--reaction', '\U0001F4A9', '--message-id', '5')
        self.assertIn('"reason": "telegram_rejected_not_sent_400"', log.getvalue())

    def test_not_sent_gives_the_not_sent_message(self):
        def transport(url, body, *args, **kwargs):
            self.calls.append((url, body))
            raise b.RequestNotSent('network request not sent', 7, 'connect')
        self.bridge.request = transport
        with patch('sys.stderr', new_callable=io.StringIO), \
                self.assertRaisesRegex(b.TransportError, '^' + b.re.escape(b.NOT_SENT_MESSAGE) + '$'):
            self.react('--reaction', 'done', '--message-id', '5')
        self.assertEqual(len(self.calls), b.SEND_ATTEMPTS)

    def test_a_bad_reaction_or_message_id_refuses_before_telegram(self):
        for value in ('ok', '\U0001F44D \U0001F44D', '', '\U0001F44D' * 17):
            with self.subTest(value=value), self.assertRaisesRegex(b.TransportError, 'must be seen, done, none or one emoji'):
                self.react('--reaction', value, '--message-id', '5')
        parser = b._build_parser()
        for value in ('0', '-1', 'x', '1.5', '99999999999999999999'):
            with self.subTest(message_id=value), patch('sys.stderr', new_callable=io.StringIO), \
                    self.assertRaises(SystemExit):
                parser.parse_args(['react', '--reaction', 'done', '--message-id', value])
        self.assertEqual(self.calls, [])

    def test_react_flags_belong_to_react(self):
        parser = b._build_parser()
        for argv in (['send', '--reaction', 'done'], ['reply-current', '--message-id', '5']):
            args = parser.parse_args(argv)
            with self.subTest(argv=argv), patch('sys.stderr', new_callable=io.StringIO), \
                    self.assertRaises(SystemExit):
                b._validate_attachment_flags(parser, args)


class ReactCliTests(unittest.TestCase):
    """bridge.py react and gc telegram react as processes, curl faked on PATH."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.settings = write_settings(self.root)
        (self.root / 'state').mkdir(mode=0o700)
        self.store = b.Store(self.root / 'state' / 'telegram-bridge.sqlite3', 6)

    def fake_curl(self, answer, status=200):
        """A curl that answers `answer` with HTTP `status` and keeps each config."""
        bin_dir = self.root / 'bin'
        write_fake_curl(bin_dir, f'#!{sys.executable}\nimport sys, uuid\nfrom pathlib import Path\n'
                        f'Path({str(self.root)!r}, "curl-" + uuid.uuid4().hex).write_text(sys.stdin.read())\n'
                        f'print({json.dumps(answer)!r})\n'
                        f'print("{status} 0.010000 0.020000 0", end="")\n')
        env = {k: v for k, v in os.environ.items() if not k.startswith('GC_')}
        env.update(PATH=f'{bin_dir}:{env["PATH"]}', TELEGRAM_BRIDGE_SETTINGS=str(self.settings))
        return env

    def sent(self):
        bodies = []
        for path in self.root.glob('curl-*'):
            config = dict(line.split(' = ', 1) for line in path.read_text().splitlines() if ' = ' in line)
            self.assertTrue(json.loads(config['url']).endswith('/setMessageReaction'))
            bodies.append(json.loads(json.loads(config['data'])))
        return bodies

    def test_gc_telegram_react_forwards_its_arguments(self):
        self.store.record_accepted(41, '2026-10-27T07:00:00+00:00')
        env = self.fake_curl(dict(ok=True, result=True))
        result = subprocess.run(['sh', str(PACK / 'commands' / 'react' / 'run.sh'), '--reaction', 'done'],
                                env=env, cwd=self.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'Telegram reaction set; message ID 41\n')
        self.assertEqual(self.sent(), [dict(chat_id=123, message_id=41,
                                            reaction=[dict(type='emoji', emoji=OK_HAND)])])

    def test_a_400_exits_1_with_its_reason(self):
        env = self.fake_curl(dict(ok=False, error_code=400, description='Bad Request: REACTION_INVALID'), status=400)
        result = subprocess.run([sys.executable, '-B', str(PACK / 'bridge' / 'bridge.py'), '--settings',
                                 str(self.settings), 'react', '--reaction', '\U0001F4A9', '--message-id', '5'],
                                env=env, cwd=self.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Telegram reaction not set: telegram_rejected_not_sent_400', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_react_without_a_reaction_is_a_usage_error(self):
        with patch.object(b.subprocess, 'run', side_effect=AssertionError('nothing may be sent')), \
                patch('sys.stderr', new_callable=io.StringIO) as err, self.assertRaises(SystemExit) as exit_:
            run_main(self, 'bridge.py', '--settings', str(self.settings), 'react')
        self.assertEqual(exit_.exception.code, 2)
        self.assertIn('react requires --reaction', err.getvalue())


if __name__ == '__main__':
    unittest.main()
