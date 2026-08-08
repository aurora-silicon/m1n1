#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Stopped-CPU AIC2 target-code probe.

This is deliberately an experiment, not a guest preload.  Run it only from
the run_guest shell before ``hv.start()``.  At that point m1n1 owns the
secondary CPUs and the Windows guest has not executed any code.

The probe uses one ADT-unowned, non-reserved software line.  It masks and
clears the line before changing the target nibble, preserves every other
configuration bit, masks DAIF on every stopped CPU, emits SW_SET, and reads
the CPU-local EVENT register through a tiny EL2 helper.  The original line
configuration and DAIF values are restored in ``finally``.
"""

import time

from m1n1 import asm


# J414s has 1961 implemented AIC2 lines.  m1n1/Mu reserve [1913, 1961) for
# timer reflection; 1800 is outside that reservation and has no ADT owner.
PROBE_IRQ = 1800
MAX_CPUS = 32

AIC_CAP0 = 0x0004
AIC_MAXNUMIRQ = 0x000C
AIC_IRQ_CFG = 0x2000
AIC_SW_SET = 0x6000
AIC_SW_CLEAR = 0x6200
AIC_MASK_SET = 0x6400
AIC_MASK_CLEAR = 0x6600
AIC_HW_STATE = 0x6800
AIC_EVENT = 0xC000
AIC_CONFIG_PREFER_PCPU = 1 << 28
AIC_BANK_COUNT = 5

AIC_EVENT_TYPE_HW = 1
AIC_EVENT_TYPE_SHIFT = 16
AIC_EVENT_TYPE_MASK = 0xFF
AIC_EVENT_IRQ_MASK = 0xFFFF
DAIF_MASK = 0x1C0


def _event_fields(raw):
    return {
        "raw": int(raw),
        "die": (int(raw) >> 24) & 0xFF,
        "type": (int(raw) >> AIC_EVENT_TYPE_SHIFT) & AIC_EVENT_TYPE_MASK,
        "irq": int(raw) & AIC_EVENT_IRQ_MASK,
    }


def _cpu_rows(compiled):
    rows = [(0, int(u.mrs("MPIDR_EL1")))]
    for cpu in range(1, MAX_CPUS):
        if p.smp_is_alive(cpu):
            mpidr = int(p.smp_call_sync(cpu, compiled.read_mpidr))
            rows.append((cpu, mpidr))
    return rows


def _read32_code():
    code = u.malloc(0x1000)
    compiled = asm.ARMAsm(
        """
        read32:
            ldr w0, [x0]
            ret

        write32:
            str w1, [x0]
            dsb sy
            ret

        read_mpidr:
            mrs x0, MPIDR_EL1
            ret

        mask_daif:
            mrs x0, DAIF
            msr DAIFSet, 0xf
            isb
            ret

        restore_daif:
            msr DAIF, x0
            isb
            ret
        """,
        code,
    )
    p.iface.writemem(code, compiled.data)
    p.dc_cvau(code, len(compiled.data))
    p.ic_ivau(code, len(compiled.data))
    return compiled


def _remote_read32(compiled, cpu, address):
    return int(p.smp_call_sync(cpu, compiled.read32, address)) & 0xFFFFFFFF


def _write32(compiled, cpu, address, value):
    if cpu == 0:
        p.call(compiled.write32, address, value)
    else:
        p.smp_call_sync(cpu, compiled.write32, address, value)


def _read_event(compiled, cpu, event_address):
    if cpu == 0:
        return int(p.read32(event_address)) & 0xFFFFFFFF
    return _remote_read32(compiled, cpu, event_address)


def _drain_candidate(compiled, rows, event_address, timeout_s=0.020):
    """Read each CPU-local EVENT until the candidate arrives or times out."""
    deadline = time.monotonic() + timeout_s
    seen = {}
    while time.monotonic() < deadline and not seen:
        for cpu, _mpidr in rows:
            raw = _read_event(compiled, cpu, event_address)
            if raw:
                seen[cpu] = _event_fields(raw)
        if not seen:
            time.sleep(0.0005)
    return seen


def run():
    node = u.adt["arm-io/aic"]
    compatible = node.getprop("compatible", [])
    if "aic,2" not in compatible:
        raise RuntimeError(f"expected AIC2, compatible={compatible!r}")

    aic, _size = node.get_reg(0)
    cap0 = int(p.read32(aic + AIC_CAP0))
    maxnumirq = int(p.read32(aic + AIC_MAXNUMIRQ))
    nr_irq = cap0 & 0xFFFF
    max_irq = maxnumirq & 0xFFFF
    if PROBE_IRQ >= nr_irq or PROBE_IRQ >= max_irq:
        raise RuntimeError(
            f"probe IRQ {PROBE_IRQ} outside AIC2 geometry "
            f"implemented={nr_irq} max={max_irq}"
        )

    compiled = _read32_code()
    rows = _cpu_rows(compiled)
    if len(rows) < 2:
        raise RuntimeError(f"expected stopped secondary CPUs, rows={rows!r}")

    word = PROBE_IRQ // 32
    bit = 1 << (PROBE_IRQ % 32)
    word_offset = word * 4
    config_address = aic + 0x14
    cfg_address = aic + AIC_IRQ_CFG + PROBE_IRQ * 4
    sw_set = aic + AIC_SW_SET + word_offset
    sw_clear = aic + AIC_SW_CLEAR + word_offset
    mask_set = aic + AIC_MASK_SET + word_offset
    mask_clear = aic + AIC_MASK_CLEAR + word_offset
    hw_state = aic + AIC_HW_STATE + word_offset
    event = aic + AIC_EVENT

    # AIC2 repeats the six register banks for each die.  The first die starts
    # at IRQ_CFG; the second die begins immediately after IRQ_CFG, SW_SET,
    # SW_CLEAR, MASK_SET, MASK_CLEAR, and HW_STATE for max_irq entries.  On
    # J414s the second bank is the unused multi-die half described by Asahi,
    # not another CPU-affinity view.
    die_stride = (4 * max_irq) + (AIC_BANK_COUNT * 4 * (max_irq >> 5))
    second_die_base = aic + AIC_IRQ_CFG + die_stride
    second_cfg = second_die_base + PROBE_IRQ * 4
    second_sw_set = second_die_base + 4 * max_irq + word_offset
    second_sw_clear = second_sw_set + 4 * (max_irq >> 5)
    second_mask_set = second_sw_clear + 4 * (max_irq >> 5)
    second_mask_clear = second_mask_set + 4 * (max_irq >> 5)
    second_hw_state = second_mask_clear + 4 * (max_irq >> 5)

    original_cfg = int(p.read32(cfg_address)) & 0xFFFFFFFF
    original_second_cfg = int(p.read32(second_cfg)) & 0xFFFFFFFF
    original_config = int(p.read32(config_address)) & 0xFFFFFFFF
    initial_hw = int(p.read32(hw_state)) & bit
    initial_second_hw = int(p.read32(second_hw_state)) & bit
    if initial_hw:
        raise RuntimeError(
            f"probe line {PROBE_IRQ} is already active: HW_STATE={initial_hw:#x}"
        )
    if initial_second_hw:
        raise RuntimeError(
            "second-die probe line is already active: "
            f"HW_STATE={initial_second_hw:#x}"
        )

    daif = {0: int(u.mrs("DAIF"))}
    for cpu, _mpidr in rows:
        if cpu:
            daif[cpu] = int(p.smp_call_sync(cpu, compiled.mask_daif))
    u.msr("DAIF", daif[0] | DAIF_MASK)

    print(
        f"AIC2 target probe: base={aic:#x} implemented={nr_irq} max={max_irq} "
        f"line={PROBE_IRQ} cfg={original_cfg:#x} CONFIG={original_config:#x}"
    )
    print("CPUS", [(cpu, f"{mpidr:#x}") for cpu, mpidr in rows])

    results = []
    try:
        # Do not leave a software source or an unmasked line behind between
        # target-code trials.  CONFIG is enabled only for this stopped probe.
        p.write32(mask_set, bit)
        p.write32(sw_clear, bit)
        # m1n1 leaves AIC2 globally disabled until the guest owns it.  Probe
        # both the normal enabled state and the documented PCPU preference
        # bit; preserve all other bits and restore the original value in
        # finally.
        for config_extra in (0, AIC_CONFIG_PREFER_PCPU):
            probe_config = original_config | 1 | config_extra
            p.write32(config_address, probe_config)
            config_readback = int(p.read32(config_address)) & 0xFFFFFFFF
            if config_readback != probe_config:
                raise RuntimeError(
                    f"CONFIG readback {config_readback:#x} != {probe_config:#x}"
                )
            print(f"CONFIG_VARIANT {config_readback:#x}")
            p.nop()

            for target in range(16):
                p.write32(mask_set, bit)
                p.write32(sw_clear, bit)
                p.nop()

                trial_cfg = (original_cfg & ~0xF) | target
                p.write32(cfg_address, trial_cfg)
                readback = int(p.read32(cfg_address)) & 0xFFFFFFFF
                if readback != trial_cfg:
                    raise RuntimeError(
                        f"target {target}: config readback {readback:#x} "
                        f"!= {trial_cfg:#x}"
                    )

                p.write32(mask_clear, bit)
                p.write32(sw_set, bit)
                p.nop()
                seen = _drain_candidate(compiled, rows, event)
                winners = [
                    cpu for cpu, fields in seen.items()
                    if fields["type"] == AIC_EVENT_TYPE_HW
                    and fields["irq"] == PROBE_IRQ
                ]
                row = {
                    "config": config_readback,
                    "target": target,
                    "cfg": readback,
                    "seen": seen,
                    "winners": winners,
                    "hw_after": int(p.read32(hw_state)) & bit,
                }
                results.append(row)
                print(
                    "TARGET",
                    target,
                    "WINNERS",
                    winners,
                    "SEEN",
                    {cpu: fields for cpu, fields in seen.items()},
                    "HW_AFTER",
                    row["hw_after"],
                )

                # EVENT is destructive and auto-masks the winner.  Clear the
                # software source and explicitly mask before the next target.
                p.write32(sw_clear, bit)
                p.write32(mask_set, bit)
                p.nop()

        # AIC register views may make SW_SET producer-local.  With target 0,
        # repeat the post from every stopped CPU and observe every CPU-local
        # EVENT.  This is separate from the target-nibble sweep above.
        writer_config = original_config | 1
        p.write32(config_address, writer_config)
        if (int(p.read32(config_address)) & 0xFFFFFFFF) != writer_config:
            raise RuntimeError("writer-test CONFIG enable failed")
        writer_cfg = original_cfg & ~0xF
        p.write32(cfg_address, writer_cfg)
        if (int(p.read32(cfg_address)) & 0xFFFFFFFF) != writer_cfg:
            raise RuntimeError("writer-test target-0 config failed")
        print(f"WRITER_CONFIG {writer_config:#x} TARGET 0")
        for writer, _mpidr in rows:
            p.write32(mask_set, bit)
            p.write32(sw_clear, bit)
            p.write32(mask_clear, bit)
            p.nop()
            _write32(compiled, writer, sw_set, bit)
            p.nop()
            seen = _drain_candidate(compiled, rows, event)
            winners = [
                cpu for cpu, fields in seen.items()
                if fields["type"] == AIC_EVENT_TYPE_HW
                and fields["irq"] == PROBE_IRQ
            ]
            print("WRITER", writer, "WINNERS", winners, "SEEN", seen)
            p.write32(sw_clear, bit)
            p.write32(mask_set, bit)
            p.nop()

        # The unused AIC2 second-die bank is a separate software source.  It
        # must be tested independently: a die-tagged EVENT is a real type-1
        # AIC token, but it does not imply per-CPU routing.  Repeat the target
        # sweep and the writer-origin control with target 0.
        second_results = []
        probe_config = original_config | 1
        p.write32(config_address, probe_config)
        if (int(p.read32(config_address)) & 0xFFFFFFFF) != probe_config:
            raise RuntimeError("second-die CONFIG enable failed")
        print(
            f"SECOND_DIE line={PROBE_IRQ} cfg={original_second_cfg:#x} "
            f"stride={die_stride:#x}"
        )
        for target in range(16):
            p.write32(second_mask_set, bit)
            p.write32(second_sw_clear, bit)
            p.write32(second_cfg, (original_second_cfg & ~0xF) | target)
            readback = int(p.read32(second_cfg)) & 0xFFFFFFFF
            if readback != ((original_second_cfg & ~0xF) | target):
                raise RuntimeError(
                    f"second-die target {target}: config readback "
                    f"{readback:#x}"
                )
            p.write32(second_mask_clear, bit)
            p.write32(second_sw_set, bit)
            p.nop()
            seen = _drain_candidate(compiled, rows, event)
            winners = [
                cpu for cpu, fields in seen.items()
                if fields["type"] == AIC_EVENT_TYPE_HW
                and fields["irq"] == PROBE_IRQ
                and fields["die"] == 1
            ]
            row = {
                "target": target,
                "cfg": readback,
                "seen": seen,
                "winners": winners,
                "hw_after": int(p.read32(second_hw_state)) & bit,
            }
            second_results.append(row)
            print(
                "SECOND_TARGET",
                target,
                "WINNERS",
                winners,
                "SEEN",
                {cpu: fields for cpu, fields in seen.items()},
                "HW_AFTER",
                row["hw_after"],
            )
            p.write32(second_sw_clear, bit)
            p.write32(second_mask_set, bit)
            p.nop()

        p.write32(second_cfg, original_second_cfg & ~0xF)
        second_writer_hits = []
        for writer, _mpidr in rows:
            p.write32(second_mask_set, bit)
            p.write32(second_sw_clear, bit)
            p.write32(second_mask_clear, bit)
            p.nop()
            _write32(compiled, writer, second_sw_set, bit)
            p.nop()
            seen = _drain_candidate(compiled, rows, event)
            winners = [
                cpu for cpu, fields in seen.items()
                if fields["type"] == AIC_EVENT_TYPE_HW
                and fields["irq"] == PROBE_IRQ
                and fields["die"] == 1
            ]
            if winners:
                second_writer_hits.append((writer, winners))
            p.write32(second_sw_clear, bit)
            p.write32(second_mask_set, bit)
            p.nop()
        print("SECOND_WRITER_HITS", second_writer_hits)

        return results
    finally:
        p.write32(sw_clear, bit)
        p.write32(mask_set, bit)
        p.write32(second_sw_clear, bit)
        p.write32(second_mask_set, bit)
        p.write32(config_address, original_config)
        p.write32(cfg_address, original_cfg)
        p.write32(second_cfg, original_second_cfg)
        p.nop()
        restored_config = int(p.read32(config_address)) & 0xFFFFFFFF
        restored_cfg = int(p.read32(cfg_address)) & 0xFFFFFFFF
        if restored_config != original_config:
            print(
                f"WARNING: CONFIG restore readback was {restored_config:#x}"
            )
        if restored_cfg != original_cfg:
            print(
                f"WARNING: IRQ config restore readback was {restored_cfg:#x}"
            )
        u.msr("DAIF", daif[0])
        for cpu, _mpidr in rows:
            if cpu:
                p.smp_call_sync(cpu, compiled.restore_daif, daif[cpu])
        print(
            f"RESTORED cfg={restored_cfg:#x} CONFIG={restored_config:#x}"
        )


run()
