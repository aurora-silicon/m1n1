import struct

import pytest

from m1n1.fw.isp.isp_broker import ISPControlBroker


class Transport:
    def __init__(self):
        self.memory = {}
        self.published = []

    def pa_for(self, addr, size):
        if not 0x1000 <= addr < addr + size <= 0x2000:
            raise ValueError('Unowned extent')
        return addr

    def read(self, addr, size):
        self.pa_for(addr, size)
        return self.memory.get(addr, struct.pack('<8Q', 1, 0, 0, 0, 0, 0, 0, 0))[:size]

    def publish(self, addr, words, expected):
        assert struct.unpack('<Q', self.read(addr, 8))[0] == expected
        self.published.append((addr, words, expected))
        self.memory[addr] = struct.pack('<8Q', *words)


def model(channels=None):
    transport = Transport()
    allocations = []
    events = []
    state = dict(requests=[], terminal=[])
    if channels is None:
        channels = dict(SHAREDMALLOC=dict(iova='0x1000', num=2, type=1, raw_source='0x3'),
                        TERMINAL=dict(iova='0x1100', num=2, type=2, raw_source='0x0'))

    def allocate(tag, size):
        events.append(('allocate', tag, size))
        allocation = dict(iova=0x1800, size=size, index=8, name='LOG')
        allocations.append(allocation)
        return allocation

    broker = ISPControlBroker(transport, channels, allocate, lambda: allocations,
        lambda source: events.append(('notify', source)), lambda stage: None, state)
    return broker, transport, allocations, events, state


@pytest.mark.parametrize('flag', [3, 4, 5, 6, 7])
def test_allocation_flags_rejected_before_allocator(flag):
    broker, transport, _, events, _ = model()
    req = [0, 0x4000, 0x4c4f47, 0, 0, 0, 0, 0]
    req[flag] = 1
    transport.memory[0x1000] = struct.pack('<8Q', *req)
    with pytest.raises(ValueError, match='flags'):
        broker.service_one()
    assert not events and not transport.published and broker.cursor == 0
    assert broker.failed


def test_successful_allocation_and_free_wrap_retain_storage():
    broker, transport, allocations, events, state = model()
    transport.memory[0x1000] = struct.pack('<8Q', 0, 0x4000, 0x4c4f47, 0, 0, 0, 0, 0)
    assert broker.service_one()
    assert broker.cursor == 1 and state['log_response_published']
    assert events == [('allocate', 0x4c4f47, 0x4000), ('notify', 8)]
    transport.memory[0x1040] = struct.pack('<8Q', 0x1800, 0, 0, 0, 0, 0, 0, 0)
    assert broker.service_one()
    assert broker.cursor == 0 and len(allocations) == 1 and allocations[0]['retired']
    assert allocations[0]['size'] == 0x4000


@pytest.mark.parametrize('retired,tail', [(False, 1), (True, 0)])
def test_invalid_free_has_no_publication(retired, tail):
    broker, transport, allocations, events, _ = model()
    allocations.append(dict(iova=0x1800, retired=retired))
    transport.memory[0x1000] = struct.pack('<8Q', 0x1800, tail, 0, 0, 0, 0, 0, 0)
    with pytest.raises(ValueError, match='owned surface'):
        broker.service_one()
    assert not events and not transport.published and broker.cursor == 0


def test_unknown_free():
    broker, transport, _, events, _ = model()
    transport.memory[0x1000] = struct.pack('<8Q', 0x1800, 0, 0, 0, 0, 0, 0, 0)
    with pytest.raises(ValueError):
        broker.service_one()
    assert not events and not transport.published


@pytest.mark.parametrize('field,value', [('num', 0), ('num', True), ('num', 0x1001),
    ('iova', '0x1001'), ('type', 0), ('raw_source', '0x1')])
def test_invalid_geometry(field, value):
    channels = dict(SHAREDMALLOC=dict(iova='0x1000', num=2, type=1, raw_source='0x3'),
                    TERMINAL=dict(iova='0x1100', num=2, type=2, raw_source='0x0'))
    channels['TERMINAL'][field] = value
    with pytest.raises(ValueError):
        model(channels)


def test_overlapping_rings():
    channels = dict(SHAREDMALLOC=dict(iova='0x1000', num=2, type=1, raw_source='0x3'),
                    TERMINAL=dict(iova='0x1040', num=2, type=2, raw_source='0x0'))
    with pytest.raises(ValueError, match='overlap'):
        model(channels)


@pytest.mark.parametrize('content', [b'Exception: fault', b'ASSERT: failed'])
def test_terminal_fault_preserved_without_ack(content):
    broker, transport, _, events, state = model()
    transport.memory[0x1100] = struct.pack('<8Q', 0x1800, len(content), 7, 0, 0, 0, 0, 0)
    transport.memory[0x1800] = content
    with pytest.raises(ValueError, match='exception/assertion'):
        broker.drain_terminal()
    assert state['terminal'][0]['text'] == content.decode()
    assert not events and not transport.published and broker.terminal_cursor == 0


def test_terminal_unowned_pointer_not_acked():
    broker, transport, _, events, _ = model()
    transport.memory[0x1100] = struct.pack('<8Q', 0x2000, 4, 0, 0, 0, 0, 0, 0)
    with pytest.raises(ValueError, match='Unowned'):
        broker.drain_terminal()
    assert not events and not transport.published


def test_terminal_quota_wrap_and_idle():
    broker, transport, _, events, state = model()
    for slot in (0x1100, 0x1140):
        transport.memory[slot] = struct.pack('<8Q', 0x1802, 4, 0, 0, 0, 0, 0, 0)
    transport.memory[0x1800] = b'log\n'
    broker.drain_terminal(limit=1)
    assert broker.terminal_cursor == 1
    broker.drain_terminal(limit=1)
    assert broker.terminal_cursor == 0 and len(state['terminal']) == 2
    broker.drain_terminal()
    assert events == [('notify', 1), ('notify', 1)]


def test_publication_failure_latches_broker():
    broker, transport, allocations, events, _ = model()
    transport.memory[0x1000] = struct.pack('<8Q', 0, 0x4000, 0x4c4f47, 0, 0, 0, 0, 0)
    def fail(*args):
        raise RuntimeError('publication failed')
    transport.publish = fail
    with pytest.raises(RuntimeError):
        broker.service_one()
    assert len(allocations) == 1 and broker.cursor == 0
    with pytest.raises(ValueError, match='cold recovery'):
        broker.service_one()
    assert len(events) == 1


@pytest.mark.parametrize('length', [0, 513])
def test_terminal_invalid_length_saved_without_ack(length):
    broker, transport, _, events, state = model()
    transport.memory[0x1100] = struct.pack('<8Q', 0x1800, length, 0, 0, 0, 0, 0, 0)
    with pytest.raises(ValueError, match='terminal report'):
        broker.drain_terminal()
    assert state['terminal_unhandled'][1] == hex(length)
    assert not events and not transport.published


def test_doorbell_failure_does_not_advance_or_retry():
    broker, transport, allocations, _, _ = model()
    transport.memory[0x1000] = struct.pack('<8Q', 0, 0x4000, 0x4c4f47, 0, 0, 0, 0, 0)
    def fail(source):
        raise RuntimeError('doorbell failed')
    broker.notify = fail
    with pytest.raises(RuntimeError):
        broker.service_one()
    assert len(transport.published) == 1 and len(allocations) == 1 and broker.cursor == 0
    with pytest.raises(ValueError, match='cold recovery'):
        broker.service_one()
    assert len(transport.published) == 1
