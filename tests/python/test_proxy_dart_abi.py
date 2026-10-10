"""DART packet ABI and persistent-buffer address arguments, without hardware."""
from pathlib import Path
import struct
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "proxyclient"))
from m1n1.proxy import DART, M1N1Proxy, ProxyRemoteError


class FakeIface:
    def __init__(self, status=M1N1Proxy.S_OK):
        self.status = status
        self.requests = []
        self.writes = []

    def proxyreq(self, packet, **kwargs):
        words = struct.unpack("<7Q", packet)
        self.requests.append(words)
        return struct.pack("<QqQ", words[0], self.status, 0x12345678)

    def writemem(self, address, data):
        self.writes.append((address, data))


class HeapSpy:
    def __init__(self):
        self.allocations = []
        self.frees = []

    def malloc(self, length):
        self.allocations.append(length)
        return 0x200000000

    def free(self, address):
        self.frees.append(address)


def proxy(status=M1N1Proxy.S_OK):
    iface = FakeIface(status)
    p = M1N1Proxy(iface)
    p.heap = HeapSpy()
    return p, iface


@pytest.mark.parametrize("dart_type", list(DART))
@pytest.mark.parametrize("keep_pts", [False, True])
def test_init_four_argument_packet_matches_C_ABI(dart_type, keep_pts):
    p, iface = proxy()
    assert p.dart_init(0x382f00000, 0, dart_type, keep_pts=keep_pts) == 0x12345678
    assert iface.requests == [(p.P_DART_INIT, 0x382f00000, 0, int(keep_pts),
                               int(dart_type), 0, 0)]
    assert not p.heap.allocations and not p.heap.frees and not iface.writes


def test_init_default_and_third_positional_type_compatibility():
    p, iface = proxy()
    p.dart_init(0x382f80000, 1)
    p.dart_init(0x382f80000, 1, DART.T8110)
    assert iface.requests == [(p.P_DART_INIT, 0x382f80000, 1, 0, int(DART.T8020), 0, 0),
                              (p.P_DART_INIT, 0x382f80000, 1, 0, int(DART.T8110), 0, 0)]


@pytest.mark.parametrize("bfr", [b"persistent", "persistent", bytearray(b"persistent"),
                                  memoryview(b"persistent"), 1.5, object()])
@pytest.mark.parametrize("with_heap", [False, True])
def test_non_address_buffer_rejected_before_allocation_write_or_RPC(bfr, with_heap):
    p, iface = proxy()
    heap = p.heap
    if not with_heap:
        p.heap = None
    with pytest.raises(TypeError):
        p.dart_map(0x1000, 0x80000000, bfr, 0x4000)
    assert not iface.requests and not iface.writes
    assert not heap.allocations and not heap.frees


class NumericAddress:
    def __index__(self):
        return 0x100040000


@pytest.mark.parametrize("bfr", [0x100040000, NumericAddress()])
def test_persistent_address_packet_uses_integer_or_index_without_temp_heap(bfr):
    p, iface = proxy()
    assert p.dart_map(0x1000, 0x80000000, bfr, 0x4000) == 0x12345678
    assert iface.requests == [(p.P_DART_MAP, 0x1000, 0x80000000, 0x100040000, 0x4000, 0, 0)]
    assert not p.heap.allocations and not p.heap.frees and not iface.writes


@pytest.mark.parametrize("method", ["dart_init", "dart_map"])
def test_remote_S_ERROR_retains_standard_exception_and_no_temporary_storage(method):
    p, iface = proxy(M1N1Proxy.S_ERROR)
    with pytest.raises(ProxyRemoteError, match="Remote operation failed"):
        if method == "dart_init":
            p.dart_init(0x382f00000, 0, DART.T8110)
        else:
            p.dart_map(0x1000, 0x80000000, NumericAddress(), 0x4000)
    assert len(iface.requests) == 1
    assert not p.heap.allocations and not p.heap.frees and not iface.writes
