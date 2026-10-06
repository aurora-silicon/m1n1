import ast
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from m1n1.fw.mtp import encode_packet
import m1n1.hw.dockchannel as dockchannel


def test_queued_reply_before_enable_preserves_evidence_and_sends_nothing(monkeypatch):
    source = Path(__file__).parents[2] / 'proxyclient/experiments/t6040_iop.py'
    tree = ast.parse(source.read_text())
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == 'probe_mtp')
    init = (b'\xf0\x01\x00\x02' + b'keyboard' + bytes(8) + bytes(2)
            + struct.pack('<HH', 2, 6) + bytes(6))
    queued = bytearray(encode_packet(0, 0, struct.pack('<HHI', 0, len(init), 0) + init))
    stale = encode_packet(0, 0, struct.pack('<HHI', 0x80, 1, 0) + b'\xb4')

    class Count:
        @property
        def val(self):
            return len(queued)

    class Receive:
        @property
        def val(self):
            word = struct.unpack_from('<I', queued)[0]
            del queued[:4]
            return word

    class Free:
        @property
        def val(self):
            # An old ACK becomes visible while the host checks TX space.
            queued.extend(stale)
            return 2048

    data = SimpleNamespace(RX_COUNT=Count(), RX_32=Receive(), TX_FREE=Free())
    monkeypatch.setattr(dockchannel, 'DockChannelDataRegs', lambda *args: data)
    ticks = iter(index / 100 for index in range(2000))
    rx_data = bytearray()

    def read_fifo(code, register, buffer, words):
        assert words > 0
        rx_data[:] = queued[:words * 4]
        del queued[:words * 4]

    context = {'struct': struct, 'time': SimpleNamespace(monotonic=lambda: next(ticks)),
               'u': SimpleNamespace(adt={'/arm-io/dockchannel-mtp':
                                        SimpleNamespace(get_reg=lambda idx: (0, 0))},
                                    memalign=lambda *args: 0, exec=read_fifo,
                                    iface=SimpleNamespace(readmem=lambda addr, size: bytes(rx_data[:size]))),
               'wait_until': lambda condition, service: condition()}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), context)
    result = {}
    with pytest.raises(ValueError, match='replies remain queued'):
        context['probe_mtp'](SimpleNamespace(work=lambda: None), True, result)
    assert result['initialization'][0]['name'] == 'keyboard'
    assert not result['keyboard_enable_ack']
    assert not result['keyboard_ready']
    assert not hasattr(data, 'TX_32')  # Reaching the transmit loop would fail.


@pytest.mark.parametrize('keyboard, reject_off_phase, fresh_actuator_ready',
    [(keyboard, phase, True) for keyboard in (False, True)
     for phase in (None, 0, 1, 'dimensions-short', 'dimensions-id')] +
    [(keyboard, None, False) for keyboard in (False, True)])
