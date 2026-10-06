import struct

import pytest

from m1n1.fw.mtp import checksum, decode_packet, encode_packet, MTPProtocol


# Cold J616s keyboard enable ACK, received over DockChannel.
KEYBOARD_ACK = bytes.fromhex('08110c00000000008000010000000000b4020000c3ebf2ff')


def test_captured_keyboard_ack():
    iface, kind, seq, payload = decode_packet(KEYBOARD_ACK)
    assert (iface, kind, seq) == (0, 0x11, 0)
    assert payload[:9] == bytes.fromhex('8000010000000000b4')


def test_keyboard_command_matches_live_exchange():
    payload = struct.pack('<HHI', 0x80, 2, 0) + b'\xb4\x02'
    packet = encode_packet(0, 0, payload)
    assert packet.hex() == '08110c00000000008000020000000000b4020000c3ebf1ff'
    assert decode_packet(packet) == (0, 0x11, 0, payload + b'\x00\x00')


@pytest.mark.parametrize('offset', [0, 1, 4, 8, 12, 23])
def test_corrupted_packet_rejected(offset):
    packet = bytearray(KEYBOARD_ACK)
    packet[offset] ^= 1
    with pytest.raises(ValueError):
        decode_packet(packet)


@pytest.mark.parametrize('packet', [b'', KEYBOARD_ACK[:11], KEYBOARD_ACK[:-4],
                                    KEYBOARD_ACK + b'\x00' * 4])
def test_incomplete_or_extra_data_rejected(packet):
    with pytest.raises(ValueError):
        decode_packet(packet)


@pytest.mark.parametrize('size', [1, 2, 4, 16])
def test_invalid_header_length_rejected(size):
    packet = bytearray(KEYBOARD_ACK)
    packet[0] = size
    packet[-4:] = struct.pack('<I', checksum(packet[:-4]))
    with pytest.raises(ValueError, match='length'):
        decode_packet(packet)


def test_legacy_send_uses_shared_encoder():
    writes = []
    work = []
    proto = object.__new__(MTPProtocol)
    proto.dockchannel = type('Channel', (), {'write': staticmethod(writes.append)})()
    proto.mtp = type('ASC', (), {'work_pending': staticmethod(lambda: work.append(True))})()
    proto.send(2, 7, b'abc')
    assert writes == [encode_packet(2, 7, b'abc')]
    assert work == [True]


@pytest.mark.parametrize(('offset', 'value'), [(1, 0xfe), (6, 1), (7, 1)])
def test_unsupported_header_rejected_with_valid_checksum(offset, value):
    packet = bytearray(KEYBOARD_ACK)
    packet[offset] = value
    packet[-4:] = struct.pack('<I', checksum(packet[:-4]))
    with pytest.raises(ValueError, match='header'):
        decode_packet(packet)
