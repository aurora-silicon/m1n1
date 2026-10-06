from io import BytesIO
from types import SimpleNamespace

import pytest

from m1n1.fw.afk.epic import EPICCategory, EPICEndpoint, EPICService, EPICSubtype
from m1n1.fw.aop.ipc import AudioPropertyFormat, GetDeviceProp


def test_device_property_reply_uses_request_parameters():
    call = GetDeviceProp(devid='lpai', modifier=200)
    service = SimpleNamespace(last_call=call)
    EPICService.handle_reply(service, EPICCategory.REPLY,
                             EPICSubtype.RETCODE_WITH_PAYLOAD, 0,
                             BytesIO(bytes.fromhex('0000000004000000656c6469')))
    assert service.reply is call.rets
    assert service.reply.retcode == 0
    assert service.reply.len == 4
    assert service.reply.data == 'idle'


def test_failed_roundtrip_releases_pending_call():
    def fail(*args, **kwargs):
        raise RuntimeError('transport failed')

    service = SimpleNamespace(send_notify=fail)
    endpoint = SimpleNamespace(serv_map={'audio': service})
    with pytest.raises(RuntimeError, match='transport failed'):
        EPICEndpoint.send_roundtrip(endpoint, 'audio', GetDeviceProp(devid='lpai', modifier=200))
    assert service.last_call is None


@pytest.mark.parametrize('reply, retcode, payload', [
    (bytes.fromhex('0000000004000000656c6469'), 0, b'eldi'),
    (bytes.fromhex('f00200e0'), 0xe00002f0, None),
])
def test_format_reply_uses_response_reader(reply, retcode, payload):
    call = AudioPropertyFormat(devid='adpx')
    service = SimpleNamespace(last_call=call)
    EPICService.handle_reply(service, EPICCategory.REPLY,
                             EPICSubtype.RETCODE_WITH_PAYLOAD, 0,
                             BytesIO(reply))
    assert service.reply is call.rets
    assert service.reply.retcode == retcode
    assert service.reply.data == payload
