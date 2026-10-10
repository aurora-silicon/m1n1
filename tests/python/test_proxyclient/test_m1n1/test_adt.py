# SPDX-License-Identifier: MIT
from types import SimpleNamespace

from proxyclient.m1n1.adt import ADTNode


def make_tree():
    root = ADTNode()
    root.name = "root"
    direct = root.create_node("direct")
    direct.reg = [SimpleNamespace(addr=0x9000, size=0x100)]
    bus = root.create_node("bus")
    bus.ranges = [SimpleNamespace(bus_addr=0, parent_addr=0x5000, size=0x1000)]
    bridge = bus.create_node("bridge")
    bridge.ranges = []
    leaf = bridge.create_node("leaf")
    leaf.reg = [SimpleNamespace(addr=0x100, size=0x100)]
    root.create_node("sibling")
    return root


def test_walk_tree_includes_each_level():
    root = make_tree()
    assert [node.name for node in root.walk_tree()] == [
        "root", "direct", "bus", "bridge", "leaf", "sibling"
    ]
    assert [node.name for node in root["bus"].walk_tree()] == ["bus", "bridge", "leaf"]


def test_addr_lookup_includes_direct_and_nested_devices():
    lookup = make_tree().build_addr_lookup()
    assert lookup.lookup(0x9000) == ("direct[0]", range(0x9000, 0x9100))
    assert lookup.lookup(0x5100) == ("leaf[0]", range(0x5100, 0x5200))
    assert lookup.lookup(0x5200)[0] == "unknown"
