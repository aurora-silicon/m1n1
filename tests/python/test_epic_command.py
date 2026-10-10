from io import BytesIO
from types import SimpleNamespace

import pytest

from m1n1.fw.afk.epic import EPICCategory, EPICCmd, EPICError, EPICService, EPICSubtype
from m1n1.fw.aop.aopep import AOPAudioEndpoint
from m1n1.fw.aop.ipc import GetDeviceProp


CLOCK_REPLY = bytes.fromhex('0000000000405f0500010000110020004f41457a1000000000000000')
CLOCK_DATA = bytes.fromhex('0c00000020306978009f24006e69616d')
ERROR_REPLY = bytes.fromhex('f00200e0000000000000000000e00bdf094932a10000000000000000')


def service(reply=CLOCK_REPLY, subtype=0xa0, payload=CLOCK_DATA):
    events = []
    iface = SimpleNamespace(
        writemem=lambda *args: events.append(('write', args)),
        readmem=lambda *args: events.append(('read', args)) or payload,
    )
    proxy = SimpleNamespace(
        dc_cvac=lambda *args: events.append(('clean', args)),
        dc_ivac=lambda *args: events.append(('invalidate', args)),
    )
    ep = SimpleNamespace(name='audio', asc=SimpleNamespace(iface=iface, p=proxy))
    svc = EPICService(ep)
    svc.log = lambda *args: None
    svc.chan = 3
    svc.rxbuf, svc.rxbuf_dva = 0x200000, 0x100055f4000
    svc.txbuf, svc.txbuf_dva = 0x204000, 0x100055f8000
    ep.send_epic = lambda *args, **kwargs: events.append(('send', args, kwargs))
    ep.asc.work = lambda: svc.handle_reply(EPICCategory.REPLY, EPICSubtype.parse(subtype.to_bytes(2, "little")), 0, BytesIO(reply))
    return svc, events


@pytest.mark.parametrize('subtype', [0xa0, 0xc0])
def test_command_envelope_reads_shared_buffer(subtype):
    svc, events = service(subtype=subtype)
    assert svc.send_cmd(0x20, b'request', retlen=1024, version=2) == CLOCK_DATA
    assert svc.pending_cmd is None
    sent = next(event for event in events if event[0] == 'send')
    assert sent[2] == {'version': 2}
    request = EPICCmd.parse(sent[1][-1])
    assert (request.rxlen, request.txlen) == (1024, 7)
    assert events[:3] == [('write', (svc.txbuf, b'request')),
                          ('clean', (svc.txbuf, 7)),
                          ('invalidate', (svc.rxbuf, 1024))]
    assert events[-2:] == [('invalidate', (svc.rxbuf, 16)), ('read', (svc.rxbuf, 16))]


def test_error_envelope_does_not_require_or_read_rx_buffer():
    svc, events = service(ERROR_REPLY)
    with pytest.raises(EPICError, match='0xe00002f0'):
        svc.send_cmd(0x20, b'request', retlen=1024)
    assert svc.pending_cmd is None
    assert not any(event[0] == 'read' for event in events)


@pytest.mark.parametrize('field,value', [('rxbuf', 0), ('rxlen', 1025), ('rxlen', 0xffffffff)])
def test_invalid_success_envelope_is_rejected_before_read(field, value):
    response = EPICCmd.parse(CLOCK_REPLY)
    response[field] = value
    svc, events = service(EPICCmd.build(response))
    with pytest.raises(EPICError, match='response buffer'):
        svc.send_cmd(0x20, b'request', retlen=1024)
    assert svc.pending_cmd is None
    assert not any(event[0] == 'read' for event in events)


@pytest.mark.parametrize('data,capacity', [(b'x' * 0x4001, 1), (b'x', -1), (b'x', 0x4001)])
def test_request_bounds_are_checked_before_any_io(data, capacity):
    svc, events = service()
    with pytest.raises(ValueError):
        svc.send_cmd(0x20, data, retlen=capacity)
    assert events == []
    assert svc.pending_cmd is None


@pytest.mark.parametrize('stage', ['build', 'write', 'send', 'work', 'parse'])
def test_pending_command_is_cleared_on_failure(stage, monkeypatch):
    svc, events = service()
    def fail(*args, **kwargs):
        raise RuntimeError(stage)
    if stage == 'build':
        monkeypatch.setattr(EPICCmd, 'build', fail)
    elif stage == 'write':
        svc.iface.writemem = fail
    elif stage == 'send':
        svc.ep.send_epic = fail
    elif stage == 'work':
        svc.ep.asc.work = fail
    else:
        monkeypatch.setattr(EPICCmd, 'parse_stream', fail)
    with pytest.raises(RuntimeError, match=stage):
        svc.send_cmd(0x20, b'request', retlen=1024)
    assert svc.pending_cmd is None


def test_notification_a0_keeps_inline_payload():
    svc, events = service()
    reply = bytes.fromhex('0000000004000000656c6469')
    svc.handle_reply(EPICCategory.REPLY, EPICSubtype.RETCODE_WITH_PAYLOAD, 0, BytesIO(reply))
    assert svc.reply == reply
    assert events == []


def test_c0_typed_reply_keeps_response_reader():
    svc, events = service()
    call = GetDeviceProp(devid='hpai', modifier=200)
    svc.last_call = call
    svc.handle_reply(EPICCategory.REPLY, EPICSubtype.STD_SERVICE, 0,
                     BytesIO(bytes.fromhex('0000000004000000656c6469')))
    assert svc.reply is call.rets
    assert call.rets.data == 'idle'
    assert events == []


def test_zero_capacity_still_marks_pending_command():
    reply = EPICCmd.parse(CLOCK_REPLY)
    reply.rxlen = 0
    svc, events = service(EPICCmd.build(reply), payload=b'')
    assert svc.send_cmd(0x20, b'', retlen=0) == b''
    assert svc.pending_cmd is None


def test_pending_command_rejects_overlapping_command_and_notify():
    svc, events = service()
    svc.pending_cmd = 0
    for send in (svc.send_cmd, svc.send_notify):
        with pytest.raises(EPICError, match='pending'):
            send(0x20, b'x')
    assert events == []
    assert svc.pending_cmd == 0


def test_audio_command_wrapper_forwards_payload_and_options():
    calls = []
    svc = SimpleNamespace(send_cmd=lambda *args, **kwargs: calls.append((args, kwargs)) or b'ok')
    endpoint = object.__new__(AOPAudioEndpoint)
    endpoint.serv_map = {'aop-audio': svc}
    assert endpoint.send_cmd(0x20, b'request', retlen=1024, version=2) == b'ok'
    assert calls == [((0x20, b'request', 1024), {'version': 2})]
