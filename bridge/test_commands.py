"""City commands ([[command]]): settings, dispatch to an executable or a
loopback URL handler, callback routing, the handler contract and its failure
modes, the delivery ledger, set-commands, and the served-agent setting. Fake
Telegram and fake GC; temp files only; no network beyond a loopback fixture."""
import contextlib
import datetime
import http.server
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
from test_bridge import MENU, write_settings  # noqa: E402

INPUT_KEYS = {'kind', 'command', 'args', 'update_id', 'message_id', 'callback_data', 'callback_message_id',
              'received_at', 'user_display_name'}
HANDLER_ENV = {'PATH', 'HOME', 'LANG', 'TZ', 'TELEGRAM_BRIDGE_COMMAND'}
MARKER = 'HANDLER-OUTPUT-MARKER'


def handler_script(directory, body, name='handler.py'):
    """An executable Python handler in `directory` that appends what it got
    (stdin, environment, cwd, argv) to record.jsonl beside itself, prints a
    marker on stderr, then runs `body`."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"""#!{sys.executable}
import json, os, sys
data = sys.stdin.read()
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, 'record.jsonl'), 'a') as stream:
    stream.write(json.dumps(dict(stdin=data, env=dict(os.environ), cwd=os.getcwd(), argv=sys.argv)) + '\\n')