def test_touchpad_shutdown_requires_both_off_replies(monkeypatch, keyboard, reject_off_phase,
                                                    fresh_actuator_ready):
    import m1n1.fw.mtp as mtp

    source = Path(__file__).parents[2] / 'proxyclient/experiments/t6040_iop.py'
    tree = ast.parse(source.read_text())
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == 'probe_mtp')
    queued = bytearray()
    commands = []
    tx = bytearray()

    def receive(iface, counter, message, flags=0, status=0, kind=0x12):
        packet = bytearray(encode_packet(iface, counter,
                                        struct.pack('<HHI', flags, len(message), status) + message))
        packet[1] = kind
        struct.pack_into('<I', packet, len(packet) - 4, mtp.checksum(packet[:-4]))
        queued.extend(packet)

    for iface, name in ((1, b'multi-touch'), (2, b'keyboard'), (4, b'actuator')):
        init = (b'\xf0\x01\x00' + bytes([iface]) + name.ljust(16, b'\0') + bytes(2)
                + struct.pack('<HH', 2, 6) + bytes(6))
        receive(0, iface, init)
    receive(0, 0, b'\xf1\x04\x00\x00')  # A stale notification must not qualify enable.

    class Count:
        @property
        def val(self):
            return len(queued)

    class Receive:
        @property
        def val(self):
            word = struct.unpack_from('<I', queued)[0]
            del queued[:4]
            return word

    class Transmit:
        @property
        def val(self):
            raise AssertionError('Transmit register is write-only')

        @val.setter
        def val(self, value):
            tx.extend(struct.pack('<I', value))
            if len(tx) < 8:
                return
            size = struct.unpack_from('<H', tx, 2)[0] + 12
            if len(tx) != size:
                return
            iface, kind, counter, payload = mtp.decode_packet(bytes(tx))
            tx.clear()
            request = mtp.RXMessage.parse(payload).msg
            if iface == 1:
                assert counter == 0 and request == b'\xd9'
                dimensions = bytes.fromhex('d9f03c000080250000e0e25dff281f4324')
                if reject_off_phase == 'dimensions-short':
                    dimensions = dimensions[:10]
                elif reject_off_phase == 'dimensions-id':
                    dimensions = b'\xd8' + dimensions[1:]
                receive(1, counter, dimensions, flags=0x81, kind=0x11)
                return
            commands.append((counter, request))
            status = 0
            # Initialization includes Off before On. Reject only the final Off pair.
            final_off = (request[:4] == b'\x40\x02\x01\x00'
                         and any(req[:4] == b'\x40\x02\x01\x02' for _, req in commands))
            if final_off and request[4] == reject_off_phase:
                status = 1
            # Input may arrive between a control request and its reply.
            receive(1, 0, b'\x60\x02')
            receive(0, counter, request if request[0] == 0x40 else request[:1],
                    flags=0x80, status=status, kind=0x11)
            if request == b'\xb4\x02':
                receive(0, 0, b'\xf1\x02\x00\x00')
            elif request == b'\xb4\x04' and fresh_actuator_ready:
                receive(0, 0, b'\xf1\x04\x00\x00')
            elif request[:5] == b'\x40\x02\x01\x02\x00':
                receive(0, 0, b'\xf1\x01\x00\x00')

    data = SimpleNamespace(RX_COUNT=Count(), RX_32=Receive(), TX_FREE=SimpleNamespace(val=4096),
                           TX_32=Transmit())
    monkeypatch.setattr(dockchannel, 'DockChannelDataRegs', lambda *args: data)
    monkeypatch.setattr(mtp, 'prepare_firmware', lambda blob, iface: b'firmware')
    ticks = iter(index / 1000 for index in range(100000))
    rx_data = bytearray()

    def read_fifo(code, register, buffer, words):
        assert words > 0
        rx_data[:] = queued[:words * 4]
        del queued[:words * 4]

    context = {'struct': struct, 'time': SimpleNamespace(monotonic=lambda: next(ticks)),
               'pathlib': SimpleNamespace(Path=lambda path: SimpleNamespace(read_bytes=lambda: b'')),
               'hashlib': SimpleNamespace(sha256=lambda blob: SimpleNamespace(hexdigest=lambda:
                   '7eb84312cb4ea74833ce9567ba8c92bc42a0c26c5c34d6bdfaa5cb65abfe4b26')),
               'u': SimpleNamespace(adt={'/arm-io/dockchannel-mtp':
                                        SimpleNamespace(get_reg=lambda idx: (0, 0))},
                                    memalign=lambda *args: 0, exec=read_fifo,
                                    iface=SimpleNamespace(writemem=lambda *args: None,
                                                          readmem=lambda addr, size: bytes(rx_data[:size]))),
               'p': SimpleNamespace(dc_cvac=lambda *args: None),
               'wait_until': lambda condition, service: condition()}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), context)
    result = {}
    asc = SimpleNamespace(work=lambda: None, ioalloc=lambda size: (0x100000, 0x200000))
    if not fresh_actuator_ready:
        with pytest.raises(TimeoutError, match='Actuator DeviceReady'):
            context['probe_mtp'](asc, keyboard, result, 'firmware', 1)
        assert not result['actuator_ready']
        assert 'touchpad_off_acknowledged' not in result
        assert 'firmware_upload_attempted' not in result
        return
    if isinstance(reject_off_phase, str):
        with pytest.raises(ValueError, match='dimensions'):
            context['probe_mtp'](asc, keyboard, result, 'firmware', 1)
        assert 'touchpad_off_acknowledged' not in result
        assert result['touchpad_trial_active']
        return
    if reject_off_phase is None:
        context['probe_mtp'](asc, keyboard, result, 'firmware', 1)
        assert result['touchpad_off_acknowledged']
        assert [request[4] for _, request in commands[-2:]] == [0, 1]
    else:
        with pytest.raises(RuntimeError):
            context['probe_mtp'](asc, keyboard, result, 'firmware', 1)
        assert 'touchpad_off_acknowledged' not in result
        assert commands[-1][1][4] == reject_off_phase
    assert result['touchpad_trial_active']
    assert result['touchpad_ready']
    assert result['actuator_ready']
    assert result['sensor_dimensions'] == dict(width=15600, height=9600, min_x=-7456,
                                               min_y=-163, max_x=7976, max_y=9283)
    assert result['touchpad_reports']
    assert [counter for counter, _ in commands] == list(range(len(commands)))
    assert result['keyboard_ready'] == keyboard
    if keyboard:
        keyboard_index = next(index for index, (_, request) in enumerate(commands)
                              if request == b'\xb4\x02')
        assert commands[keyboard_index - 1][1][:5] == b'\x40\x02\x01\x02\x01'
        assert result['keyboard_enable_counter'] == commands[keyboard_index][0]


