from io import BytesIO
import struct

import pytest
from construct import StreamError

from m1n1.fw.aop.ipc import AudioPropertyFormat, GetDeviceProp


@pytest.mark.parametrize('modifier', [200, 210, 212])
@pytest.mark.parametrize('retcode', [0xe00002c2, 0xe00002c7, 0xe00002f0])
def test_property_error_has_no_configuration_payload(modifier, retcode):
    call = GetDeviceProp(devid='pdm0', modifier=modifier)
    call.read_resp(BytesIO(struct.pack('<I', retcode)))
    assert call.rets.retcode == retcode
    assert call.rets.len is None
    assert call.rets.data is None


def test_structured_property_reply_preserves_request_context():
    call = GetDeviceProp(devid='lpai', modifier=301)
    words = (0x48000014, 16000, 3, 3)
    call.read_resp(BytesIO(struct.pack('<IIIIII', 0, 16, *words)))
    assert call.rets.len == 16
    assert tuple(call.rets.data[name] for name in ('unk1', 'unk2', 'unk3', 'unk4')) == words


@pytest.mark.parametrize('reply', [
    struct.pack('<I', 0),
    struct.pack('<II', 0, 4),
    struct.pack('<II', 0, 3) + b'eldi',
])
def test_truncated_successful_property_reply_is_rejected(reply):
    call = GetDeviceProp(devid='lpai', modifier=200)
    with pytest.raises(StreamError):
        call.read_resp(BytesIO(reply))


def test_generic_property_payload_is_bounded_by_declared_length():
    call = GetDeviceProp(devid='hpai', modifier=203)
    call.read_resp(BytesIO(struct.pack('<II', 0, 4) + b'data' + b'trailing'))
    assert call.rets.retcode == 0
    assert call.rets.data == b'data'


@pytest.mark.parametrize('devid, payload', [
    ('lpai', bytes.fromhex('6e69616d0000000000fa000000000000800c000000000000')),
    ('a2px', b'eldi'),
    ('adpx', b'eldi'),
    ('dvpx', bytes(4)),
])
def test_format_reply_preserves_native_payload(devid, payload):
    call = AudioPropertyFormat(devid=devid)
    call.read_resp(BytesIO(struct.pack('<II', 0, len(payload)) + payload))
    assert call.rets.retcode == 0
    assert call.rets.len == len(payload)
    assert call.rets.data == payload
    assert 'sample_rate' not in call.rets


def test_format_error_has_no_payload():
    call = AudioPropertyFormat(devid='hpai')
    call.read_resp(BytesIO(struct.pack('<I', 0xe00002f0)))
    assert call.rets.retcode == 0xe00002f0
    assert call.rets.len is None
    assert call.rets.data is None
