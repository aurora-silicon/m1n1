from types import SimpleNamespace
import struct

import pytest

from m1n1.fw.afk import rbep
from m1n1.fw.aop.aopep import AOPGyroEndpoint


def endpoint(monkeypatch):
    ticks = iter([0, 0, 0.5, 1, 2])
    monkeypatch.setattr(rbep.time, 'monotonic', lambda: next(ticks))
    sent = []
    asc = SimpleNamespace(iface=None, send=lambda msg, ep: sent.append(msg), work=lambda: None)
    return rbep.AFKRingBufEndpoint(asc, 0x20), asc, sent


@pytest.mark.parametrize('stale_complete', [False, True])
def test_initially_dead_endpoint_requires_fresh_shutdown_ack(monkeypatch, stale_complete):
    ep, asc, sent = endpoint(monkeypatch)
    ep.shutdown_complete = stale_complete
    with pytest.raises(rbep.ASCTimeout, match='not acknowledged'):
        ep.stop(timeout=1)
    assert len(sent) == 1
    assert isinstance(sent[0], rbep.AFKEP_Shutdown)
    assert not ep.shutdown_complete


def test_work_delivers_fresh_shutdown_ack(monkeypatch):
    ep, asc, sent = endpoint(monkeypatch)
    ep.alive = ep.started = True
    asc.work = lambda: ep.handle_msg(0xc1 << 48, 0x20)
    ep.stop(timeout=1)
    assert ep.shutdown_complete
    assert not ep.alive
    assert not ep.started


def test_ack_received_during_send_is_not_lost(monkeypatch):
    ep, asc, sent = endpoint(monkeypatch)
    asc.send = lambda msg, target: ep.handle_msg(0xc1 << 48, target)
    asc.work = lambda: pytest.fail('ACK already arrived during send')
    ep.stop(timeout=1)
    assert ep.shutdown_complete


def test_timeout_does_not_clear_live_queue_state(monkeypatch):
    ep, asc, sent = endpoint(monkeypatch)
    ep.alive = ep.started = True
    with pytest.raises(rbep.ASCTimeout):
        ep.stop(timeout=1)
    assert ep.alive
    assert ep.started


def test_gyro_uses_normal_queue_start():
    sent = []
    asc = SimpleNamespace(iface=None, send=lambda msg, target: sent.append(msg))
    ep = AOPGyroEndpoint(asc, 0x25)
    ep.start_queues()
    assert len(sent) == 1
    assert isinstance(sent[0], rbep.AFKEP_Start)


@pytest.mark.parametrize('block_size', [0x40, 0x80])
def test_ring_pointers_use_negotiated_header_spacing(block_size):
    writes = []
    iface = SimpleNamespace(
        readmem=lambda address, size: struct.pack('<II', 0x8000 - 3 * block_size, 0),
        writemem=lambda *args: pytest.fail('Pointer publication must use one aligned word store'))
    ep = SimpleNamespace(iface=iface, asc=SimpleNamespace(p=SimpleNamespace(
        write32=lambda address, value: writes.append((address, value)))))
    ring = rbep.AFKRingBuf(ep, 0x10000, 0x8000)
    ring.update_rptr(0x100)
    ring.update_wptr(0x200)
    assert writes == [(0x10000 + block_size, 0x100),
                      (0x10000 + 2 * block_size, 0x200)]
