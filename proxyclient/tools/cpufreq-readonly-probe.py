#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Inspect live Apple CPU-cluster P-state registers without changing them."""

import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

from m1n1.setup import *  # noqa: F403


CLUSTER_PSTATE = 0x20020
PSTATE_BUSY = 1 << 31
PSTATE_APSC_BUSY = 1 << 7
PSTATE_DESIRED1 = 0x1F
PSTATE_DESIRED2 = 0xF << 12


def main() -> int:
    clusters = {}
    for cpu in u.adt["/cpus"]:  # noqa: F405
        cluster_id = int(cpu.cluster_id)
        acc_base, acc_size = map(int, cpu.acc_impl_reg)
        current = clusters.get(cluster_id)
        record = (acc_base, acc_size, str(cpu.cluster_type))
        if current is not None and current != record:
            raise RuntimeError(
                f"cluster {cluster_id} has inconsistent acc-impl-reg entries: "
                f"{current!r} vs {record!r}"
            )
        clusters[cluster_id] = record

    if not clusters:
        print("CPUFREQ_PROBE no clusters found", flush=True)
        return 2

    for cluster_id, (base, size, cluster_type) in sorted(clusters.items()):
        if size < CLUSTER_PSTATE + 8:
            print(
                f"CPUFREQ_PROBE cluster={cluster_id} type={cluster_type} "
                f"base=0x{base:x} size=0x{size:x} pstate=out-of-range",
                flush=True,
            )
            return 3

        value = int(p.read64(base + CLUSTER_PSTATE))  # noqa: F405
        print(
            f"CPUFREQ_PROBE cluster={cluster_id} type={cluster_type} "
            f"base=0x{base:x} size=0x{size:x} reg=0x{value:016x} "
            f"desired1={value & PSTATE_DESIRED1} "
            f"desired2={(value & PSTATE_DESIRED2) >> 12} "
            f"busy={int(bool(value & PSTATE_BUSY))} "
            f"apsc_busy={int(bool(value & PSTATE_APSC_BUSY))}",
            flush=True,
        )

    pmgr = u.adt["/arm-io/pmgr"]  # noqa: F405
    for name, raw in sorted(pmgr._properties.items()):
        lowered = name.lower()
        if "voltage-state" not in lowered and "perf" not in lowered:
            continue
        try:
            length = len(raw)
        except TypeError:
            length = -1
        print(f"CPUFREQ_ADT property={name} length={length}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
