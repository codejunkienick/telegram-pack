"""Lettered options need buttons: a publish() body whose visible text
offers two or more lettered options (an `A:` line and a `B:` line, plain,
HTML or bulleted) without a keyboard is refused as
`options_need_keyboard` before Telegram is contacted and before anything
enters the delivery ledger. `--keyboard-file` skips the check, and
`--text-options` (`text_options: true` on publish()) lets a deliberate
text-only list through, recorded in the receipt."""
from contextlib import closing
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

OPTIONS_PLAIN = 'Which way?\nA: bridge check\nB: always-on replier'


class OptionsPublishTests(BridgeTestCase):
    def keyboard(self, key):
        ask_id = b.Bridge._ask_id_for_key(key)
        return [[dict(text='Bridge check', callback_data=f'ask:{ask_id}:a'),
                 dict(text='Replier', callback_data=f'ask:{ask_id}:b')]]

    def deliveries(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT count(*) FROM deliveries').fetchone()[0]

    def test_plain_lettered_options_are_refused_before_telegram(self):
        result = self.bridge.publish(self.publish(key='opt-plain', text=OPTIONS_PLAIN))
        self.assertFalse(result['delivered'])
        self.assertEqual(result['metadata']['reason'], 'options_need_keyboard')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.deliveries(), 0)
        # Refused again identically: the same key with the same body is
        # retry-safe, and the key stays free for the buttoned resend.
        again = self.bridge.publish(self.publish(key='opt-plain', text=OPTIONS_PLAIN))
        self.assertEqual(again['metadata']['reason'], 'options_need_keyboard')
        sent = self.bridge.publish(dict(self.publish(key='opt-plain', text=OPTIONS_PLAIN),
                                        keyboard=self.keyboard('opt-plain')))
        self.assertTrue(sent['delivered'])
        self.assertEqual(len(self.calls), 1)

    def test_html_lettered_options_are_refused_on_the_visible_text(self):
        request = dict(self.publish(key='opt-html',
                                    text='<b>Pick</b>\n<b>A:</b> one\n<b>B:</b> two'),
                       format='html')
        result = self.bridge.publish(request)
        self.assertFalse(result['delivered'])
        self.assertEqual(result['metadata']['reason'], 'options_need_keyboard')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.deliveries(), 0)

    def test_bulleted_and_dashed_lettered_options_are_refused(self):
        for key, text in (('opt-bullet', 'Pick one:\n• A) one\n• B) two'),
                          ('opt-dash', 'Pick one:\n- A - one\n* B — two')):
            with self.subTest(key=key):
                result = self.bridge.publish(self.publish(key=key, text=text))
                self.assertFalse(result['delivered'])
                self.assertEqual(result['metadata']['reason'], 'options_need_keyboard')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.deliveries(), 0)

    def test_nbsp_indented_options_are_refused(self):
        self.assertTrue(b.text_offers_lettered_options('\u00a0A: x\n\u00a0B: y'))
        self.assertTrue(b.text_offers_lettered_options('•\u00a0A) x\n•\u00a0B) y'))
        result = self.bridge.publish(
            self.publish(key='opt-nbsp', text='Which way?\n\u00a0A: one\n\u00a0B: two'))
        self.assertFalse(result['delivered'])
        self.assertEqual(result['metadata']['reason'], 'options_need_keyboard')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.deliveries(), 0)

    def test_a_confirmed_send_replays_its_receipt_not_the_refusal(self):
        # A send an older bridge confirmed under this key (before the
        # refusal existed) replays its saved receipt on the same
        # key/body retry; adding buttons to that key is a conflict.
        request = self.publish(key='opt-old', text=OPTIONS_PLAIN)
        with patch.object(b, 'text_offers_lettered_options', lambda visible: False):
            sent = self.bridge.publish(request)
        self.assertTrue(sent['delivered'])
        again = self.bridge.publish(request)
        self.assertEqual(again, sent)
        self.assertEqual(len(self.calls), 1)
        buttoned = self.bridge.publish(dict(request, keyboard=self.keyboard('opt-old')))
        self.assertEqual(buttoned['metadata']['reason'], 'idempotency_key_conflict')
        self.assertEqual(len(self.calls), 1)

    def test_a_keyboard_skips_the_check(self):
        request = dict(self.publish(key='opt-kb', text=OPTIONS_PLAIN),
                       keyboard=self.keyboard('opt-kb'))
        result = self.bridge.publish(request)
        self.assertTrue(result['delivered'])
        [body] = [body for url, body in self.calls if url.endswith('/sendMessage')]
        self.assertEqual(body['reply_markup'], {'inline_keyboard': self.keyboard('opt-kb')})

    def test_one_letter_and_midline_mentions_pass(self):
        for key, text in (('opt-one', 'Status\nA: done'),
                          ('opt-recap', 'A it is'),
                          ('opt-midline', 'Plan A won\nThe B-side ships next')):
            with self.subTest(key=key):
                result = self.bridge.publish(self.publish(key=key, text=text))
                self.assertTrue(result['delivered'])
        self.assertEqual(len(self.calls), 3)

    def test_the_detector_needs_consecutive_letters_from_a(self):
        self.assertTrue(b.text_offers_lettered_options('A: x\nB: y'))
        self.assertTrue(b.text_offers_lettered_options('B: y\nA: x'))
        self.assertTrue(b.text_offers_lettered_options('A: x\nB: y\nC: z'))
        self.assertFalse(b.text_offers_lettered_options('A: x\nC: z'))
        self.assertFalse(b.text_offers_lettered_options('A: x'))
        self.assertFalse(b.text_offers_lettered_options('Plan A and B-side'))

    def test_text_options_sends_and_is_recorded_in_the_receipt(self):
        request = dict(self.publish(key='opt-text', text=OPTIONS_PLAIN), text_options=True)
        result = self.bridge.publish(request)
        self.assertTrue(result['delivered'])
        self.assertIs(result['text_options'], True)
        self.assertIs(self.store.saved_receipt('opt-text')['text_options'], True)
        self.assertEqual(len(self.calls), 1)
        # The same key and body with the flag replays the saved receipt;
        # without it the refusal fires first, before the ledger is consulted.
        self.assertEqual(self.bridge.publish(request), result)
        refused = self.bridge.publish(self.publish(key='opt-text', text=OPTIONS_PLAIN))
        self.assertEqual(refused['metadata']['reason'], 'options_need_keyboard')
        self.assertEqual(len(self.calls), 1)

    def test_text_options_false_behaves_like_no_field(self):
        request = dict(self.publish(key='opt-false', text='a status'), text_options=False)
        result = self.bridge.publish(request)
        self.assertTrue(result['delivered'])
        self.assertNotIn('text_options', result)


