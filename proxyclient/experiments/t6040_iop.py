#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Cold J616s SIO/MTP/AOP RTKit probe. Reboot before each run."""
import argparse
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
            raise TimeoutError("IOP probe timed out")
        service()


def probe_mtp(asc, keyboard, result):
    from m1n1.hw.dockchannel import DockChannelDataRegs
    from m1n1.fw.mtp import decode_packet, encode_packet, RXMessage, InitMsg

    data = DockChannelDataRegs(u, u.adt['/arm-io/dockchannel-mtp'].get_reg(3)[0])
    pending = bytearray()
    result.update(initialization=[], keyboard_ready=False, keyboard_enable_ack=False,
                  packets=[], keyboard_reports=[])

    def capture(seconds):
        deadline = time.monotonic() + seconds
        received = 0
        while time.monotonic() < deadline:
            asc.work()
            count = data.RX_COUNT.val
            if count >= 4:
                pending.extend(struct.pack('<I', data.RX_32.val))
                received += 4
            elif count:
                pending.append(data.RX_8.DATA)
                received += 1
            if received > 16384:
                raise ValueError("MTP capture exceeds the probe limit")
            while len(pending) >= 8:
                hlen, kind, size, seq, iface, pad = struct.unpack_from('<BBHBBH', pending)
                if hlen != 8 or size % 4 or size > 4096:
                    raise ValueError("Invalid MTP header")
                if len(pending) < size + 12:
                    break
                iface, kind, seq, payload = decode_packet(bytes(pending[:size + 12]))
                del pending[:size + 12]
                msg = RXMessage.parse(payload)
                if len(payload) != (msg.hdr.length + 11) & ~3:
                    raise ValueError("Invalid MTP message length")
                result['packets'].append({'interface': iface, 'kind': kind,
                                          'counter': seq, 'payload': payload.hex()})
                if iface == 0 and msg.hdr.flags == 0:
                    if msg.msg[:3] == b'\xf0\x01\x00':
                        init = InitMsg.parse(msg.msg)
                        result['initialization'].append({
                            'id': init.device_id, 'name': init.device_name,
                            'more_packets': init.more_packets,
                            'hid_descriptors': [block.payload.descriptor.hex()
                                                for block in init.msg if block.type == 0]})
                    elif msg.msg == b'\xf1\x02\x00\x00':
                        result['keyboard_ready'] = True
                elif (iface == 0 and kind == 0x11 and seq == 0
                      and msg.hdr.flags == 0x80 and msg.msg == b'\xb4'):
                    if msg.hdr.retcode:
                        raise RuntimeError("Keyboard enable was rejected")
                    result['keyboard_enable_ack'] = True
                elif iface == 2 and msg.hdr.flags == 0:
                    result['keyboard_reports'].append(msg.msg.hex())

    capture(5)
    if pending:
        raise ValueError("Incomplete MTP initialization packet")
    if keyboard:
        candidates = [entry for entry in result['initialization'] if entry['name'] == 'keyboard']
        if len(candidates) != 1 or candidates[0]['id'] != 2 or candidates[0]['more_packets']:
            raise ValueError("Keyboard interface was not uniquely announced")
        msg = struct.pack('<HHI', 0x80, 2, 0) + b'\xb4\x02'
        packet = encode_packet(0, 0, msg)
        wait_until(lambda: data.TX_FREE.val >= len(packet), asc.work)
        if pending or data.RX_COUNT.val:
            raise ValueError("MTP replies remain queued before keyboard enable")
        result['keyboard_enable_ack'] = result['keyboard_ready'] = False
        for offset in range(0, len(packet), 4):
            data.TX_32.val = struct.unpack_from('<I', packet, offset)[0]
        capture(5)
        if pending or not result['keyboard_enable_ack'] or not result['keyboard_ready']:
            raise TimeoutError("Keyboard enable ACK/readiness did not complete")
    return result


