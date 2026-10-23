"""Unit tests for the shared test port helper.

Fake-only: binds loopback servers in this process, no network beyond
127.0.0.1, temporary ports only.
"""
import http.server
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testports  # noqa: E402


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


def _serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class _FakeBridge:
    """Stand-in for the bridge: its run() binds its callback server.

    Mirrors the real bridge, which binds a ThreadingHTTPServer on
    ('127.0.0.1', self.port) inside run(). Records every attempted
    port and every bound port, then closes the server at once.
    """

    def __init__(self):
        self.port = None
        self.attempts = []
        self.bound_ports = []

    def run(self):
        self.attempts.append(self.port)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', self.port), _Handler)
        try:
            self.bound_ports.append(server.server_address[1])
        finally:
            server.server_close()


class TestPortRangeTests(unittest.TestCase):
    def test_unset_uses_port_zero_path(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_PORT_RANGE", None)
            self.assertIsNone(testports.test_port_range())
            self.assertEqual(testports.candidate_ports(), [0])
            server = _serve(testports.bind_test_server(_Handler))
            try:
                self.assertNotEqual(server.server_address[1], 0)
                self.assertGreater(server.server_address[1], 0)
            finally:
                server.shutdown()
                server.server_close()

    def test_range_returns_port_in_range(self):
        with patch.dict(os.environ, {"TEST_PORT_RANGE": "9300-9399"}):
            self.assertEqual(testports.test_port_range(), (9300, 9399))
            server = _serve(testports.bind_test_server(_Handler))
            try:
                port = server.server_address[1]
                self.assertGreaterEqual(port, 9300)
                self.assertLessEqual(port, 9399)
            finally:
                server.shutdown()
                server.server_close()

    def test_malformed_values_fail_naming_test_port_range(self):
        for raw in ("", "9300", "9399-9300", "80-90", "a-b", "9300-9399\n",
                    "9300\n-9399", "9300-9399\r\n", "9300 -9399"):
            with self.subTest(raw=raw):
                with patch.dict(os.environ, {"TEST_PORT_RANGE": raw}):
                    with self.assertRaises(RuntimeError) as ctx:
                        testports.test_port_range()
                    self.assertIn("TEST_PORT_RANGE", str(ctx.exception))
                    with self.assertRaises(RuntimeError) as ctx2:
                        testports.bind_test_server(_Handler)
                    self.assertIn("TEST_PORT_RANGE", str(ctx2.exception))

    def test_range_with_only_port_taken_fails_naming_test_port_range(self):
        blocker = _serve(testports.bind_test_server(_Handler))
        try:
            taken = blocker.server_address[1]
            with patch.dict(os.environ, {"TEST_PORT_RANGE": f"{taken}-{taken}"}):
                with self.assertRaises(RuntimeError) as ctx:
                    testports.bind_test_server(_Handler)
                self.assertIn("TEST_PORT_RANGE", str(ctx.exception))
        finally:
            blocker.shutdown()
            blocker.server_close()

    def test_taken_first_port_is_skipped_for_next_in_range(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_PORT_RANGE", None)
            blocker = _serve(testports.bind_test_server(_Handler))
        try:
            taken = blocker.server_address[1]
            if taken + 5 > 65535:
                self.skipTest("taken port at top of range")
            with patch.dict(os.environ, {"TEST_PORT_RANGE": f"{taken}-{taken + 5}"}):
                server = _serve(testports.bind_test_server(_Handler))
                try:
                    self.assertNotEqual(server.server_address[1], taken)
                    self.assertGreater(server.server_address[1], taken)
                    self.assertLessEqual(server.server_address[1], taken + 5)
                finally:
                    server.shutdown()
                    server.server_close()
        finally:
            blocker.shutdown()
            blocker.server_close()

    def test_run_bridge_unset_uses_port_zero(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_PORT_RANGE", None)
            bridge = _FakeBridge()
            testports.run_bridge(bridge)
            self.assertEqual(bridge.attempts, [0])
            self.assertEqual(len(bridge.bound_ports), 1)
            self.assertNotEqual(bridge.bound_ports[0], 0)

    def test_run_bridge_with_range_binds_callback_port_in_range(self):
        with patch.dict(os.environ, {"TEST_PORT_RANGE": "9300-9399"}):
            bridge = _FakeBridge()
            testports.run_bridge(bridge)
            self.assertEqual(len(bridge.bound_ports), 1)
            bound = bridge.bound_ports[0]
            self.assertGreaterEqual(bound, 9300)
            self.assertLessEqual(bound, 9399)
            self.assertEqual(bridge.port, bound)
            self.assertTrue(all(9300 <= port <= 9399 for port in bridge.attempts))

    def test_run_bridge_retries_next_port_when_first_is_taken(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_PORT_RANGE", None)
            blocker = _serve(testports.bind_test_server(_Handler))
        try:
            taken = blocker.server_address[1]
            if taken + 5 > 65535:
                self.skipTest("taken port at top of range")
            with patch.dict(os.environ, {"TEST_PORT_RANGE": f"{taken}-{taken + 5}"}):
                bridge = _FakeBridge()
                testports.run_bridge(bridge)
                self.assertEqual(bridge.attempts[0], taken)
                self.assertGreaterEqual(len(bridge.attempts), 2)
                self.assertEqual(len(bridge.bound_ports), 1)
                bound = bridge.bound_ports[0]
                self.assertNotEqual(bound, taken)
                self.assertGreater(bound, taken)
                self.assertLessEqual(bound, taken + 5)
                self.assertEqual(bridge.port, bound)
        finally:
            blocker.shutdown()
            blocker.server_close()

    def test_run_bridge_with_only_port_taken_fails_naming_test_port_range(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_PORT_RANGE", None)
            blocker = _serve(testports.bind_test_server(_Handler))
        try:
            taken = blocker.server_address[1]
            with patch.dict(os.environ, {"TEST_PORT_RANGE": f"{taken}-{taken}"}):
                bridge = _FakeBridge()
                with self.assertRaises(RuntimeError) as ctx:
                    testports.run_bridge(bridge)
                self.assertIn("TEST_PORT_RANGE", str(ctx.exception))
                self.assertEqual(bridge.attempts, [taken])
                self.assertEqual(bridge.bound_ports, [])
        finally:
            blocker.shutdown()
            blocker.server_close()


if __name__ == "__main__":
    unittest.main()
