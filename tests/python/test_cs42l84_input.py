import copy

import pytest

from proxyclient.m1n1.hw.codecs.cs42l84_input import CS42L84Input


def codec(*, detection=0xd2, lock=0x10):
    memory = {0: 0x42, 1: 0xa8, 2: 0x40, 0x147d: detection, 0x40e: lock,
              0x1474: 3, 0x1477: 0x80, 0x600: 8, 0x601: 4, 0x800: 6,
              0x2002: 5, 0x5000: 4, 0x5024: 0}
    original = memory.copy()
    events = []
    records = []
    clock = [0]

    def read(address, width):
        assert records[-1]['uncertain']
        events.append(('read', address, width))
        return memory.get(address, 0)

    def write(address, width, value):
        assert records[-1]['uncertain']
        events.append(('write', address, width, value))
        memory[address] = value

    def sleep(seconds):
        events.append(('sleep', seconds))
        clock[0] += seconds

    obj = CS42L84Input(read, write, lambda state: records.append(copy.deepcopy(state)),
                      clock=lambda: clock[0], sleep=sleep)
    return obj, memory, original, events, records


def test_native_input_profile_latch_detection_freeze_and_caller_owned_lifecycle():
    obj, memory, original, events, _ = codec()
    obj.prepare(lambda: events.append(('clock_ready',)))
    assert obj.state['prepared'] and not obj.state['tx_enabled']
    assert memory[0x1477] == 0x82 and obj.state['mic_latch_before'] == 0x80
    assert memory[0x5068] == 0x1f0001
    assert memory[0x4000] == 7 and memory[0x5010] == 128
    assert memory[0x1801] == 0x68 and memory[0x5000] == 6
    assert memory[0x800] == 7 and memory[0x600] == 11
    assert memory[0x2000] == 0x78 and memory[0x2003] == 0x38
    assert memory[0x5024] == 0 and memory.get(0x5020, 0) == 0
    assert [(e[1], e[3]) for e in events if e[0] == 'write'
            and e[1] in (6, 0x2000, 0x2003)] == [(6, 1), (0x2000, 0x78), (0x2003, 0x38), (6, 0)]
    writes = [e for e in events if e[0] == 'write']
    assert writes[0] == ('write', 0x1477, 8, 0x82)
    assert events.index(('clock_ready',)) < events.index(('write', 0x800, 8, 7))
    obj.enable_tx1(lambda: events.append(('receiver_started',)))
    assert events.index(('receiver_started',)) < events.index(('write', 0x5024, 8, 1))
    obj.disable_tx1()
    start = len(events)
    obj.restore(lambda: events.append(('receiver_stopped',)))
    assert events[start] == ('receiver_stopped',)
    assert obj.state['restored'] and not obj.state['prepared']
    assert all(memory[address] == original.get(address, 0) for address, _ in obj.saved)
    restore_writes = [e for e in events[start:] if e[0] == 'write']
    assert [e[3] for e in restore_writes if e[1] == 0x1474] == [7, 3]
    assert next(i for i, e in enumerate(restore_writes) if e[1] == 0x1474) < next(
        i for i, e in enumerate(restore_writes) if e[1] == 0x1801)
    assert not any(e[0] == 'write' and (0x3000 <= e[1] < 0x4000
                   or e[1] in (0x203, 0x4001, 0x5020, 0x506c)) for e in events)


@pytest.mark.parametrize('change', [{0: 0}, {0x1801: 0x10}, {0x5020: 1},
                                    {0x5024: 1}, {0x800: 7}, {0x602: 1}, {6: 1}])
def test_fresh_identity_output_reference_and_freeze_gates(change):
    obj, memory, _, events, _ = codec()
    memory.update(change)
    with pytest.raises(RuntimeError):
        obj.prepare(lambda: None)
    count = len(events)
    for action in (lambda: obj.prepare(lambda: None),
                   lambda: obj.enable_tx1(lambda: None), obj.disable_tx1,
                   lambda: obj.restore(lambda: None)):
        with pytest.raises(RuntimeError):
            action()
    assert len(events) == count and obj.state['uncertain']
    assert not any(e[0] == 'write' and e[1] == 0x5024 for e in events)


