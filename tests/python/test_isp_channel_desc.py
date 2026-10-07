import struct

import pytest

from m1n1.fw.isp import ISPIPCChanTableDescEntry


@pytest.mark.parametrize("iova", [0x1804700, 0x10001804700])
def test_channel_descriptor_preserves_address(iova):
    # Build the wire record independently of the Construct schema.
    record = bytearray(0x100)
    record[:8] = b"TERMINAL"
    struct.pack_into("<IIIIQ", record, 0x40, 2, 0, 768, 0, iova)
    struct.pack_into("<II", record, 0x58, 0x12345678, 0xabcdef01)

    desc = ISPIPCChanTableDescEntry.parse(record)
    assert desc.name == "TERMINAL"
    assert (desc.type, desc.src, desc.num) == (2, 0, 768)
    assert desc.iova == iova
    assert ISPIPCChanTableDescEntry.sizeof() == 0x100


@pytest.mark.parametrize("iova", [0x1810700, 0x10001810700])
def test_channel_descriptor_encodes_address(iova):
    record = ISPIPCChanTableDescEntry.build(dict(
        name="IO", type=0, src=1, num=8, pad=0, iova=iova))
    assert len(record) == 0x100
    assert record[:0x40] == b"IO" + bytes(0x3e)
    assert struct.unpack_from("<IIIIQ", record, 0x40) == (0, 1, 8, 0, iova)
    assert record[0x58:] == bytes(0xa8)
