import json
from pathlib import Path
import struct

import pytest

from m1n1.fw.isp.isp_profile import T6040_25G76 as PROFILE
FIXTURE = json.loads((Path(__file__).parent / 'fixtures/isp_t6040_25g76.json').read_text())


def segments(**changes):
    values = dict(firmware_sha256=FIXTURE['firmware_sha256'],
                  segment_names=FIXTURE['segment_names'],
                  segment_ranges=bytes.fromhex(FIXTURE['segment_ranges_hex']))
    values.update(changes)
    return PROFILE.validate_segments(**values)


def boot():
    return PROFILE.boot_layout(segments(), args_offset=FIXTURE['args_offset'],
                               extra_size=FIXTURE['extra_size'])


def channels(raw=None, **changes):
    values = dict(table_iova=int(FIXTURE['channel_table_iova'], 16), count=7, boot=boot())
    values.update(changes)
    return PROFILE.parse_channels(bytes.fromhex(FIXTURE['channel_table_hex']) if raw is None else raw, **values)


def test_captured_layout():
    text, data = segments()
    assert text.remap == 1 << 40
    assert data.virtual == 0xca8000 and data.size == 0x1344000
    layout = boot()
    assert layout.ipc_iova == 0x10001ff0000
    assert layout.args_iova == 0x10001ffef80
    assert layout.command_iova == 0x10001fff250
    parsed = channels()
    assert len(parsed) == 7
    assert next(c for c in parsed if c.name == 'BUF_T2H').count == 64


@pytest.mark.parametrize('changes', [dict(firmware_sha256='0'*64), dict(segment_names='__DATA;__TEXT'), dict(segment_ranges=b'')])
def test_identity_and_extent(changes):
    with pytest.raises(ValueError):
        segments(**changes)


@pytest.mark.parametrize('offset,value', [(0, 1), (8, 0x4000), (16, 0), (24, 0x4000), (28, 0), (32, 0x1000)])
def test_segment_mutations(offset, value):
    raw = bytearray.fromhex(FIXTURE['segment_ranges_hex'])
    struct.pack_into('<I' if offset in (24,28) else '<Q', raw, offset, value)
    with pytest.raises(ValueError):
        segments(segment_ranges=bytes(raw))


@pytest.mark.parametrize('args,extra', [(-1,0x4000), (1,0x4000), (0x1c000,0x4000), (0,0), (0,1), (0,0x7004000)])
def test_negotiated_boot_bounds(args, extra):
    with pytest.raises(ValueError):
        PROFILE.boot_layout(segments(), args_offset=args, extra_size=extra)


@pytest.mark.parametrize('offset,fmt,value', [(0,'B',0), (0x40,'I',3), (0x48,'I',0), (0x50,'Q',0x700000), (0x50,'Q',0x10001ff0701)])
def test_channel_mutations(offset, fmt, value):
    raw = bytearray.fromhex(FIXTURE['channel_table_hex'])
    struct.pack_into('<'+fmt,raw,offset,value)
    with pytest.raises(ValueError):
        channels(bytes(raw))


def test_channel_table_extent_and_overlap():
    raw = bytes.fromhex(FIXTURE['channel_table_hex'])
    with pytest.raises(ValueError):
        channels(raw[:-1])
    with pytest.raises(ValueError):
        channels(table_iova=0)
    with pytest.raises(ValueError):
        channels(count=0)
    modified = bytearray(raw)
    struct.pack_into('<Q',modified,0x100+0x50,struct.unpack_from('<Q',raw,0x50)[0])
    with pytest.raises(ValueError):
        channels(bytes(modified))


@pytest.mark.parametrize("region", ["table", "args", "command"])
def test_channel_refuses_control_storage(region):
    layout = boot()
    address = {"table": int(FIXTURE['channel_table_iova'], 16),
               "args": layout.args_iova, "command": layout.command_iova}[region]
    # A one-slot ring intersects the reserved region even when its start is
    # rounded down to the required slot alignment.
    raw = bytearray.fromhex(FIXTURE['channel_table_hex'])
    struct.pack_into('<I', raw, 0x48, 1)
    struct.pack_into('<Q', raw, 0x50, address & ~0x3f)
    with pytest.raises(ValueError, match="overlaps IPC control storage"):
        channels(bytes(raw))


@pytest.mark.parametrize('region', ['args_iova', 'command_iova'])
def test_table_cannot_overlap_boot_storage(region):
    with pytest.raises(ValueError, match='table overlaps boot storage'):
        channels(table_iova=getattr(boot(), region))
