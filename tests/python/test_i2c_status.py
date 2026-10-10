from types import SimpleNamespace

import pytest

from m1n1.hw.i2c import I2C, R_SMSTA


class Peer:
    BASE = 0x429014000

    def __init__(self, status, arriving=0, fail_read=False, fail_write=False):
        self.status = status
        self.arriving = arriving
        self.fail_read = fail_read
        self.fail_write = fail_write
        self.calls = []
        self.proxy = self.iface = None
        self.adt = {"/arm-io/i2c1": SimpleNamespace(get_reg=lambda _: (self.BASE, 0x1000))}

    def read(self, address, width):
        assert (address, width) == (self.BASE + 0x14, 32)
        self.calls.append(("read", address))
        if self.fail_read:
            raise TimeoutError("status read failed")
        value = self.status
        self.status |= self.arriving
        return value

    def write(self, address, value, width):
        assert (address, width) == (self.BASE + 0x14, 32)
        self.calls.append(("write", address, value))
        if self.fail_write:
            raise TimeoutError("status write failed")
        self.status &= ~value


def test_new_error_after_snapshot_is_not_cleared():
    status = int(R_SMSTA(XEN=1))
    arriving = int(R_SMSTA(MTN=1))
    peer = Peer(status, arriving=arriving)
    I2C(peer, "/arm-io/i2c1").clear_status()
    assert peer.status == arriving
    assert peer.calls == [("read", peer.BASE + 0x14),
                          ("write", peer.BASE + 0x14, status)]


@pytest.mark.parametrize("status", [0, 0x10106, 0x20010106, 0x08010106])
def test_observed_bits_including_undefined_flags_are_preserved(status):
    peer = Peer(status)
    I2C(peer, "/arm-io/i2c1").clear_status()
    assert peer.calls[-1] == ("write", peer.BASE + 0x14, status)
    assert peer.status == 0


def test_active_transfer_refuses_status_write():
    status = int(R_SMSTA(XIP=1, XEN=1))
    peer = Peer(status)
    with pytest.raises(Exception, match="transfer in progress"):
        I2C(peer, "/arm-io/i2c1").clear_status()
    assert peer.status == status
    assert peer.calls == [("read", peer.BASE + 0x14)]


@pytest.mark.parametrize("failure", ["read", "write"])
def test_failed_status_access_has_no_retry(failure):
    peer = Peer(0x08010106, fail_read=failure == "read", fail_write=failure == "write")
    with pytest.raises(TimeoutError):
        I2C(peer, "/arm-io/i2c1").clear_status()
    assert len(peer.calls) == (1 if failure == "read" else 2)
