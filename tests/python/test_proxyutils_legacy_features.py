# SPDX-License-Identifier: MIT

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "proxyclient"))

from m1n1.proxyutils import legacy_cpu_features


def test_legacy_cpu_features_are_complete_and_conservative():
    features = legacy_cpu_features()

    assert features.apple_sysregs_unlocked is False
    assert features.actlr_el2 is False
    assert features.mmu_sprr is False
    assert features.fast_ipi is False