@pytest.mark.parametrize('status, lock', [(0, 0x10), (0xd3, 0x10), (0xd2, 0x20), (0xd2, 0)])
def test_detection_and_bounded_pll_fail_closed(status, lock):
    obj, _, _, events, _ = codec(detection=status, lock=lock)
    with pytest.raises(RuntimeError):
        obj.prepare(lambda: None)
    assert obj.state['uncertain'] and not obj.state.get('prepared')
    assert not any(e[0] == 'write' and e[1] == 0x5024 for e in events)


def test_detection_uses_classification_before_detector_powerdown():
    obj, memory, _, _, _ = codec()
    memory[0x1474] = 0
    write = obj.write

    def volatile(address, width, value):
        write(address, width, value)
        if address == 0x1474 and value & 1:
            memory[0x147d] &= ~3

    obj.write = volatile
    obj.prepare(lambda: None)
    assert memory[0x147d] == 0xd0 and obj.state['detected_status2'] == 0xd2


@pytest.mark.parametrize('gate', ['clock', 'started', 'stopped'])
def test_caller_gate_failure_prevents_all_further_register_operations(gate):
    obj, _, _, events, _ = codec()

    def failed():
        raise RuntimeError('Ownership gate failed')

    if gate == 'clock':
        action = lambda: obj.prepare(failed)
    else:
        obj.prepare(lambda: None)
        if gate == 'started':
            action = lambda: obj.enable_tx1(failed)
        else:
            obj.enable_tx1(lambda: None)
            obj.disable_tx1()
            action = lambda: obj.restore(failed)
    with pytest.raises(RuntimeError, match='Ownership gate'):
        action()
    count = len(events)
    with pytest.raises(RuntimeError):
        obj.restore(lambda: pytest.fail('Cleanup after failed gate'))
    assert len(events) == count and obj.state['uncertain']


@pytest.mark.parametrize('target', [(0x1477, 0x82), (0x2000, 0x78), (0x5024, 1), (0x1474, 7)])
def test_write_failure_stops_access_and_retains_state(target):
    obj, _, _, events, records = codec()
    original_write = obj.write

    def failing(address, width, value):
        if (address, value) == target:
            events.append(('failed_write', address, value))
            raise TimeoutError('Transport failed')
        original_write(address, width, value)

    obj.write = failing
    with pytest.raises(TimeoutError):
        obj.prepare(lambda: None)
        obj.enable_tx1(lambda: None)
        obj.disable_tx1()
        obj.restore(lambda: None)
    assert events[-1] == ('failed_write', *target)
    assert obj.state['uncertain'] and records[-1]['uncertain']
    count = len(events)
    with pytest.raises(RuntimeError):
        obj.disable_tx1()
    assert len(events) == count


@pytest.mark.parametrize('address', [0x800, 0x1477, 0x5024, 0x5000])
def test_foreign_state_not_overwritten_during_restore(address):
    obj, memory, _, events, _ = codec()
    obj.prepare(lambda: None)
    memory[address] ^= 1
    start = len(events)
    with pytest.raises(RuntimeError):
        obj.restore(lambda: None)
    assert not any(e[0] == 'write' and e[1] == address for e in events[start:])
    assert obj.state['uncertain']


def test_restoration_and_tx_lifecycle_are_terminal():
    obj, _, _, events, _ = codec()
    obj.prepare(lambda: None)
    obj.enable_tx1(lambda: None)
    with pytest.raises(RuntimeError):
        obj.enable_tx1(lambda: pytest.fail('Second start'))
    obj.disable_tx1()
    obj.restore(lambda: None)
    count = len(events)
    for action in (lambda: obj.prepare(lambda: None),
                   lambda: obj.enable_tx1(lambda: None), obj.disable_tx1,
                   lambda: obj.restore(lambda: None)):
        with pytest.raises(RuntimeError):
            action()
    assert len(events) == count


def test_malformed_read_and_failed_persistence_are_uncertain():
    obj, _, _, events, _ = codec()
    obj.read = lambda address, width: b'\0'
    with pytest.raises(ValueError):
        obj.prepare(lambda: None)
    assert obj.state['uncertain'] and not events
    obj, _, _, events, _ = codec()
    obj.persist = lambda state: (_ for _ in ()).throw(OSError('Evidence failed'))
    with pytest.raises(OSError):
        obj.prepare(lambda: None)
    assert obj.state['uncertain'] and not events


