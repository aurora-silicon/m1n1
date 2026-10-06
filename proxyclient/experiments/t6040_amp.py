#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Cold J616s independent CPU workloads. Leaves secondaries running in WFE mode."""
import json
import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

from m1n1.setup import p, u
from m1n1.proxyutils import REGION_RX_EL1

result = {'workloads': [], 'possibly_dispatched': [], 'completed': []}
try:
    if p.get_chipid() != 0x6040 or u.adt['/chosen'].board_id != 6:
        raise ValueError("This probe is qualified only on J616s / board 6")
    all_nodes = list(u.adt['/cpus'])
    if any(p.smp_is_alive(node.cpu_id) for node in all_nodes):
        raise ValueError("Secondaries are already running; reboot first")
    boot = u.mrs('MPIDR_EL1') & 0xffff
    if sum(node.reg == boot for node in all_nodes) != 1:
        raise ValueError("Boot CPU is not uniquely identified in the ADT")
    nodes = [node for node in all_nodes if node.reg != boot]
    p.smp_set_wfe_mode(True)
    p.smp_start_secondaries()
    for node in nodes:
        if not p.smp_is_alive(node.cpu_id):
            raise RuntimeError(f'Secondary {node.cpu_id} did not start')
        p.mmu_init_secondary(node.cpu_id)
    mask = (1 << 64) - 1
    code = '''
        cbz x1, 9f
        ldr x4, [x0, #24]
    1: mrs x5, cntpct_el0
        cmp x5, x4
        b.lo 1b
        str x5, [x0, #8]
        mov x6, x1
        cbz x3, 2f
        cmp x3, #1
        b.eq 5f
    7: ror x2, x2, #7
        eor x2, x2, x6
        subs x6, x6, #1
        b.ne 7b
        b 8f
    5: mov x7, #33
    6: mul x2, x2, x7
        add x2, x2, x6
        subs x6, x6, #1
        b.ne 6b
        b 8f
    2: add x7, x0, #256
        mov x8, #256
    3: ldr x9, [x7], #8
        add x2, x2, x9
        subs x8, x8, #1
        b.ne 3b
        subs x6, x6, #1
        b.ne 2b
    8: str x2, [x0]
        mrs x5, cntpct_el0
        str x5, [x0, #16]
        dmb sy
    9: mrs x0, mpidr_el1
    '''
    freq = u.mrs('CNTFRQ_EL0')
    for node in nodes:
        cpu = node.cpu_id
        mode = 0 if cpu < 4 else 1 if cpu < 10 else 2
        loops = 10000 if mode == 0 else 2000000
        seed = cpu + 1
        buf = u.memalign(0x4000, 0x4000)
        if mode == 0:
            values = [seed * (index + 1) for index in range(256)]
            u.iface.writemem(buf + 256, b''.join(x.to_bytes(8, 'little') for x in values))
            expected = (seed + loops * sum(values)) & mask
        elif mode == 1:
            expected = seed
            for k in range(loops, 0, -1):
                expected = (expected * 33 + k) & mask
        else:
            expected = seed
            for k in range(loops, 0, -1):
                expected = ((expected >> 7) | ((expected << 57) & mask)) ^ k
        result['workloads'].append({'cpu': cpu, 'mpidr_expected': node.reg, 'mode': mode,
                                    'iterations': loops, 'seed': seed, 'buffer': buf,
                                    'expected': expected})
    result['nominal_clocks_before'] = [p.cpufreq_get_cluster_hz(c) for c in range(3)]
    start = u.mrs('CNTPCT_EL0') + 2 * freq
    # Keep this code buffer unchanged until every secondary returns.
    u.exec(code)
    address = u.code_buffer | REGION_RX_EL1
    for entry in result['workloads']:
        p.write64(entry['buffer'] + 24, start)
    for entry in result['workloads']:
        result['possibly_dispatched'].append(entry['cpu'])
        p.smp_call(entry['cpu'], address, entry['buffer'], entry['iterations'],
                   entry['seed'], entry['mode'])
    for entry in result['workloads']:
        entry['mpidr'] = p.smp_wait(entry['cpu']) & 0xffff
        result['completed'].append(entry['cpu'])
        buf = entry['buffer']
        entry['actual'] = p.read64(buf)
        entry['start_tick'] = p.read64(buf + 8)
        entry['end_tick'] = p.read64(buf + 16)
    for entry in result['workloads']:
        if entry['mpidr'] != entry['mpidr_expected'] or entry['actual'] != entry['expected']:
            raise RuntimeError(f"CPU {entry['cpu']} returned an unexpected result")
    result['concurrent_overlap_ticks'] = (
        min(e['end_tick'] for e in result['workloads']) -
        max(e['start_tick'] for e in result['workloads']))
    if result['concurrent_overlap_ticks'] <= 0:
        raise RuntimeError("Workloads did not run concurrently")
    result['nominal_clocks_after'] = [p.cpufreq_get_cluster_hz(c) for c in range(3)]
    if result['nominal_clocks_before'] != result['nominal_clocks_after']:
        raise RuntimeError("Cluster clock settings changed")
    p.nop()
    result['proxy_nop'] = 'passed'
    result['counter_frequency'] = freq
except Exception as exc:
    result['error'] = str(exc)
    result['reboot_required'] = bool(
        set(result['possibly_dispatched']) - set(result['completed']))
    raise
finally:
    print(json.dumps(result, indent=2))
