# SPDX-License-Identifier: MIT
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "proxyclient"))
from m1n1.pmgr2 import PMGR2Tables
from m1n1.asc_v8 import MailboxLayout, decode_status


def tables(**overrides):
    device = bytearray(48)
    struct.pack_into("<I", device, 16, 0x01000024)
    struct.pack_into("<H", device, 26, 352)
    device[32:36] = b"TEST"
    service = bytearray(36)
    struct.pack_into("<I", service, 0, 3)
    struct.pack_into("<H", service, 18, 170)
    service[20:24] = b"SVC0"
    values = dict(compatible=["pmgr2,t8152"], devices=bytes(device), service=bytes(service),
                  ps_groups=struct.pack("<IIIIII", 0, 0, 0, 1, 0x100, 0),
                  virt_service_start=170, reg=[None, None],
                  get_reg=lambda i: (0x300000000 + i * 0x10000, 0x1000))
    values.update(overrides)
    return PMGR2Tables(SimpleNamespace(**values))


def test_pmgr2_namespaces_and_register_resolution():
    t = tables()
    d = t.devices[0]
    assert (d.id, d.name, d.group, d.offset) == (352, "TEST", 1, 0x24)
    assert t.device_register(d) == 0x300010124
    assert t.service_description(6) == dict(id=6, kind="implicit", name=None)
    assert t.service_description(170) == dict(id=170, kind="explicit", name="SVC0")
    assert t.services[0].flags == 3
    with pytest.raises(ValueError):
        t.service_description(352)  # device IDs do not imply a service ID


@pytest.mark.parametrize("field,size", [("devices", 48), ("service", 36), ("ps_groups", 12)])
def test_reject_truncated_tables(field, size):
    with pytest.raises(ValueError):
        tables(**{field: bytes(size - 1)})


def test_reject_unproven_compatible_and_duplicate_ids():
    with pytest.raises(ValueError):
        tables(compatible=["pmgr1,t8142"])
    for field, raw in [("devices", tables().devices[0].raw), ("service", tables().services[0].raw)]:
        with pytest.raises(ValueError, match="duplicate"):
            tables(**{field: raw * 2})


@pytest.mark.parametrize("group,offset", [(3, 0), (1, 0x1000), (1, 3)])
def test_reject_invalid_register_location(group, offset):
    raw = bytearray(tables().devices[0].raw)
    struct.pack_into("<I", raw, 16, group << 24 | offset)
    t = tables(devices=bytes(raw))
    with pytest.raises(ValueError):
        t.device_register(t.devices[0])


@pytest.mark.parametrize("aperture", [0, 1])
@pytest.mark.parametrize("alias", [False, True])
def test_mailbox_layout(aperture, alias):
    m = MailboxLayout(0x316000000, 3, aperture, alias)
    expected = 0x316000000 + 0x300 + aperture * 0x80 + alias * 0x10000
    assert (m.read_data, m.write_data, m.status) == (expected, expected + 16, expected + 32)


def test_mailbox_status_exhaustive_fields():
    for capacity in range(16):
        for readable in range(16):
            for writable in range(16):
                s = decode_status(capacity << 16 | writable << 8 | readable)
                assert s["rx_full"] == (readable == capacity)
                assert s["tx_empty"] == (writable == capacity)
                assert s["tx_full"] == (writable == 0)
                assert s["rx_empty"] == (readable == 0)


def test_mailbox_reject_invalid_inputs():
    for args in [(-1, 0, 0), (0, -1, 0), (0, 256, 0), (0, 0, 2)]:
        with pytest.raises(ValueError):
            MailboxLayout(*args)
    for value in [-1, 0x100000000]:
        with pytest.raises(ValueError):
            decode_status(value)