sys.stderr.write({MARKER!r} + '\\n')
sys.stderr.flush()
{body}
""")
    path.chmod(0o700)
    return path


def answer(*actions):
    return 'print(' + repr(json.dumps(dict(actions=list(actions)))) + ')'


class FakeTelegram:
    """Records every Telegram and GC call; answers like the real services."""

    def __init__(self, agent='mayor'):
        self.calls, self.next_id, self.agent, self.answers = [], 500, agent, {}

    def __call__(self, url, body, *args, **kwargs):
        method = url.rsplit('/', 1)[1]
        self.calls.append((method, body))
        if method in self.answers:
            result = self.answers[method]
            return result(body) if callable(result) else result
        if method == 'sendMessage':
            self.next_id += 1
            return 200, dict(ok=True, result=dict(message_id=self.next_id, chat=dict(id=123)))
        if method == 'editMessageText':
            return 200, dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
        if method == 'answerCallbackQuery':
            return 200, dict(ok=True, result=True)
        if method == 'inbound':
            message = body['message']
            return 200, dict(TargetAgentName=self.agent, TargetSessionID='', TranscriptEntry=dict(
                ID='entry', Sequence=1, Kind='inbound', Conversation=message['conversation'],
                ProviderMessageID=message['provider_message_id'], Actor=message['actor']))
        raise AssertionError('unexpected call ' + method)

    def methods(self):
        return [method for method, _ in self.calls]

    def of(self, method):
        return [body for name, body in self.calls if name == method]


class CommandCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.handlers = self.root / 'handlers'
        self.store = b.Store(self.root / 'state.sqlite3', 10)
        self.telegram = FakeTelegram()
        self.handler = handler_script(self.handlers, answer(dict(type='send', text='ok')))
        self.env = self.enterContext(patch.dict(os.environ, {'LANG': 'C.UTF-8', 'GC_CITY': '/srv/city',
                                                             'GC_SESSION_ID': 'mayor', 'TELEGRAM_BOT_TOKEN': 'SECRET'}))
        self.bridge = self.make_bridge()

    def make_bridge(self, commands=None, **config):
        if commands is None:
            commands = (self.command(),)
        config = dict(dict(bot_id=999, user_id=123, chat_id=123, account_id='test_bot', city='test-city',
                           user_display_name='Pat', timezone='Asia/Tokyo', menu=MENU,
                           commands=commands), **config)
        return b.Bridge(config, self.store, 'SECRET', 'CAPABILITY', request=self.telegram, state_dir=self.root)

    def command(self, name='deploy', **fields):
        entry = dict(name=name, description='Deploy a branch', menu=True, callback_prefix=name + ':', timeout=5)
        if 'url' not in fields:
            entry['handler'] = str(self.handler)
        entry.update(fields)
        return entry

    def set_handler(self, body):
        handler_script(self.handlers, body)

    def message(self, text, uid=40, sender=123, chat=123):
        return dict(update_id=uid, message=dict(message_id=uid + 1000, text=text, chat=dict(id=chat, type='private'),
                                                **{'from': dict(id=sender, is_bot=False)}))

    def callback(self, data, uid=41, sender=123, chat=123, is_bot=False, message_id=501):
        return dict(update_id=uid, callback_query=dict(
            id=f'query-{uid}', data=data, chat_instance='x', **{'from': dict(id=sender, is_bot=is_bot)},
            message=dict(message_id=message_id, chat=dict(id=chat, type='private'))))

    def drain(self, *updates):
        with patch('sys.stderr', new_callable=io.StringIO) as err:
            self.bridge.drain(list(updates))
        return err.getvalue()

    def records(self):
        path = self.handlers / 'record.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def inputs(self):
        return [json.loads(record['stdin']) for record in self.records()]

    def ledger(self):
        with contextlib.closing(sqlite3.connect(self.store.path)) as db:
            rows = db.execute('SELECT key, receipt FROM deliveries ORDER BY key').fetchall()
        return {key: json.loads(receipt) for key, receipt in rows}


class SettingsCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.handler = handler_script(self.root / 'handlers', 'pass')

    def settings(self, tables='', **replace):
        path = write_settings(self.root, **replace)
        path.write_text(path.read_text() + tables)
        return path

    def entry(self, name='deploy', **fields):
        fields.setdefault('description', 'Deploy a branch')
        if 'url' not in fields and 'handler' not in fields:
            fields['handler'] = str(self.handler)
        return f'\n[[command]]\nname = "{name}"\n' + ''.join(f'{k} = {json.dumps(v)}\n' for k, v in fields.items())

    def refused(self, pattern, tables='', **replace):
        with self.assertRaisesRegex(b.SettingsError, pattern):
            b.load_settings(self.settings(tables, **replace))


class CommandSettingsTests(SettingsCase):
    def test_an_entry_gets_its_defaults_and_reaches_the_config(self):
        settings = b.load_settings(self.settings(
            self.entry() + self.entry('report', url='http://127.0.0.1:8090/report', menu=False,
                                      callback_prefix='rep|', timeout=30)
            + self.entry('six', url='http://[::1]:9/x'), handler_write_paths=f'["{self.root / "out"}"]'))
        self.assertEqual(settings.config['commands'], (
            dict(name='deploy', description='Deploy a branch', menu=True, timeout=10, handler=str(self.handler),
                 callback_prefix='deploy:'),
            dict(name='report', description='Deploy a branch', menu=False, timeout=30,
                 url='http://127.0.0.1:8090/report', callback_prefix='rep|'),
            dict(name='six', description='Deploy a branch', menu=True, timeout=10, url='http://[::1]:9/x',
                 callback_prefix='six:')))
        self.assertEqual(settings.handler_write_paths, (self.root / 'out',))
        self.assertEqual(settings.agent, 'mayor')

    def test_a_command_timeout_of_120_loads(self):
        settings = b.load_settings(self.settings(self.entry(timeout=120)))
        self.assertEqual(settings.config['commands'][0]['timeout'], 120)

    def test_without_commands_the_config_is_unchanged(self):
        settings = b.load_settings(self.settings())
        self.assertNotIn('commands', settings.config)
        self.assertNotIn('agent', settings.config)
        self.assertEqual(settings.handler_write_paths, ())

    def test_each_bad_entry_is_refused_by_name(self):
        executable_dir = self.root / 'dir-handler'
        executable_dir.mkdir()
        plain = self.root / 'not-executable'
        plain.write_text('#!/bin/sh\n')
        plain.chmod(0o600)
        shared = self.root / 'group-writable'
        shared.write_text('#!/bin/sh\n')
        shared.chmod(0o770)
        long_text = 'x' * 257
        cases = [
            (r'unknown key shell in command entry 1 \(deploy\)', self.entry(shell=True)),
            (r'command entry 2 \(deploy\) repeats a command name', self.entry() + self.entry()),
            (r'command entry 1 \(start\) clashes with /start', self.entry('start')),
            (r'command entry 1 \(quota\) clashes with /quota', self.entry('quota')),
            (r'command entry 1 \(status\) clashes with /status', self.entry('status')),
            (r'command entry 1 name is missing or malformed', self.entry('Deploy')),
            (r'command entry 1 \(deploy\) needs exactly one of handler and url',
             self.entry(url='http://127.0.0.1:8090/x', handler=str(self.handler))),
            (r'command entry 1 \(deploy\) needs exactly one of handler and url',
             '\n[[command]]\nname = "deploy"\ndescription = "d"\n'),
            (r'command entry 1 \(deploy\) handler .* does not exist', self.entry(handler=str(self.root / 'missing'))),
            (r'command entry 1 \(deploy\) handler must be an absolute', self.entry(handler='handlers/handler.py')),
            (r'command entry 1 \(deploy\) handler .* must be an executable regular file', self.entry(handler=str(plain))),
            (r'command entry 1 \(deploy\) handler .* must be an executable regular file',
             self.entry(handler=str(executable_dir))),
            (r'command entry 1 \(deploy\) handler .* must not be writable by group or others',
             self.entry(handler=str(shared))),
            (r'command entry 1 \(deploy\) description is missing', self.entry(description=long_text)),
            (r'command entry 1 \(deploy\) description is missing', self.entry(description='')),
            (r'command entry 1 \(deploy\) timeout must be an integer from 1 to 3600', self.entry(timeout=0)),
            (r'command entry 1 \(deploy\) timeout must be an integer from 1 to 3600', self.entry(timeout=3601)),
            (r'command entry 1 \(deploy\) menu must be true or false', self.entry(menu='yes')),
            (r'command entry 1 \(deploy\) callback_prefix must be', self.entry(callback_prefix='')),
            (r'command entry 1 \(deploy\) callback_prefix must be', self.entry(callback_prefix='a b')),
            (r'command entry 1 \(deploy\) callback_prefix must be', self.entry(callback_prefix='x' * 65)),
            (r'command entry 2 \(other\) callback_prefix overlaps the one of /deploy',
             self.entry() + self.entry('other', callback_prefix='deploy:')),
            (r'command entry 2 \(other\) callback_prefix overlaps the one of /deploy',
             self.entry() + self.entry('other', callback_prefix='deploy:x')),
            (r'command entry 2 \(other\) callback_prefix overlaps the one of /deploy',
             self.entry() + self.entry('other', callback_prefix='dep')),
        ]
        for url in ('http://localhost:8090/x', 'https://127.0.0.1:8090/x', 'http://127.0.0.2:8090/x',
                    'http://127.0.0.1/x', 'http://127.0.0.1:8090', 'http://127.0.0.1:0/x', 'http://127.0.0.1:70000/x',
                    'http://127.0.0.1:8090/x?y=1', 'http://user@127.0.0.1:8090/x', 'http://[::2]:8090/x',
                    'http://10.0.0.1:8090/x', 'http://127.0.0.1:8090/x#y'):
            cases.append((r'command entry 1 \(deploy\) url must be http://127.0.0.1', self.entry(url=url, handler=None)
                          .replace('handler = null\n', '')))
        for pattern, tables in cases:
            with self.subTest(pattern=pattern, tables=tables):
                self.refused(pattern, tables)

    def test_a_handler_in_a_group_writable_directory_is_refused_and_in_a_private_one_loads(self):
        shared = self.root / 'shared-handlers'
        shared.mkdir()
        shared.chmod(0o775)
        shared_handler = handler_script(shared, 'pass', name='shared.py')
        shared_handler.chmod(0o755)
        self.refused(r'command entry 1 \(deploy\) handler .* must be owned by this user or root, '
                     r'in a directory only they can write', self.entry(handler=str(shared_handler)))
        private = self.root / 'private-handlers'
        private.mkdir()
        private.chmod(0o700)
        private_handler = handler_script(private, 'pass', name='private.py')
        private_handler.chmod(0o755)
        settings = b.load_settings(self.settings(self.entry(handler=str(private_handler))))
        self.assertEqual(settings.config['commands'][0]['handler'], str(private_handler))

    def test_a_handler_owned_by_another_user_is_refused(self):
        real_stat = os.stat

        def other_owner(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if os.fspath(path) == str(self.handler):
                return os.stat_result((result.st_mode, result.st_ino, result.st_dev, result.st_nlink,
                                       result.st_uid + 1000, result.st_gid, result.st_size,
                                       int(result.st_atime), int(result.st_mtime), int(result.st_ctime)))
            return result

        with patch('os.stat', other_owner):
            self.refused(r'command entry 1 \(deploy\) handler .* must be owned by this user or root, '
                         r'in a directory only they can write', self.entry())

    def test_a_symlinked_handler_needs_both_directories_safe(self):
        safe = self.root / 'safe-target'
        safe.mkdir()
        safe.chmod(0o700)
        target = handler_script(safe, 'pass', name='target.py')
        target.chmod(0o755)
        shared = self.root / 'shared-link'
        shared.mkdir()
        shared.chmod(0o775)
        link_in_shared = shared / 'link.py'
        link_in_shared.symlink_to(target)
        self.refused(r'must be owned by this user or root, in a directory only they can write',
                     self.entry(handler=str(link_in_shared)))
        shared_target = handler_script(shared, 'pass', name='shared-target.py')
        shared_target.chmod(0o755)
        link_in_safe = safe / 'link.py'
        link_in_safe.symlink_to(shared_target)
        self.refused(r'must be owned by this user or root, in a directory only they can write',
                     self.entry(handler=str(link_in_safe)))

    def test_more_than_100_menu_entries_in_total_are_refused(self):
        many = ''.join(self.entry(f'c{i}', callback_prefix=f'c{i}|') for i in range(97))
        b.load_settings(self.settings(many))  # 3 [[menu]] + 97 = 100
        self.refused(r'101 menu entries', many + self.entry('one_more', callback_prefix='more|'))
        # Hidden commands do not count against the menu.
        b.load_settings(self.settings(many + self.entry('hidden', callback_prefix='hid|', menu=False)))

    def test_handler_write_paths_must_be_safe_absolute_directories(self):
        for value, pattern in (('"/srv/out"', 'must be a list'), ('["relative/out"]', 'entry 1 must be an absolute'),
                               ('["/srv/a b"]', 'entry 1 must be an absolute'), ('["/"]', 'entry 1 must be a distinct'),
                               ('["/srv/out", "/srv/out"]', 'entry 2 must be a distinct'),
                               ('[' + ', '.join(f'"/srv/o{i}"' for i in range(257)) + ']', 'must be a list of at most 256')):
            with self.subTest(value=value):
                self.refused('handler_write_paths ' + pattern, handler_write_paths=value)

    def test_handler_write_paths_accepts_256_entries(self):
        paths = '[' + ', '.join(f'"/srv/o{i}"' for i in range(256)) + ']'
        settings = b.load_settings(self.settings(handler_write_paths=paths))
        self.assertEqual(len(settings.handler_write_paths), 256)

    def test_command_must_be_a_list_of_tables(self):
        path = self.settings()
        text = path.read_text()
        path.write_text('command = "deploy"\n' + text)
        with self.assertRaisesRegex(b.SettingsError, 'command must be a list of at most 100'):
            b.load_settings(path)

    def test_agent_setting(self):
        self.assertEqual(b.load_settings(self.settings(agent='"reviewer"')).config['agent'], 'reviewer')
        self.assertNotIn('agent', b.load_settings(self.settings(agent='"mayor"')).config)
        for bad in ('""', '"two words"', '7'):
            with self.subTest(bad=bad):
                self.refused('agent is missing or malformed', agent=bad)


class CommandUnitTests(SettingsCase):
    def test_render_unit_appends_handler_write_paths_and_names_the_agent(self):
        out, logs = self.root / 'out', self.root / 'logs'
        path = self.settings(self.entry(), handler_write_paths=f'["{out}", "{logs}"]', agent='"reviewer"')
        unit = b.render_unit(b.load_settings(path), bridge_path='/srv/bridge.py')
        self.assertIn(f'ReadWritePaths={self.root / "state"} {self.root / "private"} {out} {logs}\n', unit)
        self.assertIn(f'ReadOnlyPaths=-{path} -{self.root / "private" / "telegram-bot-token"} '
                      f'-{self.root / "private" / "telegram-callback-secret"}\n', unit)
        self.assertIn('Description=Private Telegram transport for the test-city reviewer\n', unit)
        default = b.render_unit(b.load_settings(self.settings()), bridge_path='/srv/bridge.py')
        self.assertIn(f'ReadWritePaths={self.root / "state"} {self.root / "private"}\n', default)
        self.assertIn('Description=Private Telegram transport for the test-city mayor\n', default)

    def test_check_install_reports_handlers_and_write_paths(self):
        out = self.root / 'out'
        path = self.settings(self.entry() + self.entry('report', url='http://127.0.0.1:8090/r'),
                             handler_write_paths=f'["{out}"]')
        settings = b.load_settings(path)
        lines = dict((line, ok) for ok, line in b.install_problems(settings, which=lambda n: '/usr/bin/' + n))
        self.assertTrue(lines[f"command /deploy handler {self.handler} (runs in the unit's sandbox)"])
        self.assertTrue(lines["command /report handler http://127.0.0.1:8090/r (loopback; outside the unit's sandbox)"])
        self.assertFalse(lines[f'handler write path {out}'])
        out.mkdir()
        self.handler.chmod(0o600)
        lines = dict((line, ok) for ok, line in b.install_problems(settings, which=lambda n: '/usr/bin/' + n))
        self.assertTrue(lines[f'handler write path {out}'])
        self.assertFalse(lines[f"command /deploy handler {self.handler} (runs in the unit's sandbox)"])


class DispatchTests(CommandCase):
    def test_a_command_runs_its_handler_with_the_exact_input_and_never_reaches_gc(self):
        before = datetime.datetime.now(datetime.timezone.utc)
        log = self.drain(self.message('/deploy'))
        [given] = self.inputs()
        received = datetime.datetime.fromisoformat(given.pop('received_at'))
        self.assertLessEqual(before, received)
        self.assertEqual(given, dict(kind='command', command='deploy', args='', update_id=40, message_id=1040,
                                     callback_data=None, callback_message_id=None, user_display_name='Pat'))
        self.assertEqual(self.telegram.methods(), ['sendMessage'])
        self.assertEqual(self.telegram.of('sendMessage'), [dict(chat_id=123, text='ok',
                                                                link_preview_options={'is_disabled': True})])
        self.assertEqual(self.store.offset(), 41)
        self.assertEqual(log, 'command deploy handled (update 40): 1 action(s)\n')

    def test_bot_suffix_and_arguments(self):
        self.drain(self.message('/deploy@test_bot', 40), self.message('/deploy feature/x  now', 42),
                   self.message('/deploy@Test_Bot two words', 44))
        self.assertEqual([(i['args'], i['message_id']) for i in self.inputs()],
                         [('', 1040), ('feature/x  now', 1042), ('two words', 1044)])
        self.assertNotIn('inbound', self.telegram.methods())

    def test_anything_else_still_reaches_the_agent_as_text(self):
        for uid, text in enumerate(('/deploy@other_bot', '/deployx', '/deploy\nnext line', 'deploy', '/Deploy',
                                    ' /deploy', '/status', '/quota'), 50):
            self.drain(self.message(text, uid * 2))
        self.assertEqual(self.records(), [])
        self.assertEqual([body['message']['text'] for body in self.telegram.of('inbound')],
                         ['/deploy@other_bot', '/deployx', '/deploy\nnext line', 'deploy', '/Deploy', ' /deploy',
                          '/status', '/quota'])

    def test_unauthorized_or_forwarded_commands_never_run_a_handler(self):
        self.drain(self.message('/deploy', 40, sender=124), self.message('/deploy', 42, chat=124))
        forwarded = self.message('/deploy', 44)
        forwarded['message']['forward_origin'] = {'type': 'user'}
        self.drain(forwarded)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.telegram.calls, [])

    def test_quota_as_a_city_command_runs_its_handler_and_never_the_builtin(self):
        self.bridge = self.make_bridge(commands=(self.command('quota'),))
        seen = []

        def fake_run(argv, *args, **kwargs):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 1, '', 'no quota')

        with patch.object(b.subprocess, 'run', side_effect=fake_run):
            log = self.drain(self.message('/quota', 40))
        [given] = self.inputs()
        self.assertEqual((given['kind'], given['command'], given['args'], given['update_id']),
                         ('command', 'quota', '', 40))
        self.assertEqual(self.telegram.methods(), ['sendMessage'])
        self.assertNotIn('inbound', self.telegram.methods())
        self.assertEqual(list(self.ledger()), ['command:40:0'])
        self.assertEqual(seen, [])  # the handler runs via Popen; nothing shells out
        self.assertEqual(log, 'command quota handled (update 40): 1 action(s)\n')

    def test_the_handler_environment_is_fixed_and_carries_no_secret(self):
        self.drain(self.message('/deploy'))
        [record] = self.records()
        self.assertEqual(set(record['env']), HANDLER_ENV)
        self.assertEqual(record['env']['PATH'], '/usr/local/bin:/usr/bin:/bin')
        self.assertEqual(record['env']['TELEGRAM_BRIDGE_COMMAND'], 'deploy')
        self.assertEqual(record['env']['TZ'], 'Asia/Tokyo')
        self.assertEqual(record['env']['LANG'], 'C.UTF-8')
        self.assertEqual(record['env']['HOME'], os.environ['HOME'])
        self.assertEqual(record['cwd'], str(self.handlers))
        self.assertEqual(record['argv'], [str(self.handler)])
        for secret in ('SECRET', 'CAPABILITY', '/srv/city'):
            self.assertNotIn(secret, json.dumps(record))

    def test_a_url_handler_gets_the_same_json_by_post(self):
        server = LoopbackHandler(self)
        server.reply = json.dumps(dict(actions=[dict(type='send', text='from url')])).encode()
        self.bridge = self.make_bridge((self.command(url=server.url),))
        self.drain(self.message('/deploy go'))
        [(path, headers, body)] = server.requests
        self.assertEqual(path, '/hook')
        self.assertEqual(headers['Content-Type'], 'application/json')
        given = json.loads(body)
        self.assertEqual(set(given), INPUT_KEYS)
        self.assertEqual((given['kind'], given['command'], given['args'], given['update_id']), ('command', 'deploy', 'go', 40))
        self.assertEqual([m['text'] for m in self.telegram.of('sendMessage')], ['from url'])
        self.assertNotIn('SECRET', body.decode())


class LoopbackHandler:
    """A loopback HTTP fixture that records POSTs and answers with `reply`."""

    def __init__(self, case, status=200, delay=0, location=None):
        self.requests, self.reply, self.status, self.delay = [], b'{"actions": []}', status, delay
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                fixture.requests.append((self.path, dict(self.headers), body))
                time.sleep(fixture.delay)
                self.send_response(fixture.status)
                if location:
                    self.send_header('Location', location)
                self.send_header('Content-Length', str(len(fixture.reply)))
                self.end_headers()
                try:
                    self.wfile.write(fixture.reply)
                except OSError:
                    pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        case.addCleanup(self.server.server_close)
        case.addCleanup(self.server.shutdown)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/hook'


class CallbackTests(CommandCase):
    def test_a_button_press_under_the_prefix_runs_the_handler_and_is_answered(self):
        self.set_handler(answer(dict(type='edit', message_id=501, text='Deployed', keyboard=[[dict(
            text='Again', callback_data='deploy:again')]]), dict(type='answer_callback', text='Done', alert=True)))
        self.drain(self.callback('deploy:yes'))
        [given] = self.inputs()
        given.pop('received_at')
        self.assertEqual(given, dict(kind='callback', command='deploy', args=None, update_id=41, message_id=None,
                                     callback_data='deploy:yes', callback_message_id=501, user_display_name='Pat'))
        self.assertEqual(self.telegram.calls, [
            ('editMessageText', dict(chat_id=123, text='Deployed', link_preview_options={'is_disabled': True},
                                     message_id=501, reply_markup={'inline_keyboard': [[dict(
                                         text='Again', callback_data='deploy:again')]]})),
            ('answerCallbackQuery', dict(callback_query_id='query-41', text='Done', show_alert=True)),
        ])
        self.assertEqual(self.store.offset(), 42)

    def test_a_handler_that_does_not_answer_gets_an_empty_answer(self):
        self.set_handler(answer(dict(type='send', text='noted')))
        self.drain(self.callback('deploy:yes'))
        self.assertEqual(self.telegram.methods(), ['sendMessage', 'answerCallbackQuery'])
        self.assertEqual(self.telegram.of('answerCallbackQuery'), [dict(callback_query_id='query-41')])

    def test_routing_picks_the_command_whose_prefix_matches(self):
        other = handler_script(self.root / 'other', answer())
        self.bridge = self.make_bridge((self.command(), self.command('report', handler=str(other),
                                                                        callback_prefix='r|')))
        self.drain(self.callback('r|page2', 41), self.callback('deploy:no', 43))
        self.assertEqual([i['callback_data'] for i in self.inputs()], ['deploy:no'])
        [record] = [json.loads(line) for line in (self.root / 'other' / 'record.jsonl').read_text().splitlines()]
        self.assertEqual(json.loads(record['stdin'])['command'], 'report')
        self.assertEqual(record['env']['TELEGRAM_BRIDGE_COMMAND'], 'report')

    def test_unknown_or_unauthorized_callbacks_are_answered_empty_and_dropped(self):
        updates = [self.callback('other:yes', 41), self.callback('deploy:yes', 43, sender=124),
                   self.callback('deploy:yes', 45, chat=124), self.callback('deploy:yes', 47, is_bot=True),
                   self.callback('', 49)]
        no_message = self.callback('deploy:yes', 51)
        del no_message['callback_query']['message']
        log = self.drain(*updates, no_message)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.telegram.of('answerCallbackQuery'),
                         [dict(callback_query_id=f'query-{uid}') for uid in (41, 43, 45, 47, 49, 51)])
        self.assertNotIn('inbound', self.telegram.methods())
        self.assertEqual(log.splitlines(), [
            'callback dropped (update 41): no_registered_prefix', 'callback dropped (update 43): not_authorized',
            'callback dropped (update 45): not_authorized', 'callback dropped (update 47): not_authorized',
            'callback dropped (update 49): no_registered_prefix', 'callback dropped (update 51): not_authorized'])
        self.assertEqual(self.store.offset(), 52)

    def test_a_malformed_callback_is_logged_and_dropped(self):
        update = self.callback('deploy:yes')
        del update['callback_query']['id']
        log = self.drain(update)
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(log, 'callback dropped (update 41): malformed\n')
        self.assertEqual(self.store.offset(), 42)


class ActionTests(CommandCase):
    def test_send_with_html_and_a_keyboard(self):
        self.set_handler(answer(dict(type='send', text='<b>Pick</b> one', format='html', keyboard=[
            [dict(text='Yes', callback_data='deploy:yes'), dict(text='No', callback_data='deploy:no')],
            [dict(text='Später ✓', callback_data='deploy:later')]])))
        self.drain(self.message('/deploy'))
        self.assertEqual(self.telegram.of('sendMessage'), [dict(
            chat_id=123, text='<b>Pick</b> one', parse_mode='HTML', link_preview_options={'is_disabled': True},
            reply_markup={'inline_keyboard': [[dict(text='Yes', callback_data='deploy:yes'),
                                               dict(text='No', callback_data='deploy:no')],
                                              [dict(text='Später ✓', callback_data='deploy:later')]]})])

    def test_html_rejected_by_telegram_is_resent_once_as_plain(self):
        self.set_handler(answer(dict(type='send', text='<b>Pick</b> &amp; go', format='html')))
        replies = iter([(400, dict(ok=False, error_code=400, description="Bad Request: can't parse entities"))])
        def send(body):
            try:
                return next(replies)
            except StopIteration:
                return 200, dict(ok=True, result=dict(message_id=9, chat=dict(id=123)))
        self.telegram.answers['sendMessage'] = send
        self.drain(self.message('/deploy'))
        self.assertEqual([m['text'] for m in self.telegram.of('sendMessage')], ['<b>Pick</b> &amp; go', 'Pick & go'])
        self.assertEqual(self.ledger()['command:40:0']['format_fallback_reason'], 'telegram_html_parse_rejected_400')

    def test_several_actions_run_in_order_under_their_own_keys(self):
        self.set_handler(answer(dict(type='send', text='one'), dict(type='edit', message_id=7, text='two'),
                                dict(type='send', text='three')))
        self.drain(self.message('/deploy'))
        self.assertEqual(self.telegram.methods(), ['sendMessage', 'editMessageText', 'sendMessage'])
        ledger = self.ledger()
        self.assertEqual(sorted(ledger), ['command:40:0', 'command:40:1', 'command:40:2'])
        self.assertTrue(all(receipt['delivered'] for receipt in ledger.values()))

    def test_an_empty_action_list_does_nothing(self):
        self.set_handler(answer())
        self.drain(self.message('/deploy'))
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(self.store.offset(), 41)

    def test_a_telegram_rejection_is_logged_not_retried_and_sends_no_notice(self):
        self.set_handler(answer(dict(type='edit', message_id=7, text='same')))
        self.telegram.answers['editMessageText'] = (400, dict(ok=False, error_code=400,
                                                              description='message is not modified'))
        log = self.drain(self.message('/deploy'))
        self.assertEqual(self.telegram.methods(), ['editMessageText'])
        self.assertIn('command deploy (update 40): action 0 edit not delivered: telegram_rejected_not_sent_400\n', log)
        self.assertNotIn('message is not modified', log)


class FailureTests(CommandCase):
    NOTICE = dict(chat_id=123, text='command deploy failed', link_preview_options={'is_disabled': True})

    def assert_failure(self, body, reason, update=None, url=None):
        self.set_handler(body)
        if url:
            self.bridge = self.make_bridge((self.command(url=url, timeout=1),))
        log = self.drain(update or self.message('/deploy'))
        self.assertEqual(self.telegram.of('sendMessage'), [self.NOTICE])
        uid = (update or {}).get('update_id', 40)
        self.assertEqual(log, f'command deploy failed (update {uid}): {reason}\n')
        self.assertNotIn(MARKER, log)
        self.assertEqual(list(self.ledger())[0], f'command:{uid}:notice')
        self.assertEqual(self.store.offset(), uid + 1)

    def test_32_actions_and_a_full_keyboard_are_accepted(self):
        button = dict(text='a', callback_data='deploy:a')
        keyboard = [[dict(button) for _ in range(8)] for _ in range(12)]
        keyboard.append([dict(button) for _ in range(4)])
        actions = [dict(type='send', text=str(i), keyboard=keyboard) if i == 0
                   else dict(type='send', text=str(i)) for i in range(32)]
        self.set_handler(answer(*actions))
        self.drain(self.message('/deploy'))
        sends = self.telegram.of('sendMessage')
        self.assertEqual(len(sends), 32)
        self.assertEqual(sends[0]['reply_markup'], {'inline_keyboard': keyboard})

    def test_each_bad_answer_is_a_failure_with_its_reason(self):
        many = [dict(type='send', text=str(i)) for i in range(33)]
        cases = [
            (f'print({MARKER!r}); sys.exit(3)', 'exit_status'),
            (f'sys.stdout.write("x" * 70000 + {MARKER!r})', 'output_too_large'),
            (f'print("{{not json {MARKER}")', 'malformed_json'),
            ('print(\'{"actions": [], "actions": []}\')', 'malformed_json'),
            ('print(\'{"actions": [{"type": "send", "text": NaN}]}\')', 'malformed_json'),
            ('sys.stdout.buffer.write(b\'{"actions": ["\\xff"]}\')', 'malformed_json'),
            ('', 'malformed_json'),
            ('print("[]")', 'not_an_actions_object'),
            (f'print(json.dumps(dict(actions=[], note={MARKER!r})))', 'not_an_actions_object'),
            (answer(*many), 'too_many_actions'),
            (answer(dict(type='delete', message_id=1)), 'unknown_action_type'),
            (answer('send'), 'unknown_action_type'),
            (answer(dict(type=['send'], text='x')), 'unknown_action_type'),
            (answer(dict(type='send', text='x', parse_mode='HTML')), 'unknown_action_key'),
            (answer(dict(type='send')), 'unknown_action_key'),
            (answer(dict(type='edit', text='x')), 'unknown_action_key'),
            (answer(dict(type='send', text='x', format='markdown')), 'invalid_text'),
            (answer(dict(type='send', text='   ')), 'invalid_text'),
            (answer(dict(type='send', text='😀' * 2049)), 'invalid_text'),
            (answer(dict(type='send', text=7)), 'invalid_text'),
            (answer(dict(type='send', text='<script>x</script>', format='html')), 'invalid_html'),
            (answer(dict(type='edit', message_id=0, text='x')), 'invalid_message_id'),
            (answer(dict(type='edit', message_id=True, text='x')), 'invalid_message_id'),
            (answer(dict(type='send', text='x', keyboard=[])), 'invalid_keyboard'),
            (answer(dict(type='send', text='x', keyboard=[[]])), 'invalid_keyboard'),
            (answer(dict(type='send', text='x',
                          keyboard=[[dict(text='a', callback_data='deploy:a') for _ in range(9)]])),
             'invalid_keyboard'),
            (answer(dict(type='send', text='x',
                          keyboard=[[dict(text='a', callback_data='deploy:a')] for _ in range(101)])),
             'invalid_keyboard'),
            (answer(dict(type='send', text='x',
                          keyboard=[[dict(text='a', callback_data='deploy:a') for _ in range(101)]])),
             'invalid_keyboard'),
            (answer(dict(type='send', text='x', keyboard=[[dict(text='a', callback_data='deploy:' + 'y' * 58)]])),
             'invalid_keyboard'),
            (answer(dict(type='send', text='x', keyboard=[[dict(text='a', callback_data='deploy:a', url='x')]])),
             'invalid_keyboard'),
            (answer(dict(type='send', text='x', keyboard=[[dict(text='a', callback_data='other:a')]])),
             'foreign_callback_data'),
            (answer(dict(type='answer_callback', text='hi')), 'invalid_answer'),
        ]
        for uid, (body, reason) in enumerate(cases, 30):
            with self.subTest(reason=reason, body=body):
                self.telegram.calls.clear()
                self.store = b.Store(self.root / f'state-{uid}.sqlite3', 10)
                self.bridge = self.make_bridge()
                self.assert_failure(body, reason)

    def test_a_failed_callback_is_answered_empty_after_the_notice(self):
        self.set_handler(answer(dict(type='answer_callback', text='x' * 201)))
        self.drain(self.callback('deploy:yes'))
        self.assertEqual(self.telegram.calls, [('sendMessage', self.NOTICE),
                                               ('answerCallbackQuery', dict(callback_query_id='query-41'))])
        self.set_handler(answer(dict(type='answer_callback'), dict(type='answer_callback')))
        self.telegram.calls.clear()
        log = self.drain(self.callback('deploy:yes', 43))
        self.assertEqual(log, 'command deploy failed (update 43): invalid_answer\n')
        self.assertEqual(self.telegram.methods(), ['sendMessage', 'answerCallbackQuery'])

    def test_a_lone_surrogate_in_any_answer_string_is_a_failure(self):
        # json.dumps escapes the lone surrogate as \ud83d: valid strict JSON
        # that no Telegram text, length count or callback_data can encode.
        lone = 'ok \ud83d'
        button = dict(text='a', callback_data='deploy:a')
        cases = [
            ('send text', answer(dict(type='send', text=lone))),
            ('html text', answer(dict(type='send', text='<b>' + lone + '</b>', format='html'))),
            ('edit text', answer(dict(type='edit', message_id=7, text=lone))),
            ('button text', answer(dict(type='send', text='x', keyboard=[[dict(button, text=lone)]]))),
            ('callback_data', answer(dict(type='send', text='x',
                                          keyboard=[[dict(button, callback_data='deploy:' + lone)]]))),
        ]
        for uid, (name, body) in enumerate(cases, 60):
            with self.subTest(name):
                self.assertIn('\\ud83d', body)
                self.telegram.calls.clear()
                self.store = b.Store(self.root / f'state-{uid}.sqlite3', 10)
                self.bridge = self.make_bridge()
                self.assert_failure(body, 'invalid_unicode')
        with self.subTest('answer text'):
            self.telegram.calls.clear()
            self.store = b.Store(self.root / 'state-answer.sqlite3', 10)
            self.bridge = self.make_bridge()
            self.set_handler(answer(dict(type='answer_callback', text=lone)))
            log = self.drain(self.callback('deploy:yes'))
            self.assertEqual(log, 'command deploy failed (update 41): invalid_unicode\n')
            self.assertEqual(self.telegram.calls, [('sendMessage', self.NOTICE),
                                                   ('answerCallbackQuery', dict(callback_query_id='query-41'))])
            self.assertEqual(self.store.offset(), 42)

    def test_a_handler_that_cannot_start_is_a_failure(self):
        self.bridge = self.make_bridge((self.command(handler=str(self.root / 'gone')),))
        log = self.drain(self.message('/deploy'))
        self.assertEqual(log, 'command deploy failed (update 40): handler_not_started\n')
        self.assertEqual(self.telegram.of('sendMessage'), [self.NOTICE])

    def test_a_timeout_kills_the_whole_process_group(self):
        self.set_handler(f"""import subprocess, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
