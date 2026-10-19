"""Preserve exact memory writes through the word-oriented KIS transport."""

import ast
import struct
import sys
from enum import IntFlag
from pathlib import Path


def test_kis_write_preserves_odd_payloads_and_transaction_boundaries():
    path = Path(__file__).resolve().parents[2] / "proxyclient/m1n1/proxy.py"
    source = ast.parse(path.read_text())
    interface = next(node for node in source.body if isinstance(node, ast.ClassDef)
                     and node.name == "UartInterface")
    feature = next(node for node in source.body if isinstance(node, ast.ClassDef)
                   and node.name == "Feature")
    methods = [node for node in interface.body if isinstance(node, ast.FunctionDef)
               and node.name in {"checksum", "data_checksum", "writemem"}]
    namespace = {"IntFlag": IntFlag, "struct": struct, "sys": sys}
    exec(compile(ast.Module(body=[feature, *methods], type_ignores=[]), str(path), "exec"), namespace)

    class Peer:
        REQ_MEMWRITE = 0x03AA55FF
        CHECKSUM_SENTINEL = 0xD0DECADE
        DATA_END_SENTINEL = 0xB0CACC10
        checksum = namespace["checksum"]
        data_checksum = namespace["data_checksum"]
        writemem = namespace["writemem"]
        debug = False

        def __init__(self, kis, features):
            self.is_kis = kis
            self.enabled_features = features
            self.dev = self
            self.memory = {}
            self.transactions = 0

        def cmd(self, command, request):
            assert command == self.REQ_MEMWRITE
            self.address, self.size, self.digest = struct.unpack("<QQI", request)
            self.wire = bytearray()

        def write(self, data):
            count = len(data) & ~3 if self.is_kis else len(data)
            self.wire.extend(data[:count])
            return len(data)

        def reply(self, command):
            assert command == self.REQ_MEMWRITE
            sentinel = (struct.pack("<I", self.DATA_END_SENTINEL)
                        if self.enabled_features else b"")
            end = self.size + len(sentinel)
            assert len(self.wire) >= end, "KIS discarded a partial transport word"
            payload = bytes(self.wire[:self.size])
            assert self.wire[self.size:end] == sentinel
            assert self.wire[end:] == b"\0" * (-end % 4 if self.is_kis else 0)
            assert self.digest == self.data_checksum(payload)
            self.memory.update((self.address + i, value) for i, value in enumerate(payload))
            self.transactions += 1

    for kis in (False, True):
        for features in (namespace["Feature"](0), namespace["Feature"].DISABLE_DATA_CSUMS):
            for size in (0, 1, 2, 3, 511, 667, 1023, 1024, 1025, 2051):
                peer = Peer(kis, features)
                payload = bytes(i % 251 for i in range(size))
                peer.writemem(0x10000, payload)
                assert peer.memory == {0x10000 + i: value for i, value in enumerate(payload)}
                assert peer.transactions == (max(1, (size + 1023) // 1024) if kis else 1)
