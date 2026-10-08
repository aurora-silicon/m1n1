import struct

import pytest

from m1n1.fw.isp import isp_transport as module


class Memory:
    def __init__(self):
        self.data = bytearray(256)
        self.events = []
        self.reuse = False
        struct.pack_into('<Q', self.data, 64, 2)

    def readmem(self, pa, size):
        self.events.append(('read', pa, size))
        return bytes(self.data[pa:pa + size])

    def writemem(self, pa, data):
        self.events.append(('payload', pa))
        self.data[pa:pa + len(data)] = data

    def write64(self, pa, value):
        self.events.append(('validity', pa))
        struct.pack_into('<Q', self.data, pa, value)
        if self.reuse:
            self.data[pa:pa + 64] = bytes(64)

    def dc_ivac(self, pa, size):
        self.events.append(('invalidate', pa, size))

    def dc_cvac(self, pa, size):
        self.events.append(('clean', pa, size))


def model():
    memory = Memory()
    allocations = [dict(iova=0x10000000000, pa=0, size=256)]
    transport = module.ISPOwnedTransport(memory, memory,
        lambda: memory.events.append(('guard',)), lambda: allocations)
    return transport, memory, allocations


def test_owned_read_cache_range():
    transport, memory, _ = model()
    assert transport.read(0x10000000001, 128) == bytes(63) + b'\x02' + bytes(64)
    assert memory.events == [('guard',), ('invalidate', 0, 256), ('read', 1, 128)]


@pytest.mark.parametrize('iova,size', [(0x10000000000,0), (0x100000000ff,2),
    (0x10000000100,1), (-1,1), ((1 << 64)-1,2), (True,1)])
def test_unowned_access_has_no_memory_operation(iova,size):
    transport, memory, _ = model()
    with pytest.raises(ValueError):
        transport.read(iova,size)
    assert memory.events == [('guard',)]


def test_ambiguous_mapping_and_dynamic_allocations():
    transport, memory, allocations = model()
    allocations.append(dict(allocations[0]))
    with pytest.raises(ValueError):
        transport.read(0x10000000000,64)
    allocations.pop()
    assert transport.pa_for(0x10000000040,64) == 64


def test_publication_can_be_immediately_reused():
    transport, memory, _ = model()
    memory.reuse = True
    transport.publish(0x10000000040,(3,4,5,6,7,8,9,10),2)
    assert memory.events == [('guard',), ('invalidate',0,128), ('read',64,8),
        ('payload',72), ('clean',0,128), ('read',64,64),
        ('validity',64), ('clean',0,128)]


def test_ownership_change_refuses_payload():
    transport, memory, _ = model()
    with pytest.raises(ValueError):
        transport.publish(0x10000000040,(3,0,0,0,0,0,0,0),1)
    assert not any(event[0] in ('payload','validity') for event in memory.events)


def test_fair_pump_order():
    events=[]
    assert module.poll_control(lambda: events.append('service') or True,
        lambda: events.append('terminal'), lambda: events.append('reports'),
        lambda: events.append('ack'))
    assert events == ['service','terminal','reports']


@pytest.mark.parametrize('pa,words,expected', [
    (120, (3, 0, 0, 0, 0, 0, 0, 0), 2),
    (-64, (3, 0, 0, 0, 0, 0, 0, 0), 2),
    (64, (3,), 2),
    (64, (3, 0, 0, 0, 0, 0, 0, 1 << 64), 2),
    (64, (3, 0, 0, 0, 0, 0, 0, 0), -1),
])
def test_direct_publication_refuses_invalid_geometry_before_io(pa, words, expected):
    memory = Memory()
    with pytest.raises(ValueError):
        module.publish_slot(memory, memory, pa, words, expected)
    assert not memory.events


@pytest.mark.parametrize('pa,size', [(1, 256), (0, 129)])
def test_cache_envelope_must_be_owned(pa, size):
    transport, memory, allocations = model()
    allocations[0].update(pa=pa, size=size)
    with pytest.raises(ValueError, match='complete cache lines'):
        transport.read(0x10000000000, 1)
    assert memory.events == [('guard',)]


@pytest.mark.parametrize('with_poll', [False, True])
def test_unserviced_pump_acknowledges_after_callbacks(with_poll):
    events = []
    poll = (lambda: events.append('reports')) if with_poll else None
    assert not module.poll_control(lambda: events.append('service') or False,
        lambda: events.append('terminal'), poll, lambda: events.append('ack'))
    assert events == ['service', 'terminal'] + (['reports'] if with_poll else []) + ['ack']
