from io import BytesIO
from types import SimpleNamespace
import struct

import pytest

from m1n1.fw.afk.epic import EPICCategory
from m1n1.fw.aop.aopep import AOPAudioService
from m1n1.fw.aop.ipc import AudioProducerReport

# Captured J616s LP report: frame counter 639, write offset 7680.
CAPTURED = bytes.fromhex("6961706c080000c30100000000000000f5ac8110000000000ab0e97468000000946e7572676863706961706c66646e75310100003000000000000000000000007f02000000000000a26f81100000000002000000000000007f02000000000000001e000000000000")


def service():
    ep = SimpleNamespace(asc=SimpleNamespace(iface=None), name="audio")
    return AOPAudioService(ep)


def test_captured_producer_report_dispatch():
    audio = service()
    assert audio.handle_report(EPICCategory.REPORT, 0x20, 0, BytesIO(CAPTURED))
    rep = audio.last_producer_report
    assert (rep.devid, rep.subtype, rep.frame_count, rep.write_offset) == (
        "lpai", 0xc3000008, 639, 7680)
    assert AudioProducerReport.build(rep) == CAPTURED


def test_full_width_counters_and_extension_preserved():
    payload = bytearray(CAPTURED)
    struct.pack_into("<Q", payload, 0x40, (1 << 40) + 7)
    struct.pack_into("<Q", payload, 0x60, (1 << 32) + 9)
    payload.extend(b"extension")
    audio = service()
    assert audio.handle_report(EPICCategory.REPORT, 0x20, 2, BytesIO(payload))
    rep = audio.last_producer_report
    assert rep.frame_count == (1 << 40) + 7
    assert rep.write_offset == (1 << 32) + 9
    assert AudioProducerReport.build(rep) == bytes(payload)


@pytest.mark.parametrize("payload", [CAPTURED[:8], CAPTURED[:103],
                                     b"unknown!" + CAPTURED[8:]])
def test_unqualified_report_keeps_unknown_dispatch(payload):
    audio = service()
    assert not audio.handle_report(EPICCategory.REPORT, 0x20, 0, BytesIO(payload))
    assert audio.last_producer_report is None
