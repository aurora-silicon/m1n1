#!/usr/bin/env python3
# SPDX-License-Identifier: MIT

import contextlib
import io
import os
import serial
import unittest
from unittest.mock import patch

from m1n1.proxy import UartInterface, UartTimeout


class _FakeSerial:
    def __init__(self, *, disconnect_once=False):
        self.port = "/dev/cu.usbmodem-test"
        self.timeout = 5
        self.close_count = 0
        self.open_count = 0
        self.disconnect_once = disconnect_once

    def close(self):
        self.close_count += 1

    def open(self):
        self.open_count += 1
        if self.disconnect_once:
            self.disconnect_once = False
            raise serial.serialutil.SerialException("device disappeared")


class ProxyReconnectTests(unittest.TestCase):
    def test_open_node_must_answer_protocol_before_wait_boot_returns(self):
        iface = UartInterface.__new__(UartInterface)
        iface.dev = _FakeSerial(disconnect_once=True)
        iface.reply = lambda _opcode: (_ for _ in ()).throw(UartTimeout("reset"))

        nop_calls = 0

        def nop():
            nonlocal nop_calls
            nop_calls += 1
            if nop_calls == 1:
                raise UartTimeout("CDC node exists but proxy is not ready")

        iface.nop = nop

        with patch("m1n1.proxy.time.sleep", return_value=None):
            with contextlib.redirect_stdout(io.StringIO()):
                iface.wait_boot()

        self.assertEqual(nop_calls, 2)
        self.assertEqual(iface.dev.open_count, 3)
        self.assertEqual(iface.dev.timeout, 5)
        self.assertGreaterEqual(iface.dev.close_count, 2)

    def test_reconnect_readiness_is_bounded(self):
        iface = UartInterface.__new__(UartInterface)
        iface.dev = _FakeSerial()
        iface.reply = lambda _opcode: (_ for _ in ()).throw(UartTimeout("reset"))
        iface.nop = lambda: None

        with patch.dict(os.environ, {"M1N1RECONNECTTIMEOUT": "0"}):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(
                    UartTimeout, "Reconnection/protocol readiness timed out"
                ):
                    iface.wait_boot()

        self.assertEqual(iface.dev.open_count, 0)
        self.assertEqual(iface.dev.timeout, 5)


if __name__ == "__main__":
    unittest.main()
