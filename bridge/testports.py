"""Shared loopback port helper for the pack's tests.

With TEST_PORT_RANGE unset, tests bind port 0 and the kernel picks a
port, exactly as before. With TEST_PORT_RANGE set to "LOW-HIGH", every
listening socket the tests open binds a port inside that range, tried
in order. A malformed value, or a range with no free port, fails at
once with a message naming TEST_PORT_RANGE.

Pick a range that excludes ports the host's own services use. A service
that listens only on demand may otherwise find its port taken by a
test run.
"""
import errno
import http.server
import os
import re

_DIGITS = re.compile(r"\d+")


def test_port_range():
    """Return (low, high) from TEST_PORT_RANGE, or None when unset.

    Raises RuntimeError naming TEST_PORT_RANGE when the value is set
    but is not "LOW-HIGH" with 1024 <= LOW <= HIGH <= 65535.
    """
    if "TEST_PORT_RANGE" not in os.environ:
        return None
    raw = os.environ["TEST_PORT_RANGE"]
    message = (
        f"TEST_PORT_RANGE must be \"LOW-HIGH\" with "
        f"1024 <= LOW <= HIGH <= 65535, got {raw!r}"
    )
    parts = raw.split("-")
    if len(parts) != 2 or not _DIGITS.fullmatch(parts[0]) or not _DIGITS.fullmatch(parts[1]):
        raise RuntimeError(message)
    low = int(parts[0], 10)
    high = int(parts[1], 10)
    if not (1024 <= low <= high <= 65535):
        raise RuntimeError(message)
    return (low, high)


def candidate_ports():
    """Return the ports to try in order: [0] when unset, else the range."""
    parsed = test_port_range()
    if parsed is None:
        return [0]
    low, high = parsed
    return list(range(low, high + 1))


def bind_test_server(handler, server_class=None):
    """Bind a loopback server for `handler` and return the bound server.

    Bind-then-use: the returned server is already bound, so its port
    is the one it listens on. With TEST_PORT_RANGE unset this binds
    port 0. With it set, ports are tried in order until one binds,
    and the bound port is checked to lie inside the range.
    """
    if server_class is None:
        server_class = http.server.ThreadingHTTPServer
    parsed = test_port_range()
    if parsed is None:
        return server_class(('127.0.0.1', 0), handler)
    low, high = parsed
    last_error = None
    for port in range(low, high + 1):
        try:
            server = server_class(('127.0.0.1', port), handler)
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                last_error = error
                continue
            raise
        bound = server.server_address[1]
        if not (low <= bound <= high):
            server.server_close()
            raise RuntimeError(
                f"TEST_PORT_RANGE {low}-{high}: bound port {bound} is outside the range"
            )
        return server
    raise RuntimeError(
        f"TEST_PORT_RANGE {low}-{high}: no free port in the range"
        + (f" (last error: {last_error})" if last_error is not None else "")
    )


def run_bridge(bridge):
    """Run `bridge.run()` on a test port, retrying the next port on bind conflict.

    The bridge's own callback server binds `bridge.port` inside
    `run()`, so this sets that port from the helper before each try.
    With TEST_PORT_RANGE unset it sets port 0 once, as before. With
    it set, it tries each port in order and retries only when the
    bind fails with EADDRINUSE.
    """
    parsed = test_port_range()
    if parsed is None:
        bridge.port = 0
        bridge.run()
        return
    low, high = parsed
    last_error = None
    for port in range(low, high + 1):
        bridge.port = port
        try:
            bridge.run()
            return
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                last_error = error
                continue
            raise
    raise RuntimeError(
        f"TEST_PORT_RANGE {low}-{high}: no free port in the range"
        + (f" (last error: {last_error})" if last_error is not None else "")
    )