class OptionsCliTests(BridgeTestCase):
    def args(self, *extra, command='reply-current', key='k-opt'):
        return b._build_parser().parse_args(
            [command, '--body-file', 'x.txt', '--idempotency-key', key, *extra])

    def keyboard_file(self, key):
        ask_id = b.Bridge._ask_id_for_key(key)
        rows = [[dict(text='Bridge check', callback_data=f'ask:{ask_id}:a'),
                 dict(text='Replier', callback_data=f'ask:{ask_id}:b')]]
        path = Path(self.tmp.name) / 'keyboard.json'
        path.write_text(json.dumps(rows))
        return str(path), rows

    def test_text_options_flag_defaults_off(self):
        self.assertFalse(self.args().text_options)
        self.assertTrue(self.args('--text-options').text_options)

    def test_dispatch_refuses_lettered_options_before_gc(self):
        for command in ('send', 'reply-current'):
            with self.subTest(command=command), \
                    patch.object(self.bridge, 'gc') as gc_mock, \
                    patch('sys.stderr', new_callable=io.StringIO) as log:
                with self.assertRaises(b.TransportError) as ctx:
                    b._dispatch_outbound(self.bridge,
                                         self.args(command=command, key=f'k-{command}'),
                                         OPTIONS_PLAIN)
            gc_mock.assert_not_called()
            self.assertIn('options_need_keyboard', str(ctx.exception))
            self.assertIn('--keyboard-file', str(ctx.exception))
            self.assertIn('Asks with buttons', str(ctx.exception))
            line = next(l for l in log.getvalue().splitlines() if l.startswith('GC_RECEIPT '))
            self.assertEqual(json.loads(line[len('GC_RECEIPT '):])['reason'],
                             'options_need_keyboard')
        self.assertEqual(self.calls, [])

    def test_dispatch_with_a_keyboard_file_sends_the_ask(self):
        path, rows = self.keyboard_file('k-kb')
        with patch.object(self.bridge, 'gc') as gc_mock:
            message_id = b._dispatch_outbound(
                self.bridge, self.args('--keyboard-file', path, key='k-kb'), OPTIONS_PLAIN)
        gc_mock.assert_not_called()
        self.assertEqual(message_id, '77')
        [body] = [body for url, body in self.calls if url.endswith('/sendMessage')]
        self.assertEqual(body['reply_markup'], {'inline_keyboard': rows})

    def test_dispatch_text_options_publishes_directly_and_records_it(self):
        with patch.object(self.bridge, 'gc') as gc_mock:
            message_id = b._dispatch_outbound(
                self.bridge, self.args('--text-options', key='k-text'), OPTIONS_PLAIN)
        gc_mock.assert_not_called()
        self.assertEqual(message_id, '77')
        self.assertIs(self.store.saved_receipt('k-text')['text_options'], True)

    def test_dispatch_replays_a_confirmed_send_instead_of_refusing(self):
        with patch.object(b, 'text_offers_lettered_options', lambda visible: False):
            sent = self.bridge.publish(self.publish(key='k-old', text=OPTIONS_PLAIN))
        self.assertTrue(sent['delivered'])
        fake_gc = lambda path, body, method='POST', timeout=20: {
            'Receipt': {'Delivered': True, 'MessageID': '77'}}
        with patch.object(self.bridge, 'gc', fake_gc):
            message_id = b._dispatch_outbound(self.bridge, self.args(key='k-old'), OPTIONS_PLAIN)
        self.assertEqual(message_id, '77')
        self.assertEqual(len(self.calls), 1)

    def test_dispatch_a_single_letter_still_uses_the_gc_relay(self):
        fake_gc = lambda path, body, method='POST', timeout=20: {
            'Receipt': {'Delivered': True, 'MessageID': '55'}}
        with patch.object(self.bridge, 'gc', fake_gc):
            message_id = b._dispatch_outbound(self.bridge, self.args(key='k-one'), 'A: done')
        self.assertEqual(message_id, '55')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
