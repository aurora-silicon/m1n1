import json
import struct
from pathlib import Path

import pytest

from m1n1.fw.isp.isp_capture import T6040PreviewConfiguration


def profile():
    info, preset = bytearray(0x190), bytearray(0x120)
    struct.pack_into('<3I', info, 0, 0, 0x10d, 0)
    struct.pack_into('<4I', preset, 0, 0, 0x106, 0, 5)
    struct.pack_into('<I', info, 0x20, 0x958)
    struct.pack_into('<I', info, 0x60, 13)
    struct.pack_into('<I', info, 0x68, 0x4d00)
    struct.pack_into('<2H', preset, 0x10, 1920, 2160)
    return info, preset


def test_commands_match_successful_live_720p_capture():
    fixture = json.loads((Path(__file__).parent / 'fixtures/t6040_preview_commands.json').read_text())
    actual = []
    def query(name, opcode, size, params, **kwargs):
        assert kwargs == ({} if name in ('CH_META_POOL_CONFIG_SET', 'CH_OUTPUT_POOL_CONFIG_SET') else {'outsize': 0})
        payload = struct.pack('<II', 0, opcode) + struct.pack(f'<{len(params)}I', *params)
        assert len(payload) == size
        actual.append(dict(name=name, payload_hex=payload.hex()))
    c = T6040PreviewConfiguration(query)
    assert c.configure(*profile())['surface_size'] == 0x2a8000
    assert actual == fixture['commands']


@pytest.mark.parametrize('kind', ['short_info', 'short_preset', 'route', 'metadata', 'preset', 'sensor', 'count', 'index', 'header'])
def test_profile_rejected_before_any_command(kind):
    info, preset = profile()
    if kind == 'short_info': info = info[:0x138]
    if kind == 'short_preset': preset = preset[:0x13]
    if kind == 'route': info[0x138] = 1
    if kind == 'metadata': struct.pack_into('<I', info, 0x68, 1)
    if kind == 'preset': struct.pack_into('<H', preset, 0x10, 1280)
    if kind == 'sensor': struct.pack_into('<I', info, 0x20, 1)
    if kind == 'count': struct.pack_into('<I', info, 0x60, 5)
    if kind == 'index': struct.pack_into('<I', preset, 0xc, 4)
    if kind == 'header': struct.pack_into('<I', info, 4, 0)
    c = T6040PreviewConfiguration(lambda *args, **kwargs: pytest.fail('command'))
    with pytest.raises(ValueError): c.configure(info, preset)
    assert not c.attempted


def test_failed_configuration_cannot_be_retried():
    calls = []
    def query(*args, **kwargs):
        calls.append(args[0])
        raise TimeoutError('ambiguous command')
    c = T6040PreviewConfiguration(query)
    with pytest.raises(TimeoutError): c.configure(*profile())
    with pytest.raises(ValueError): c.configure(*profile())
    assert calls == ['FLICKER_SENSOR_SET']
