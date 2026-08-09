# SPDX-License-Identifier: MIT
import os, struct, sys, time

from .hv import HV
from .proxy import *
from .proxyutils import *
from .sysreg import *
from .tgtypes import *
from .utils import *
from .hw.pmu import PMU

# Create serial connection
iface = UartInterface()
# Construct m1n1 proxy layer over serial connection
p = M1N1Proxy(iface, debug=False)
# Customise parameters of proxy and serial port
# based on information sent over the connection
bootstrap_port(iface, p)

# Initialise the Proxy interface from values fetched from
# the remote end
u = ProxyUtils(p)
# Build a Register Monitoring object on Proxy Interface
mon = RegMonitor(u)
hv = HV(iface, p, u)

fb = u.ba.video.base

try:
    PMU(u).reset_panic_counter()
except Exception as e:
    # Same guard as tools/run_guest.py. On T8142 the SPMI RX FIFO never reports
    # empty (its STATUS layout differs), so this raises SPMITimeout and would
    # otherwise make shell.py unusable. The panic counter is a convenience.
    print(f"WARNING: could not reset the PMU panic counter: {e!r}")

print(f"m1n1 base: 0x{u.base:x}")

try:
    PMU(u).reset_panic_counter()
except Exception as e:
    # Same guard as tools/run_guest.py. On T8142 the SPMI RX FIFO never reports
    # empty (its STATUS layout differs), so this raises SPMITimeout and would
    # otherwise make shell.py unusable. The panic counter is a convenience.
    print(f"WARNING: could not reset the PMU panic counter: {e!r}")
