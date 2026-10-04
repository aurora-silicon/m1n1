#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise the RAM-loaded public M5 SMP candidate and record per-core replies."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--device", required=True)
parser.add_argument("--proxyclient", type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
os.environ["M1N1DEVICE"] = args.device
os.environ["M1N1TIMEOUT"] = "15"
sys.path.insert(0, str(args.proxyclient.resolve()))
from m1n1.proxy import UartInterface, M1N1Proxy
from m1n1.proxyutils import ProxyUtils, bootstrap_port
from m1n1.asm import ARMAsm

iface = UartInterface()
p = M1N1Proxy(iface)
if p.get_chipid() != 0x8142 or int(p.iodev_whoami()) != 1:
    raise RuntimeError("Expected T8142 on KIS")
bootstrap_port(iface, p)
u = ProxyUtils(p)
if u.adt.model != "Mac17,3":
    raise RuntimeError("Expected J813")
cpus = [{"id": c.cpu_id, "mpidr": c.reg | (0x10000 if c.reg & 0x100 else 0),
         "boot": c.state == "running"}
        for c in u.adt["/cpus"]]
print("TARGET J813", json.dumps(cpus), flush=True)
report = {"target": "J813", "cpus": cpus, "replies": [], "missing": [], "pass": False}
args.output.write_text(json.dumps(report, indent=2) + "\n")

code = ARMAsm("""
get_mpidr:
    mrs x0, mpidr_el1
    ret
sum_loop:
    mov x2, #0
1:
    add x2, x2, x1
    subs x1, x1, #1
    b.ne 1b
    eor x0, x0, x2
    ret
""")
addr = u.memalign(0x4000, 0x4000)
iface.writemem(addr, code.data)
p.dc_cvac(addr, len(code.data))
p.ic_ivau(addr, len(code.data))
report["code_sha256"] = hashlib.sha256(code.data).hexdigest()

print("START_SECONDARIES", flush=True)
p.smp_start_secondaries()
for cpu in cpus:
    index = cpu["id"]
    if cpu["boot"]:
        continue
    if not p.smp_is_alive(index):
        report["missing"].append(index)
        print("MISSING", index, flush=True)
        continue
    mpidr = p.smp_call_sync(index, addr + code.get_mpidr)
    if mpidr & 0xFFFFFF != cpu["mpidr"] & 0xFFFFFF:
        raise RuntimeError(f"CPU {index}: wrong MPIDR {mpidr:#x}")
    for iteration in range(20):
        nonce = secrets.randbits(48)
        count = 100000 + iteration
        expected = nonce ^ (count * (count + 1) // 2)
        actual = p.smp_call_sync(index, addr + code.sum_loop, nonce, count)
        if actual != expected:
            raise RuntimeError(f"CPU {index}: computation mismatch")
    report["replies"].append({"cpu": index, "mpidr": hex(mpidr), "rounds": 20})
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print("CPU_PASS", index, hex(mpidr), "20 computations", flush=True)
report["pass"] = not report["missing"] and len(report["replies"]) == len(cpus) - 1
args.output.write_text(json.dumps(report, indent=2) + "\n")
print("SMP_RESULT", json.dumps(report), flush=True)
iface.dev.close()
if not report["pass"]:
    raise SystemExit(1)