with open(os.path.join(here, 'child.pid'), 'w') as stream:
    stream.write(str(child.pid))
print({MARKER!r}, flush=True)
time.sleep(60)
""")
        self.bridge = self.make_bridge((self.command(timeout=1),))
        started = time.monotonic()
        log = self.drain(self.message('/deploy'))
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(log, 'command deploy failed (update 40): timeout\n')
        self.assertEqual(self.telegram.of('sendMessage'), [self.NOTICE])
        child = int((self.handlers / 'child.pid').read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.alive(child):
            time.sleep(0.05)
        self.assertFalse(self.alive(child), 'the handler\'s child outlived the timeout')

    @staticmethod
    def alive(pid):
        try:
            state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
        except (OSError, IndexError):
            return False
        return state not in ('Z', 'X')

    def test_a_handler_that_never_reads_its_input_still_works(self):
        self.set_handler(answer(dict(type='send', text='ok')))
        long_args = 'a' * 4000
        self.drain(self.message('/deploy ' + long_args))
        self.assertEqual([m['text'] for m in self.telegram.of('sendMessage')], ['ok'])

    def test_url_failures(self):
        for name, server, reason in (
                ('status', LoopbackHandler(self, status=500), 'http_status'),
                ('redirect', LoopbackHandler(self, status=302, location='http://127.0.0.1:1/x'), 'http_status'),
                ('slow', LoopbackHandler(self, delay=2.5), 'timeout')):
            with self.subTest(name):
                self.telegram.calls.clear()
                self.store = b.Store(self.root / f'state-{name}.sqlite3', 10)
                self.assert_failure(answer(), reason, url=server.url)
        big = LoopbackHandler(self)
        big.reply = b'{"actions": []}' + b' ' * 70000
        self.telegram.calls.clear()
        self.store = b.Store(self.root / 'state-big.sqlite3', 10)
        self.assert_failure(answer(), 'output_too_large', url=big.url)
        closed = LoopbackHandler(self)
        url = closed.url
        closed.server.shutdown()
        closed.server.server_close()
        self.telegram.calls.clear()
        self.store = b.Store(self.root / 'state-closed.sqlite3', 10)
        self.assert_failure(answer(), 'handler_unreachable', url=url)

    def test_the_url_deadline_covers_writing_the_request(self):
        clock = [1000.0]
        seen = []

        class Sock:
            def settimeout(self, value):
                seen.append(('settimeout', value))

        class SlowConnection:
            """Connecting takes `connect_cost` seconds of the fake clock; the
            request is never answered, so only the deadline ends the call."""
            connect_cost = 0.0

            def __init__(self, host, port, timeout):
                seen.append(('init', timeout))
                self.sock = Sock()

            def connect(self):
                clock[0] += self.connect_cost

            def request(self, *args, **kwargs):
                seen.append(('request',))
                clock[0] += 5
                raise TimeoutError

            def getresponse(self):
                raise AssertionError('no request was written')

            def close(self):
                pass

        command = self.command(url='http://127.0.0.1:1/hook', timeout=2)
        self.bridge = self.make_bridge((command,))
        command = self.bridge.commands['deploy']
        with patch.object(b.time, 'monotonic', lambda: clock[0]):
            SlowConnection.connect_cost = 1.5
            with self.assertRaises(self.bridge._HandlerFailure) as caught:
                self.bridge._post_url_handler(command, b'{}', connection=SlowConnection)
            self.assertEqual(caught.exception.args[0], 'timeout')
            # The write gets only what connecting left of the 2 s deadline.
            self.assertEqual(seen, [('init', 2), ('settimeout', 0.5), ('request',)])
            seen.clear()
            SlowConnection.connect_cost = 2.5
            with self.assertRaises(self.bridge._HandlerFailure) as caught:
                self.bridge._post_url_handler(command, b'{}', connection=SlowConnection)
            self.assertEqual(caught.exception.args[0], 'timeout')
            self.assertEqual(seen, [('init', 2)], 'nothing is written once the deadline has passed')

    def test_the_url_handler_ignores_proxy_settings(self):
        server = LoopbackHandler(self)
        server.reply = json.dumps(dict(actions=[dict(type='send', text='direct')])).encode()
        with patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1', 'HTTP_PROXY': 'http://127.0.0.1:1'}):
            self.bridge = self.make_bridge((self.command(url=server.url),))
            self.drain(self.message('/deploy'))
        self.assertEqual([m['text'] for m in self.telegram.of('sendMessage')], ['direct'])


class LedgerTests(CommandCase):
    def test_a_reprocessed_update_never_sends_twice(self):
        self.set_handler(answer(dict(type='send', text='one'), dict(type='send', text='two')))
        self.drain(self.message('/deploy'))
        self.bridge.inbound(self.message('/deploy'))  # the same update again, as after a crash before the cursor
        self.assertEqual([m['text'] for m in self.telegram.of('sendMessage')], ['one', 'two'])
        self.assertEqual(sorted(self.ledger()), ['command:40:0', 'command:40:1'])

    def test_an_unconfirmed_send_stays_ambiguous_and_is_never_resent(self):
        def lost(body):
            raise b.TransportError('network response unconfirmed')
        self.telegram.answers['sendMessage'] = lost
        log = self.drain(self.message('/deploy'))
        self.assertEqual(self.ledger()['command:40:0']['metadata']['reason'],
                         'ambiguous_delivery_requires_manual_reconciliation')
        self.assertIn('action 0 send not delivered: ambiguous_delivery_requires_manual_reconciliation', log)
        del self.telegram.answers['sendMessage']
        self.bridge.inbound(self.message('/deploy'))
        self.assertEqual(len(self.telegram.of('sendMessage')), 1)
        self.assertEqual(self.store.offset(), 41)

    def test_a_different_answer_on_reprocessing_is_not_sent_under_the_used_key(self):
        self.drain(self.message('/deploy'))
        self.set_handler(answer(dict(type='send', text='changed')))
        with patch('sys.stderr', new_callable=io.StringIO) as err:
            self.bridge.inbound(self.message('/deploy'))
        self.assertEqual([m['text'] for m in self.telegram.of('sendMessage')], ['ok'])
        self.assertIn('action 0 send not delivered: idempotency_key_conflict', err.getvalue())

    def test_a_callback_is_answered_once(self):
        self.set_handler(answer(dict(type='answer_callback', text='Done')))
        self.drain(self.callback('deploy:yes'))
        with patch('sys.stderr', new_callable=io.StringIO):
            self.bridge.inbound(self.callback('deploy:yes'))
        self.assertEqual(len(self.telegram.of('answerCallbackQuery')), 1)
        self.assertEqual(list(self.ledger()), ['command:41:answer'])

    def test_the_failure_notice_is_sent_once(self):
        self.set_handler('sys.exit(1)')
        self.drain(self.message('/deploy'))
        with patch('sys.stderr', new_callable=io.StringIO):
            self.bridge.inbound(self.message('/deploy'))
        self.assertEqual(len(self.telegram.of('sendMessage')), 1)


class SetCommandsTests(CommandCase):
    def test_menu_then_shown_commands_in_file_order_with_read_back(self):
        commands = (self.command('deploy', description='Deploy'), self.command('hidden', menu=False),
                    self.command('report', description='Weekly report'))
        self.bridge = self.make_bridge(commands)
        expected = [dict(command=c, description=d) for c, d in MENU] + [
            dict(command='deploy', description='Deploy'), dict(command='report', description='Weekly report')]
        self.telegram.answers['setMyCommands'] = (200, dict(ok=True, result=True))
        self.telegram.answers['getMyCommands'] = (200, dict(ok=True, result=expected))
        self.assertEqual(self.bridge.set_commands(), expected)
        self.assertEqual(self.telegram.calls, [
            ('setMyCommands', dict(commands=expected, scope=dict(type='chat', chat_id=123))),
            ('getMyCommands', dict(scope=dict(type='chat', chat_id=123)))])
        self.telegram.answers['getMyCommands'] = (200, dict(ok=True, result=expected[:3]))
        with self.assertRaisesRegex(b.TransportError, 'read-back differs'):
            self.bridge.set_commands()

    def test_commands_alone_make_a_menu(self):
        self.bridge = self.make_bridge((self.command(description='Deploy'),), menu=())
        self.telegram.answers['setMyCommands'] = (200, dict(ok=True, result=True))
        self.telegram.answers['getMyCommands'] = (200, dict(ok=True, result=[dict(command='deploy',
                                                                                    description='Deploy')]))
        self.assertEqual(self.bridge.set_commands(), [dict(command='deploy', description='Deploy')])
        self.bridge = self.make_bridge((self.command(menu=False),), menu=())
        self.telegram.calls.clear()
        self.telegram.answers['setMyCommands'] = (200, dict(ok=True, result=True))
        self.telegram.answers['getMyCommands'] = (200, dict(ok=True, result=[]))
        self.assertEqual(self.bridge.set_commands(), [])
        self.assertEqual(self.telegram.calls, [
            ('setMyCommands', dict(commands=[], scope=dict(type='chat', chat_id=123))),
            ('getMyCommands', dict(scope=dict(type='chat', chat_id=123)))])


class NoCommandsTests(CommandCase):
    def setUp(self):
        super().setUp()
        self.bridge = self.make_bridge(())

    def test_messages_reach_the_agent_and_callbacks_are_answered_empty(self):
        log = self.drain(self.message('/deploy'), self.callback('deploy:yes'))
        self.assertEqual(self.records(), [])
        self.assertEqual([body['message']['text'] for body in self.telegram.of('inbound')], ['/deploy'])
        self.assertEqual(self.telegram.of('answerCallbackQuery'), [dict(callback_query_id='query-41')])
        self.assertEqual(log, 'callback dropped (update 41): no_registered_prefix\n')
        self.assertEqual(self.store.offset(), 42)

    def test_get_updates_asks_for_messages_and_callbacks(self):
        polls = []
        def transport(url, body, *args, **kwargs):
            if url.endswith('/getUpdates'):
                polls.append(body)
                self.bridge.stop.set()
                return 200, dict(ok=True, result=[])
            return 200, {}
        self.bridge.request, self.bridge.port = transport, 0
        with patch.object(self.bridge, 'probe_with_retry', return_value=True), \
             patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
            self.bridge.run()
        self.assertEqual(polls[0]['allowed_updates'], ['message', 'callback_query'])


class AgentTests(CommandCase):
    def test_default_agent_is_the_mayor(self):
        self.bridge = self.make_bridge(())
        self.assertEqual(self.bridge.agent, 'mayor')
        self.drain(self.message('hello'))
        self.assertEqual(self.store.offset(), 41)

    def test_another_agent_accepts_only_entries_routed_to_it(self):
        self.bridge = self.make_bridge((), agent='other')
        self.telegram.agent = 'other'
        self.drain(self.message('hello'))
        self.assertEqual(self.store.offset(), 41)
        self.telegram.agent = 'mayor'
        with self.assertRaisesRegex(b.TransportError, 'routed to the agent'):
            self.drain(self.message('again', 42))
        self.assertEqual(self.store.offset(), 41)

    def test_the_quota_notice_watches_the_agents_session(self):
        health = self.root / 'health.json'
        until = int(time.time() * 1000) + 3600 * 1000
        observed = datetime.datetime.now(datetime.timezone.utc).isoformat()
        def sessions(template):
            health.write_text(json.dumps(dict(observed_at=observed, sessions=[dict(
                template=template, status='quota_wait', episode='e1', until=until)])))
        self.bridge = self.make_bridge((), agent='other', health_file=str(health))
        sessions('mayor')
        self.assertIsNone(self.bridge.mayor_quota_episode())
        sessions('other')
        self.assertEqual(self.bridge.mayor_quota_episode(), ('e1', until))
        mayor = self.make_bridge((), health_file=str(health))
        self.assertIsNone(mayor.mayor_quota_episode())

    def test_send_defaults_its_session_to_the_agent(self):
        parser = b._build_parser()
        with patch.dict(os.environ):
            os.environ.pop('GC_SESSION_ID')
            self.assertIsNone(b._build_parser().parse_args(['send']).session)
        self.assertEqual(parser.parse_args(['send', '--session', 's1']).session, 's1')

    def test_a_bad_agent_in_the_config_is_refused(self):
        with self.assertRaisesRegex(b.TransportError, 'agent name required'):
            self.make_bridge((), agent='two words')


if __name__ == '__main__':
    unittest.main()
