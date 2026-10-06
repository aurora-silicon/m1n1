#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Cold J616s SIO RTKit probe. Reboot before each run; no audio DMA commands."""
import json
import pathlib
import struct
import sys
import time

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

from m1n1.setup import p, u
from m1n1.hw.dart import DART
from m1n1.hw.dart8110 import PTE
from m1n1.fw.asc import StandardASC
from m1n1.fw.asc.mgmt import Mgmt_SetAPPower, Mgmt_SetIOPPower


def wait_until(condition, service=lambda: None, timeout=1):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise TimeoutError("SIO probe timed out")
        service()


def probe():
    if p.get_chipid() != 0x6040 or u.adt['/chosen'].board_id != 6:
        raise ValueError("This probe is qualified only on J616s / board 6")
    base = u.adt['/arm-io/sio'].get_reg(0)[0]
    dart_base = u.adt['/arm-io/dart-sio'].get_reg(0)[0]
    plan = []
    segments = u.adt['/arm-io/sio/iop-sio-nub'].getprop('segment-ranges')
    for phys, virt, remap, size, flags in struct.iter_unpack('<QQQII', segments):
        if remap < 1 << 40:  # Local SRAM is outside the external DART mappings.
            continue
        offset = remap & 0x3fff
        va, pa = remap - offset, phys - offset
        span = (size + offset + 0x3fff) & ~0x3fff
        if phys & 0x3fff != offset or not 0 < span < 64 * 1024 * 1024:
            raise ValueError("Invalid external firmware segment")
        if va + span > 1 << 42 or pa + span > 1 << 42:
            raise ValueError("Firmware segment exceeds the tested DART address width")
        if any(va < oldva + oldspan and oldva < va + span
               for oldva, _, oldspan, _ in plan):
            raise ValueError("Overlapping firmware mappings")
        plan.append((va, pa, span, flags))
    if not plan:
        raise ValueError("No external firmware segments")
    start = max(va + span for va, _, span, _ in plan)
    end = start + 0x10000000
    if end > 1 << 42:
        raise ValueError("Endpoint allocation arena exceeds the DART address width")
    wrapper = DART.from_adt(u, '/arm-io/dart-sio', iova_range=(start, end))
    dart = wrapper.dart
    ctrl, tcr, ttbr = p.read32(base + 0x44), dart.regs.TCR[0].val, dart.regs.TTBR[0].val
    if (ctrl, tcr, ttbr, dart.enabled_streams) != (0, 1, 0, 0):
        raise ValueError("SIO/DART is not in the tested cold state; reboot first")
    if dart.regs.REG_PROTECT.reg.LOCK_TCR_TTBR:
        raise ValueError("SIO DART tables are locked")
    if dart.regs.ERROR.reg.FLAG:
        raise ValueError("SIO DART already reports a fault")
    params = dart.regs.PARAMS_8.reg
    if params.VA_WIDTH != 42 or params.VERS_MAJ != 2:
        raise ValueError("Unsupported DART address width or revision")
    if not p.read32(base + 0x8114) & (1 << 17):
        raise ValueError("SIO outbox is not empty")

    def invalidate(streams=1):
        if streams != 1:
            raise ValueError("Only SIO stream 0 is supported")
        for table in dart.pt_cache:
            p.dc_cvac(table, 0x4000)
        u.exec('dsb sy')
        p.write32(dart_base + 0x80, 0x100)
        wait_until(lambda: not p.read32(dart_base + 0x80) & (1 << 31), timeout=.5)

    result = {'mappings': [], 'pong_received': False}
    asc = None
    boot_attempted = False
    error = None
    try:
        dart.regs.TCR[0].val = tcr | 8
        if dart.regs.TCR[0].val != 9:
            raise RuntimeError("Four-level TCR did not latch")
        for va, pa, span, flags in plan:
            dart.iomap_at(0, va, pa, span)
            if flags & 1:  # Firmware text must remain read-only.
                root = dart.regs.TTBR[0].reg.ADDR << 14
                dirty = set()
                for page in range(va, va + span, 0x4000):
                    _, l0 = dart.get_pt(root)
                    _, l1 = dart.get_pt(PTE(l0[(page >> 36) & 2047]).OFFSET << 14)
                    leaf = PTE(l1[(page >> 25) & 2047]).OFFSET << 14
                    _, l2 = dart.get_pt(leaf)
                    index = (page >> 14) & 2047
                    entry = PTE(l2[index])
                    entry.WRPROT = 1
                    l2[index] = entry.value
                    dirty.add(leaf)
                for leaf in dirty:
                    dart.flush_pt(leaf)
            result['mappings'].append({'iova': hex(va), 'pa': hex(pa), 'size': span,
                                       'write_protect': bool(flags & 1)})
        invalidate()
        wrapper.invalidate_streams = invalidate
        asc = StandardASC(u, base, wrapper, dva_mask=(1 << 42) - 1)
        asc.dva_offset, asc.dva_size = 0, 1 << 42

        def send(msg0, msg1):
            wait_until(lambda: not p.read32(base + 0x8110) & (1 << 16), timeout=.5)
            asc.asc.INBOX0.val, asc.asc.INBOX1.val = msg0, msg1

        asc.send = send
        asc.mgmt.msghandler[4] = lambda msg: result.update(pong_received=True) or True
        boot_attempted = True
        asc.start()
        result['endpoints'] = asc.eps
        result['power_ack'] = {'iop': asc.mgmt.iop_power_state, 'ap': asc.mgmt.ap_power_state}
        asc.mgmt.ping()
        wait_until(lambda: result['pong_received'], asc.work)
        result['dart_error'] = hex(dart.regs.ERROR.val)
        if dart.regs.ERROR.reg.FLAG:
            raise RuntimeError("SIO DART reported a fault")
    except Exception as exc:
        error = exc
        result['error'] = str(exc)
    finally:
        try:
            if boot_attempted:
                asc.mgmt.send(Mgmt_SetAPPower(STATE=0x10))
                wait_until(lambda: asc.mgmt.ap_power_state == 0x10, asc.work)
                asc.mgmt.send(Mgmt_SetIOPPower(STATE=0x10))
                wait_until(lambda: asc.mgmt.iop_power_state == 0x10, asc.work)
                result['quiesced'] = True
            if not boot_attempted or result.get('quiesced'):
                p.write32(base + 0x44, ctrl)
                p.write32(dart_base + 0xc20, 1)
                dart.regs.TTBR[0].val, dart.regs.TCR[0].val = ttbr, tcr
                invalidate()
                result['restored'] = {
                    'cpu_control': hex(p.read32(base + 0x44)),
                    'tcr': hex(dart.regs.TCR[0].val), 'ttbr': hex(dart.regs.TTBR[0].val),
                    'enabled': hex(p.read32(dart_base + 0xc00)),
                    'dart_error': hex(dart.regs.ERROR.val),
                }
                restored = result['restored']
                if (restored['cpu_control'], restored['tcr'], restored['ttbr'],
                    restored['enabled']) != (hex(ctrl), hex(tcr), hex(ttbr), '0x0'):
                    raise RuntimeError("SIO/DART state restoration failed")
                if dart.regs.ERROR.reg.FLAG:
                    raise RuntimeError("SIO DART fault after quiescence")
        except Exception as exc:
            # Preserve mappings if shutdown was not acknowledged; recover by reboot.
            result['cleanup_error'] = str(exc)
            result['reboot_required'] = True
            error = error or exc
        try:
            p.nop()
            result['proxy_nop'] = 'passed'
        except Exception as exc:
            result['proxy_error'] = str(exc)
            error = error or exc
        print(json.dumps(result, indent=2))
    # Preserve both failures when startup and cleanup fail independently.
    if error is not None:
        raise error
    return result


if __name__ == '__main__':
    probe()