@pytest.mark.parametrize('offset', [1, 2, 3, 4, 5, 6, 7, 13, 31])
def test_capture_deadline_finishes_only_the_partial_packet(offset):
    from m1n1.fw.mtp import decode_packet, RXMessage, InitMsg

    source = Path(__file__).parents[2] / 'proxyclient/experiments/t6040_iop.py'
    tree = ast.parse(source.read_text())
    probe = next(node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == 'probe_mtp')
    capture = next(node for node in probe.body
                   if isinstance(node, ast.FunctionDef) and node.name == 'capture')
    capture.body = [node for node in capture.body if not isinstance(node, ast.Nonlocal)]
    message = b'\x01' + bytes(9)
    packet = encode_packet(2, 0, struct.pack('<HHI', 0, len(message), 0) + message)
    pending = bytearray(packet[:offset])
    queued = bytearray(packet[offset:] + packet)
    rx_data = bytearray()

    class Count:
        @property
        def val(self):
            return len(queued)

    class ReceiveByte:
        @property
        def DATA(self):
            return queued.pop(0)

    def read_fifo(code, register, buffer, words):
        assert words > 0
        rx_data[:] = queued[:words * 4]
        del queued[:words * 4]

    ticks = iter([0] + [2 + index / 100 for index in range(100)])
    result = dict(packets=[], keyboard_reports=[])
    context = dict(struct=struct, time=SimpleNamespace(monotonic=lambda: next(ticks)),
                   pending=pending, asc=SimpleNamespace(work=lambda: None),
                   data=SimpleNamespace(RX_COUNT=Count(), RX_8=ReceiveByte()),
                   u=SimpleNamespace(exec=read_fifo,
                                     iface=SimpleNamespace(readmem=lambda addr, size: bytes(rx_data[:size]))),
                   rx_base=0, rx_buffer=0, rx_copy='', decode_packet=decode_packet,
                   RXMessage=RXMessage, InitMsg=InitMsg, result=result,
                   expected_reply=None, image=None, keyboard=False, feature_pending=False)
    exec(compile(ast.Module(body=[capture], type_ignores=[]), str(source), 'exec'), context)
    context['capture'](2)
    assert not pending
    assert queued == packet
    assert len(result['packets']) == 1
    assert result['keyboard_reports'] == [message.hex()]
