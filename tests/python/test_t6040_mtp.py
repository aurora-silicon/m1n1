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
    context = {'struct': struct, 'time': SimpleNamespace(monotonic=lambda: next(ticks)),
               'u': SimpleNamespace(adt={'/arm-io/dockchannel-mtp':
                                        SimpleNamespace(get_reg=lambda idx: (0, 0))}),
               'wait_until': lambda condition, service: condition()}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), context)
    result = {}
    with pytest.raises(ValueError, match='replies remain queued'):
        context['probe_mtp'](SimpleNamespace(work=lambda: None), True, result)
    assert result['initialization'][0]['name'] == 'keyboard'
    assert not result['keyboard_enable_ack']
    assert not result['keyboard_ready']
    assert not hasattr(data, 'TX_32')  # Reaching the transmit loop would fail.


@pytest.mark.parametrize('keyboard', [False, True])
@pytest.mark.parametrize('reject_off_phase', [None, 0, 1])
def test_touchpad_shutdown_requires_both_off_replies(monkeypatch, keyboard, reject_off_phase):
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

    for iface, name in ((1, b'multi-touch'), (2, b'keyboard')):
        init = (b'\xf0\x01\x00' + bytes([iface]) + name.ljust(16, b'\0') + bytes(2)
                + struct.pack('<HH', 2, 6) + bytes(6))
        receive(0, iface, init)

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
            elif request[:5] == b'\x40\x02\x01\x02\x00':
                receive(0, 0, b'\xf1\x01\x00\x00')

    data = SimpleNamespace(RX_COUNT=Count(), RX_32=Receive(), TX_FREE=SimpleNamespace(val=4096),
                           TX_32=Transmit())
    monkeypatch.setattr(dockchannel, 'DockChannelDataRegs', lambda *args: data)
    monkeypatch.setattr(mtp, 'prepare_firmware', lambda blob, iface: b'firmware')
    ticks = iter(index / 1000 for index in range(100000))
    context = {'struct': struct, 'time': SimpleNamespace(monotonic=lambda: next(ticks)),
               'pathlib': SimpleNamespace(Path=lambda path: SimpleNamespace(read_bytes=lambda: b'')),
               'hashlib': SimpleNamespace(sha256=lambda blob: SimpleNamespace(hexdigest=lambda:
                   '7eb84312cb4ea74833ce9567ba8c92bc42a0c26c5c34d6bdfaa5cb65abfe4b26')),
               'u': SimpleNamespace(adt={'/arm-io/dockchannel-mtp':
                                        SimpleNamespace(get_reg=lambda idx: (0, 0))},
                                    iface=SimpleNamespace(writemem=lambda *args: None)),
               'p': SimpleNamespace(dc_cvac=lambda *args: None),
               'wait_until': lambda condition, service: condition()}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), context)
    result = {}
    asc = SimpleNamespace(work=lambda: None, ioalloc=lambda size: (0x100000, 0x200000))
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
    assert result['touchpad_reports']
    assert [counter for counter, _ in commands] == list(range(len(commands)))
    assert result['keyboard_ready'] == keyboard
