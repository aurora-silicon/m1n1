import struct

import pytest

from m1n1.fw.isp.isp_command import ISPCommandChannel
from test_isp_control_broker import Transport


def model(response_tail=None, header=None, notify_error=False):
    transport = Transport()
    events = []
    args = struct.pack('<3I', 0, 3, 0)
    transport.memory[0x1800] = args
    channel = dict(iova='0x1000', num=2, type=0, raw_source='0x1')
    def notify(source):
        events.append(source)
        if notify_error:
            raise RuntimeError('doorbell failed')
        slot, words, _ = transport.published[-1]
        transport.memory[slot] = struct.pack('<8Q', words[0] | 1,
            *(response_tail if response_tail is not None else (words[2], 0, 0, 0, 0, 0, 0)))
        if header:
            transport.memory[words[0]] = header + args[8:]
    dispatcher = ISPCommandChannel(transport, channel, notify, lambda: None)
    return dispatcher, transport, args, events


def test_successful_dispatch_wraps_and_captures_raw_before_validation():
    dispatcher, transport, args, events = model()
    captured = []
    for _ in range(2):
        assert dispatcher.send(0x1800, args, 12, lambda: None, timeout=5,
            on_response=lambda response, payload: captured.append((response, payload))) == args
    assert dispatcher.cursor == 0 and events == [2, 2]
    assert len(captured) == 2 and len(transport.published) == 2


@pytest.mark.parametrize('tail', [(12, 1, 0, 0, 0, 0, 0), (4, 0, 0, 0, 0, 0, 0),
    (12, 0, 0, 0, 0, 0, 1)])
def test_bad_reply_preserved_and_latched(tail):
    dispatcher, transport, args, _ = model(response_tail=tail)
    captured = []
    with pytest.raises(ValueError, match='reply/status'):
        dispatcher.send(0x1800, args, 12, lambda: None, timeout=5,
            on_response=lambda response, payload: captured.append(response))
    assert captured[0][1:] == tail and dispatcher.cursor == 0 and dispatcher.failed
    with pytest.raises(ValueError, match='cold recovery'):
        dispatcher.send(0x1800, args, 12, lambda: None, timeout=5)
    assert len(transport.published) == 1


@pytest.mark.parametrize('header', [struct.pack('<2I', 1, 3), struct.pack('<2I', 0, 4)])
def test_reply_header_rejected(header):
    dispatcher, _, args, _ = model(header=header)
    with pytest.raises(ValueError, match='header/status'):
        dispatcher.send(0x1800, args, 12, lambda: None, timeout=5)
    assert dispatcher.failed and dispatcher.cursor == 0


@pytest.mark.parametrize('iova,outsize,timeout', [(0x1801,12,5), (0x1800,13,5),
    (0x1800,-1,5), (0x1800,12,0), (0x1800,12,float('inf')), (0x1000,12,5)])
def test_bad_geometry_before_publication(iova, outsize, timeout):
    dispatcher, transport, args, events = model()
    with pytest.raises(ValueError):
        dispatcher.send(iova, args, outsize, lambda: None, timeout=timeout)
    assert not events and not transport.published and dispatcher.cursor == 0


def test_doorbell_failure_latches_without_cursor_advance():
    dispatcher, transport, args, _ = model(notify_error=True)
    with pytest.raises(RuntimeError):
        dispatcher.send(0x1800, args, 12, lambda: None, timeout=5)
    assert dispatcher.failed and dispatcher.cursor == 0 and len(transport.published) == 1


@pytest.mark.parametrize('during_read', [False, True])
def test_expired_start_deadline_has_no_publication(during_read):
    from m1n1.fw.isp.isp_command import StartDeadline
    dispatcher, transport, args, events = model()
    now = [0]
    deadline = StartDeadline(lambda: now[0])
    if during_read:
        read = transport.read
        def delayed_read(addr, size):
            now[0] = 40
            return read(addr, size)
        transport.read = delayed_read
    else:
        now[0] = 40
    with pytest.raises(TimeoutError):
        dispatcher.send(0x1800, args, 12, lambda: None, timeout=30,
            start_deadline=deadline)
    assert not events and not transport.published
