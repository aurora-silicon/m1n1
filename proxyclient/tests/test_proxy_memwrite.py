#!/usr/bin/env python3
# SPDX-License-Identifier: MIT

import contextlib
import io
import struct
import unittest

from m1n1.proxy import Feature, UartInterface


class FakeSerial:
    def __init__(self):
        self.writes = []

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        return len(data)


def make_interface(features):
    interface = object.__new__(UartInterface)
    interface.debug = False
    interface.dev = FakeSerial()
    interface.enabled_features = features
    interface.cmd = lambda _command, _request: None
    interface.reply = lambda _command: b""
    return interface


class ProxyMemwriteTests(unittest.TestCase):
    def test_checksum_free_usb_uses_megabyte_writes_and_bounded_progress(self):
        interface = make_interface(Feature.DISABLE_DATA_CSUMS)
        data = b"x" * (33 * 1024 * 1024 + 17)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            interface.writemem(0x800000000, data, progress=True)

        payloads = interface.dev.writes[:-1]
        self.assertEqual(len(payloads), 34)
        self.assertTrue(all(len(part) == 1024 * 1024 for part in payloads[:-1]))
        self.assertEqual(len(payloads[-1]), 17)
        self.assertEqual(b"".join(payloads), data)
        self.assertEqual(
            interface.dev.writes[-1],
            struct.pack("<I", interface.DATA_END_SENTINEL),
        )
        self.assertEqual(output.getvalue(), "...\n")

    def test_checksum_uart_retains_bounded_small_writes(self):
        interface = make_interface(Feature(0))
        data = b"y" * (2 * interface.UART_MEMWRITE_CHUNK_SIZE + 9)

        interface.writemem(0x800000000, data)

        self.assertEqual(
            [len(part) for part in interface.dev.writes],
            [interface.UART_MEMWRITE_CHUNK_SIZE,
             interface.UART_MEMWRITE_CHUNK_SIZE, 9],
        )
        self.assertEqual(b"".join(interface.dev.writes), data)


if __name__ == "__main__":
    unittest.main()
