"""Regression coverage for the Windows WinError 10022 fix.

On Windows, pymavlink's 'udpout' socket is never bind()-ed -- it only gets
a local address once the first sendto() happens (see prime_connection()'s
docstring in sim.py). This test can't reproduce WSAEINVAL itself (that's a
WinSock-only behavior, not reproducible on the Linux/macOS CI runners this
suite runs on), so instead it locks in the actual fix: that priming a fresh
connection sends a HEARTBEAT and, critically, does not touch recv at all --
i.e. the send-before-any-recv ordering the real bug violated.
"""
from unittest.mock import MagicMock

from sim import VehicleState, prime_connection


def test_prime_connection_sends_heartbeat_and_never_calls_recv():
    conn = MagicMock()
    state = VehicleState(home_lat=37.4275, home_lon=-122.1697)

    prime_connection(conn, state)

    conn.mav.heartbeat_send.assert_called_once()
    conn.recv_match.assert_not_called()


def test_prime_connection_works_before_any_state_step():
    # Guards against a future change to VehicleState breaking prime_connection
    # by requiring fields that only get populated after step() has run.
    conn = MagicMock()
    state = VehicleState(home_lat=0.0, home_lon=0.0)

    prime_connection(conn, state)  # must not raise

    args, _ = conn.mav.heartbeat_send.call_args
    assert args[0] == 2  # MAV_TYPE_QUADROTOR
