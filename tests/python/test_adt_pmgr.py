# SPDX-License-Identifier: MIT

import sys
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "proxyclient"))

from m1n1.adt import ADTNode, ADTNodeStruct, load_adt


def _device():
    return SimpleNamespace(
        id1=7,
        id2=700,
        parents_un=SimpleNamespace(
            u8id=SimpleNamespace(parents=[1, 2]),
            u16id=SimpleNamespace(parents=[100, 200]),
        ),
    )


def test_pmgr_u8id_accessors_use_initialized_public_flag():
    adt = SimpleNamespace(pmgr_u8id=True)
    dev = _device()

    assert ADTNode.pmgr_dev_get_id(adt, dev) == 7
    assert ADTNode.pmgr_dev_get_parents(adt, dev) == [1, 2]


def test_pmgr_u16id_accessors_use_initialized_public_flag():
    adt = SimpleNamespace(pmgr_u8id=False)
    dev = _device()

    assert ADTNode.pmgr_dev_get_id(adt, dev) == 700
    assert ADTNode.pmgr_dev_get_parents(adt, dev) == [100, 200]


def _node(name, props=None, children=None):
    values = {"name": name.encode() + b"\0", **(props or {})}
    return dict(property_count=len(values), child_count=len(children or []),
                properties=[dict(name=k, size=len(v), value=v) for k, v in values.items()],
                children=children or [])


def _tree(pmgr=None, siblings=None):
    children = ([] if pmgr is None else [pmgr]) + (siblings or [])
    return ADTNodeStruct.build(_node("device-tree", children=[_node("arm-io", children=children)]))


def test_pmgr2_loads_losslessly_and_leaves_child_records_opaque():
    opaque = bytes(range(96))
    raw = _tree(_node("pmgr", {"compatible": b"pmgr2,arch\0"}), [
        _node("pmgr-child", {"compatible": b"pmgr2,t8152\0", "devices": opaque})
    ])
    tree = load_adt(raw)
    assert tree.build() == raw
    assert tree["/arm-io/pmgr-child"].devices == opaque
    for method in (tree.pmgr_dev_get_id, tree.pmgr_dev_get_parents,
                   tree.pmgr_dev_get_block, tree.pmgr_dev_get_offset,
                   tree.pmgr_dev_get_addr):
        with pytest.raises(NotImplementedError, match="PMGR1"):
            method(_device())


@pytest.mark.parametrize("gates", [[], [6], [178, 86], [1, 2, 3, 4]])
def test_service_gates_remain_individual_u32_ids(gates):
    raw = _tree(siblings=[_node("consumer", {"service-gates": struct.pack("<" + "I" * len(gates), *gates)})])
    tree = load_adt(raw)
    assert list(tree["/arm-io/consumer"].service_gates or []) == gates
    assert tree.build() == raw


@pytest.mark.parametrize("pmgr", [
    None,
    _node("pmgr", {"compatible": b"pmgr1,t8103\0"}),
    _node("pmgr", {"compatible": b"pmgr1,t8103\0", "devices": bytes(48)}),
])
def test_adt_without_complete_legacy_pmgr_is_still_readable(pmgr):
    raw = _tree(pmgr)
    tree = load_adt(raw)
    assert tree.build() == raw
    assert tree.pmgr_u8id is None


@pytest.mark.parametrize("u8_ids", [True, False])
@pytest.mark.parametrize("table", ["ps-regs", "ps-groups"])
def test_legacy_pmgr_initialization_and_offsets(table, u8_ids):
    devices = bytearray(96)
    devices[3] = 7 if u8_ids else 0
    devices[51] = 8 if u8_ids else 0
    struct.pack_into("<H", devices, 26, 700)
    devices[10] = 2  # legacy psidx
    devices[16:19] = (0x38).to_bytes(3, "little")
    raw = _tree(_node("pmgr", {
        "compatible": b"pmgr1,t8142\0",
        "devices": bytes(devices),
        table: struct.pack("<III", 0, 0x100, 0xffffffff),
    }))
    tree = load_adt(raw)
    dev = tree["/arm-io/pmgr"].devices[0]
    assert tree.build() == raw
    assert tree.pmgr_dev_get_id(dev) == (7 if u8_ids else 700)
    assert tree.pmgr_dev_get_offset(dev) == (0x38 if table == "ps-groups" else 0x110)
