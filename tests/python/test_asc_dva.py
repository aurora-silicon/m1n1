from types import SimpleNamespace

import pytest

from proxyclient.m1n1.fw.asc import StandardASC
from proxyclient.m1n1.fw.asc.ioreporting import (
    ASCIOReportingEndpoint, IOReporting_Report,
)


@pytest.mark.parametrize('mask,expected', [
    (0xFFFFFFFFF, 0x123000),
    ((1 << 42) - 1, 0x10000123000),
])
@pytest.mark.parametrize('operation,payload', [
    ('ioread', 16), ('iowrite', b'test'), ('iotranslate', 16),
])
def test_asc_dva_mask_preserves_or_removes_tag_as_configured(mask, expected, operation, payload):
    backend = SimpleNamespace(proxy=None, iface=None,
                              read=lambda *args, **kwargs: 0,
                              write=lambda *args, **kwargs: None)
    asc = StandardASC(backend, 0,
                      SimpleNamespace(), dva_mask=mask)
    asc.dva_size = 1 << 44
    calls = []
    setattr(asc.dart, operation, lambda *args: calls.append(args))
    getattr(asc, operation)(0x10000123000, payload)
    assert calls == [(0, expected, payload)]


def test_ioreport_preallocated_buffer_uses_dart_translation():
    reads, sent = [], []
    asc = SimpleNamespace(
        ioread=lambda addr, size: reads.append((addr, size)) or bytes(size),
        send=lambda msg, ep: sent.append((int(msg), int(ep))),
    )
    ep = ASCIOReportingEndpoint(asc, 4)
    ep.iobuffer = ep.iobuffer_dva = 0x10000123000
    ep.bufsize = 64
    assert ep.Init(IOReporting_Report()) is True
    assert reads == [(0x10000123000, 64)]
    assert sent == [(8 << 52, 4)]
