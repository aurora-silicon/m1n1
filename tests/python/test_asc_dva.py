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


@pytest.mark.parametrize('allow_phys', [False, True])
@pytest.mark.parametrize('address', [0x8000, 0x18000, 0xffffffffef010bf0])
@pytest.mark.parametrize('operation,payload', [
    ('ioread', 16), ('iowrite', b'test'), ('iotranslate', 16),
])
def test_asc_physical_bypass_requires_opt_in(allow_phys, address, operation, payload):
    calls = []
    iface = SimpleNamespace(
        readmem=lambda *args: calls.append(('physical', args)),
        writemem=lambda *args: calls.append(('physical', args)),
    )
    backend = SimpleNamespace(proxy=None, iface=iface,
                              read=lambda *args, **kwargs: 0,
                              write=lambda *args, **kwargs: None)
    dart = SimpleNamespace()
    setattr(dart, operation, lambda *args: calls.append(('dart', args)))
    asc = StandardASC(backend, 0, dart, dva_mask=(1 << 42) - 1)
    asc.dva_offset, asc.dva_size = 0x10000, 0x10000
    asc.allow_phys = allow_phys
    result = getattr(asc, operation)(address, payload)
    if allow_phys and not 0x10000 <= address < 0x20000:
        if operation == 'iotranslate':
            assert result == [(address, payload)]
            assert calls == []
        else:
            assert calls == [('physical', (address, payload))]
    else:
        assert calls == [('dart', (0, address & asc.dva_mask, payload))]


@pytest.mark.parametrize('stream', [0, 1, 15])
def test_asc_mapping_invalidates_its_stream(stream):
    calls = []
    backend = SimpleNamespace(proxy=None, iface=None,
                              read=lambda *args, **kwargs: 0,
                              write=lambda *args, **kwargs: None)
    dart = SimpleNamespace(
        iomap=lambda *args: calls.append(('map', args)) or 0x8000,
        invalidate_streams=lambda mask: calls.append(('invalidate', mask)),
    )
    asc = StandardASC(backend, 0, dart, stream=stream)
    assert asc.iomap(0x123000, 0x4000) == 0x8000
    assert calls == [('map', (stream, 0x123000, 0x4000)), ('invalidate', 1 << stream)]
