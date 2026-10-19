"""Reject reload writes that overlap preserved firmware or live memory."""

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "proxyclient"))
from m1n1.adt import ADTNode
from m1n1.chainload import check_span, preserved_regions


def test_preserved_ram_boundaries():
    ram_base = 0x10003A10000
    ram_size = 0x7DB5F0000
    old_base = 0x10004728000
    oslog = 0x10004A88000
    regions = [("DCP __OS_LOG", oslog, 0x25000)]
    with pytest.raises(ValueError, match="DCP __OS_LOG"):
        check_span(old_base, 0x364000, ram_base, ram_size, regions)
    with pytest.raises(ValueError, match="DCP __OS_LOG"):
        check_span(old_base, 0x370000, ram_base, ram_size, regions)
    assert check_span(old_base, oslog - old_base, ram_base, ram_size, regions) == oslog
    fresh = 0x1000D918000
    assert check_span(fresh, 0x1000000, ram_base, ram_size, regions) == fresh + 0x1000000
    with pytest.raises(ValueError, match="RAM"):
        check_span(ram_base + ram_size - 1, 2, ram_base, ram_size, regions)
    with pytest.raises(ValueError, match="RAM"):
        check_span(ram_base - 1, 1, ram_base, ram_size, regions)
    with pytest.raises(ValueError):
        check_span(fresh, 0, ram_base, ram_size, regions)
    with pytest.raises(ValueError):
        check_span(fresh, 1, ram_base, ram_size, [("bad", (1 << 64) - 1, 2)])
    assert check_span(fresh, 1, ram_base, ram_size, [("empty", 0, 0)]) == fresh + 1
    assert check_span(fresh, 1, ram_base, ram_size, [("alias", 0x31004728000, 0x360000)]) == fresh + 1


def test_deep_firmware_region_is_protected():
    root = ADTNode()
    root.name = "device-tree"

    def child(parent, name):
        node = ADTNode(path=parent._path + "/", parent=parent)
        node.name = name
        parent._children.append(node)
        return node

    memory_map = child(child(root, "chosen"), "memory-map")
    memory_map._properties["SEPFW"] = [0x1000, 0x100]
    nub = child(child(child(root, "arm-io"), "dcp"), "iop-dcp-nub")
    nub.segment_ranges = struct.pack("<QQQII", 0x4000, 0x8000, 0xC000, 0x100, 0)
    regions = preserved_regions(root)
    with pytest.raises(ValueError, match="iop-dcp-nub"):
        check_span(0x4000, 1, 0, 0x10000, regions)
    with pytest.raises(ValueError, match="SEPFW"):
        check_span(0x1000, 1, 0, 0x10000, regions)
    assert check_span(0x5000, 0x100, 0, 0x10000, regions) == 0x5100
    nub.segment_ranges += b"\x00"
    with pytest.raises(struct.error):
        preserved_regions(root)
