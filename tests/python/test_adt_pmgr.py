# SPDX-License-Identifier: MIT

import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "proxyclient"))

from m1n1.adt import ADTNode


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
