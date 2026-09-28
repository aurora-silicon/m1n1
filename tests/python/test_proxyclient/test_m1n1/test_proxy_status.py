# SPDX-License-Identifier: MIT

import struct

import pytest

from proxyclient.m1n1.proxy import M1N1Proxy, ProxyRemoteError


def test_remote_operation_failure_is_distinct_from_unknown_opcode():
    assert M1N1Proxy.P_KBOOT_BOOT_STORAGE == 0x705
    class Interface:
        def proxyreq(self, request, **_kwargs):
            opcode = struct.unpack_from("<Q", request)[0]
            return struct.pack("<QqQ", opcode, -2, 0)

    proxy = object.__new__(M1N1Proxy)
    proxy.iface = Interface()
    proxy.debug = False
    with pytest.raises(ProxyRemoteError, match="Remote operation failed"):
        proxy._request(proxy.P_SMP_CALL, 99, 1)