def probe(device, keyboard=False):
    if p.get_chipid() != 0x6040 or u.adt['/chosen'].board_id != 6:
        raise ValueError("This probe is qualified only on J616s / board 6")
    if keyboard and device != 'mtp':
        raise ValueError("Keyboard testing requires the MTP probe")
    dev_path = f'/arm-io/{device}'
    dart_path = f'/arm-io/dart-{device}'
    base = u.adt[dev_path].get_reg(0)[0]
    dart_node = u.adt[dart_path]
    dart_base = dart_node.get_reg(0)[0]
    if dart_node[f'mapper-{device}'].reg != 0:
        raise ValueError("Only ADT stream 0 is qualified")
    plan = []
    segments = u.adt[f'{dev_path}/iop-{device}-nub'].getprop('segment-ranges')
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
    wrapper = DART.from_adt(u, dart_path, iova_range=(start, end))
    dart = wrapper.dart
    ctrl, tcr, ttbr = p.read32(base + 0x44), dart.regs.TCR[0].val, dart.regs.TTBR[0].val
    if (ctrl, tcr, dart.enabled_streams) != (0, 1, 0) or dart.regs.TTBR[0].reg.VALID:
        raise ValueError("IOP/DART is not in the tested cold state; reboot first")
    if dart.regs.REG_PROTECT.reg.LOCK_TCR_TTBR:
        raise ValueError("IOP DART tables are locked")
    if dart.regs.ERROR.reg.FLAG:
        raise ValueError("IOP DART already reports a fault")
    params = dart.regs.PARAMS_8.reg
    if params.VA_WIDTH != 42 or params.VERS_MAJ != 2:
        raise ValueError("Unsupported DART address width or revision")
    if not p.read32(base + 0x8114) & (1 << 17):
        raise ValueError("IOP outbox is not empty")

    def invalidate(streams=1):
        if streams != 1:
            raise ValueError("Only IOP stream 0 is supported")
        for table in dart.pt_cache:
            p.dc_cvac(table, 0x4000)
        u.exec('dsb sy')
        p.write32(dart_base + 0x80, 0x100)
        wait_until(lambda: not p.read32(dart_base + 0x80) & (1 << 31), timeout=.5)

    result = {'device': device, 'mappings': [], 'pong_received': False}
    asc = None
    boot_attempted = False
    error = None
    dapf_saved = []
    if device in ('mtp', 'aop'):
        dapf_base = dart_node.get_reg(1)[0]
        dapf_configs = dart_node.getprop('dapf-instance-0')
        dapf_offsets = (0, 4, 8, 12, 16, 20, 32)
        dapf_saved = [{off: p.read32(dapf_base + idx * 0x40 + off)
                       for off in dapf_offsets} for idx in range(len(dapf_configs))]
    aop = None
    if device == 'aop':
        from m1n1.fw.aop.base import AOPBase
        from m1n1.fw.aop.client import AOPClient
        aop = AOPBase(u)
        bootargs = aop.read_bootargs()
        original_bootargs = bootargs.to_bytes()
        text = [va for va, _, _, flags in plan if flags & 1]
        if len(text) != 1 or text[0] != 1 << 40:
            raise ValueError("Unsupported AOP external code mapping")
        bootargs.update(dict(p0CE=text[0]))
    try:
        dart.regs.TTBR[0].val = 0
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
        asc.dva_size = 1 << 42

        def send(msg0, msg1):
            wait_until(lambda: not p.read32(base + 0x8110) & (1 << 16), timeout=.5)
            asc.asc.INBOX0.val, asc.asc.INBOX1.val = msg0, msg1

        asc.send = send
        asc.mgmt.msghandler[4] = lambda msg: result.update(pong_received=True) or True
        if dapf_saved:
            result['dapf_init_return'] = p.dapf_init(dart_path, 1)
            if result['dapf_init_return'] != 0:
                raise RuntimeError("DAPF initialization failed")
            for idx, config in enumerate(dapf_configs):
                entry = dapf_base + idx * 0x40
                expected = (config.r0h << 4 | config.r0l, config.r4,
                            config.start, config.end | 3, config.r20)
                observed = (p.read32(entry), p.read32(entry + 4),
                            p.read64(entry + 8), p.read64(entry + 16), p.read32(entry + 32))
                if observed != expected:
                    raise RuntimeError("ADT DAPF ranges did not latch")
            result['dapf_adt_ranges_latched'] = True
        if aop:
            aop.write_bootargs(bootargs)
            p.dc_cvac(aop._bootargs_span[0], len(original_bootargs))
            u.exec('dsb sy')
        boot_attempted = True
        asc.start()
        result['endpoints'] = asc.eps
        result['power_ack'] = {'iop': asc.mgmt.iop_power_state, 'ap': asc.mgmt.ap_power_state}
        asc.mgmt.ping()
        wait_until(lambda: result['pong_received'], asc.work)
        if aop:
            asc.epcls.update(AOPClient.ENDPOINTS)
            for ep in asc.eps:
                if ep >= 0x20 and ep in asc.epcls:
                    asc.start_ep(ep)
            services = {'SPUApp', 'wakehint', 'aop-audio', 'aop-voicetrigger',
                        'accel', 'gyro', 'als'}
            wait_until(lambda: services <= {name for ep, obj in asc.epmap.items()
                                            if ep >= 0x20 for name in obj.serv_map},
                       asc.work, timeout=5)
            wait_until(lambda: all(obj.started for ep, obj in asc.epmap.items() if ep >= 0x20),
                       asc.work, timeout=5)
            result['endpoint_start_acks'] = [ep for ep in asc.epmap if ep >= 0x20]
            result['application_services'] = {
                hex(ep): list(obj.serv_map) for ep, obj in asc.epmap.items() if ep >= 0x20}
        if device == 'mtp':
            result['mtp'] = {}
            probe_mtp(asc, keyboard, result['mtp'])
        result['dart_error'] = hex(dart.regs.ERROR.val)
        if dart.regs.ERROR.reg.FLAG:
            raise RuntimeError("IOP DART reported a fault")
    except Exception as exc:
        error = exc
        result['error'] = str(exc)
    finally:
        try:
            if boot_attempted:
                if aop:
                    result['endpoint_shutdown_acks'] = []
                    for ep, obj in sorted(asc.epmap.items()):
                        if ep < 0x20:
                            continue
                        obj.stop(timeout=5)
                        result['endpoint_shutdown_acks'].append(ep)
                asc.mgmt.send(Mgmt_SetAPPower(STATE=0x10))
                wait_until(lambda: asc.mgmt.ap_power_state == 0x10, asc.work)
                if aop:
                    # IOP sleep/quiescence crashes this firmware; retain its mappings.
                    result['reboot_required'] = True
                    result['iop_shutdown'] = 'unqualified'
                else:
                    asc.mgmt.send(Mgmt_SetIOPPower(STATE=0x10))
                    wait_until(lambda: asc.mgmt.iop_power_state == 0x10, asc.work)
                    result['quiesced'] = True
            if not boot_attempted or result.get('quiesced'):
                p.write32(base + 0x44, ctrl)
                p.write32(dart_base + 0xc20, 1)
                dart.regs.TTBR[0].val, dart.regs.TCR[0].val = ttbr, tcr
                for idx, values in enumerate(dapf_saved):
                    # Publish the original filter configuration after its range words.
                    for off in (4, 8, 12, 16, 20, 32, 0):
                        p.write32(dapf_base + idx * 0x40 + off, values[off])
                if dapf_saved:
                    result['dapf_restored'] = all(
                        p.read32(dapf_base + idx * 0x40 + off) == value
                        for idx, values in enumerate(dapf_saved) for off, value in values.items())
                    if not result['dapf_restored']:
                        raise RuntimeError("DAPF restoration failed")
                if aop:
                    u.iface.writemem(aop._bootargs_span[0], original_bootargs)
                    p.dc_cvac(aop._bootargs_span[0], len(original_bootargs))
                    result['bootargs_restored'] = aop.read_bootargs().to_bytes() == original_bootargs
                    if not result['bootargs_restored']:
                        raise RuntimeError("AOP boot argument restoration failed")
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
                    raise RuntimeError("IOP/DART state restoration failed")
                if dart.regs.ERROR.reg.FLAG:
                    raise RuntimeError("IOP DART fault after quiescence")
        except Exception as exc:
            # Preserve mappings if shutdown was not acknowledged; recover by reboot.
            result['cleanup_error'] = str(exc)
            result['reboot_required'] = True
            error = error or exc
        try:
            result['shutdown_dart_error'] = hex(dart.regs.ERROR.val)
            if dart.regs.ERROR.reg.FLAG:
                raise RuntimeError("DART fault after shutdown attempt")
        except Exception as exc:
            result['dart_check_error'] = str(exc)
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('device', choices=('sio', 'mtp', 'aop'))
    parser.add_argument('--keyboard', action='store_true', help='Enable only the MTP keyboard')
    args = parser.parse_args()
    probe(args.device, args.keyboard)
