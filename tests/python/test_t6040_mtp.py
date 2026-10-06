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
