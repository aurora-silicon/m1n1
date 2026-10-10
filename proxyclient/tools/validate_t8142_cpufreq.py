#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check M5 firmware states and exercise m1n1 cluster initialization."""
import argparse
import json
import os
from pathlib import Path
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

iface = UartInterface()
p = M1N1Proxy(iface)
if p.get_chipid() != 0x8142 or int(p.iodev_whoami()) != 1:
    raise RuntimeError("Expected T8142 KIS")
bootstrap_port(iface, p)
u = ProxyUtils(p)
if u.adt.model != "Mac17,3":
    raise RuntimeError("Expected J813")
registers = [0x210e20020, 0x211e20020]
before = [p.read64(r) for r in registers]
print("BEFORE", [hex(v) for v in before], flush=True)
report = {"target": "J813", "before": before, "rounds": [], "pass": False}
pmgr = u.adt["/arm-io/pmgr"]
report["firmware_states"] = {}
for name, state, frequency in (("voltage-states1-sram", 2, 972000),
                               ("voltage-states5-sram", 10, 3516000)):
    values = list(pmgr.getprop(name))
    # State 1 is the 300 MHz floor; SRAM table entries begin at state 2.
    if len(values) % 2 or len(values) < (state - 1) * 2 or values[(state - 2) * 2] != frequency:
        raise RuntimeError("Unexpected firmware frequency table: " + name)
    report["firmware_states"][name] = values
print("FIRMWARE_HANDOFF_STATES_VERIFIED", flush=True)
args.output.write_text(json.dumps(report, indent=2) + "\n")
if [v & 31 for v in before] != [2, 10] or any(v & (1 << 31) for v in before):
    raise RuntimeError("Unexpected firmware states; refusing initialization")
for iteration in range(3):
    ret = p.cpufreq_init()
    after = [p.read64(r) for r in registers]
    report["rounds"].append({"return": ret, "after": after})
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    if ret != 0 or [v & 31 for v in after] != [2, 10]:
        raise RuntimeError("Cluster initialization failed")
    if any(v & (1 << 31) for v in after):
        raise RuntimeError("Cluster still busy")
    request_bits = 31 | (1 << 25) | (1 << 31)
    if any((a ^ b) & ~request_bits for a, b in zip(before, after)):
        raise RuntimeError("Unrelated command bits changed")
    print("INIT_PASS", iteration, [hex(v) for v in after], flush=True)
report["pass"] = True
args.output.write_text(json.dumps(report, indent=2) + "\n")
print("PASS M5 cluster initialization and command-state readback", flush=True)
iface.dev.close()