def test_latch_race_is_rejected_before_any_codec_write():
    obj, memory, _, events, _ = codec()
    read = obj.read
    calls = [0]

    def changed(address, width):
        if address == 0x1477:
            calls[0] += 1
            if calls[0] == 2:
                memory[address] = 2
        return read(address, width)

    obj.read = changed
    with pytest.raises(RuntimeError, match='changed before update'):
        obj.prepare(lambda: None)
    assert obj.state['uncertain']
    assert not any(e[0] == 'write' for e in events)


def test_foreign_output_enable_rejects_before_input_tx_write():
    obj, memory, _, events, _ = codec()
    obj.prepare(lambda: None)
    memory[0x1801] |= 0x10
    start = len(events)
    with pytest.raises(RuntimeError, match='Input/output state changed'):
        obj.enable_tx1(lambda: None)
    assert not any(e[0] == 'write' for e in events[start:])
    assert obj.state['uncertain']


def test_readback_mismatch_prevents_later_configuration_and_cleanup():
    obj, _, _, events, _ = codec()
    obj.write = lambda address, width, value: events.append(('ignored_write', address, value))
    with pytest.raises(RuntimeError, match='readback mismatch'):
        obj.prepare(lambda: None)
    assert events[-1] == ('read', 0x1477, 8)
    count = len(events)
    with pytest.raises(RuntimeError):
        obj.restore(lambda: pytest.fail('Cleanup after uncertain write'))
    assert len(events) == count and obj.state['uncertain']


@pytest.mark.parametrize('phase', ['write', 'tx_enabled'])
def test_post_access_persistence_failure_latches_uncertainty(phase):
    obj, _, _, events, _ = codec()
    if phase == 'tx_enabled':
        obj.prepare(lambda: None)
    persist = obj.persist

    def failed(state):
        persist(state)
        last = state['operations'][-1]
        if (phase == 'write' and last['kind'] == 'write' and not last['pending']
                or phase == 'tx_enabled' and state['tx_enabled']):
            raise OSError('Evidence failed after hardware access')

    obj.persist = failed
    with pytest.raises(OSError):
        if phase == 'write':
            obj.prepare(lambda: None)
        else:
            obj.enable_tx1(lambda: None)
    count = len(events)
    with pytest.raises(RuntimeError):
        obj.disable_tx1()
    assert len(events) == count and obj.state['uncertain']


def test_output_change_during_detection_rejects_before_freeze_or_adc_writes():
    obj, memory, _, events, _ = codec()
    sleep = obj.sleep

    def changed(seconds):
        sleep(seconds)
        memory[0x1801] = 0x10

    obj.sleep = changed
    with pytest.raises(RuntimeError, match='state changed before freeze'):
        obj.prepare(lambda: None)
    assert not any(e[0] == 'write' and e[1] in (6, 0x2000, 0x2003, 0x1801) for e in events)
    assert obj.state['uncertain']


@pytest.mark.parametrize('gate', ['clock', 'started', 'stopped'])
def test_false_caller_prerequisite_is_not_accepted(gate):
    obj, _, _, events, _ = codec()
    with pytest.raises(RuntimeError, match='Caller prerequisite gate failed'):
        if gate == 'clock':
            obj.prepare(lambda: False)
        else:
            obj.prepare(lambda: None)
            if gate == 'started':
                obj.enable_tx1(lambda: False)
            else:
                obj.disable_tx1()
                obj.restore(lambda: False)
    count = len(events)
    assert obj.state['uncertain']
    with pytest.raises(RuntimeError):
        obj.disable_tx1()
    assert len(events) == count


def test_foreign_output_state_prevents_bias_restoration_writes():
    obj, memory, _, events, _ = codec()
    obj.prepare(lambda: None)
    memory[0x1801] |= 0x10
    start = len(events)
    with pytest.raises(RuntimeError, match='state changed before restoration'):
        obj.restore(lambda: None)
    assert not any(e[0] == 'write' for e in events[start:])
    assert obj.state['uncertain']
