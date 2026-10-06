# SPDX-License-Identifier: MIT
import struct

import pytest

from m1n1.fw.mtp import prepare_firmware, check_control_reply, RXMessage


def hidf(payload=b'abc\0def', offset=3):
    return struct.pack('<4sIIII12x', b'HIDF', 1, 32, len(payload), offset) + payload


def test_firmware_upload_excludes_header_and_patches_only_interface():
    assert prepare_firmware(hidf(), 1) == b'abc\1def'


@pytest.mark.parametrize('data', [b'', hidf()[:31], hidf()[:-1], hidf(offset=7),
                                hidf(payload=b'abcdefg')])
def test_invalid_firmware_cannot_be_prepared(data):
    with pytest.raises(ValueError):
        prepare_firmware(data, 1)


@pytest.mark.parametrize('iface', [0, 256, -1])
def test_invalid_interface_rejected(iface):
    with pytest.raises(ValueError):
        prepare_firmware(hidf(), iface)


# Full echoed Off/Will reply from the cold J616s firmware trial.
POWER_REQUEST = bytes.fromhex('400201000000000000')
POWER_REPLY = bytes.fromhex('8000090000000000400201000000000000000000')


def test_live_power_reply_echo_and_counter_match():
    check_control_reply(POWER_REQUEST, 2, 2, RXMessage.parse(POWER_REPLY))


@pytest.mark.parametrize('counter, command', [(1, POWER_REQUEST),
    (2, bytes.fromhex('400201020000000000')), (2, bytes.fromhex('400201000100000000'))])
def test_old_counter_or_other_power_phase_rejected(counter, command):
    with pytest.raises(ValueError):
        check_control_reply(command, 2, counter, RXMessage.parse(POWER_REPLY))


def test_nonzero_status_rejected():
    reply = RXMessage.parse(POWER_REPLY)
    reply.hdr.retcode = 0xe00002bc
    with pytest.raises(RuntimeError, match='failed'):
        check_control_reply(POWER_REQUEST, 2, 2, reply)
