#
# Find out why UEFI hangs reading the xHCI PORTSC register on a DWC3 port.
#
# Run from the plain m1n1 proxy shell (proxyclient/tools/shell.py, NOT the
# hypervisor), then:
#
#     u.adt   # force the ADT to load first, optional
#     exec(open("/path/to/usb_portsc_probe.py").read())
#
# Why the plain proxy and not run_guest: this isolates hardware behaviour from
# anything the hypervisor does. UEFI's hang happens as a guest under m1n1, so a
# trapped/unmapped stage-2 mapping is an alternative explanation. If PORTSC reads
# fine here but hangs under UEFI, the problem is the HV mapping, not the port.
#
# Background -- what we are testing:
#
# m1n1's usb_phy_bringup() (src/usb.c:143-151) is applied to every port, and it
# *parks* the USB3 PIPE PHY:
#
#     PIPEHANDLER_MUX_CTRL           = 0x22  (MUX_CTRL_DUMMY)
#     PIPEHANDLER_NONSELECTED_OVERRIDE = 0x9332
#         bit 15 DUMMY_PHY_EN        = 1     dummy PHY feeding the core
#         bit 12 NATIVE_RESET        = 1     real ATC PHY held in reset
#         bits 3:0 NATIVE_POWER_DOWN = 2     real PHY partly powered down
#
# That is correct for m1n1's own USB2 device-mode gadget. But UEFI's XhciDxe
# reports "max speed 3, 2 ports" and then hangs in GetPortStatus before its first
# debug print -- i.e. in a PORTSC read. If xHCI port 1 is the SuperSpeed port, its
# PORTSC lives in a register domain clocked by the PIPE PHY, and reading it with
# that PHY in reset and powered down would stall forever. Capability registers and
# DCBAA/CRCR are in the always-on core domain, which is why those all worked.
#
# This script reads, in order: the pipehandler state (always-on, safe), the xHCI
# capability registers (safe -- UEFI already read them), the Supported Protocol
# extended capabilities to learn which ports are USB2 vs USB3, and only then
# PORTSC. Every step prints and flushes BEFORE the access, so if the machine wedges
# the last line printed names the exact register that did it.
#
# Nothing here writes anything unless you set UNPARK = True.
#

import sys

# Ports to probe. Reading is safe on any of them, including the one m1n1's console
# is on: PORTSC reads do not disturb the DWC3 device-mode gadget.
#
# On the plain proxy (unlike under the hypervisor) NO node is stripped, so all of
# usb-drd0/1/3 are visible here. J704 has no usb-drd2.
CANDIDATE_PORTS = [0, 1, 3]

# Set True to clear NATIVE_RESET / NATIVE_POWER_DOWN and move the mux off the dummy
# PHY before re-reading PORTSC. Leave False for the first run.
UNPARK = False

# Which ports UNPARK is allowed to touch. Deliberately empty by default.
#
# DANGER: un-parking the port m1n1's console is on will drop the proxy link and the
# VUART -- you lose the session and the log. Reading is harmless, writing is not.
# m1n1 prints which iodev it came up on at boot; put the OTHER ports here.
#
# Nothing is written unless UNPARK is True AND the port appears in this list.
UNPARK_PORTS = []

PIPEHANDLER_MUX_CTRL = 0x0C
PIPEHANDLER_AON_GEN = 0x1C
PIPEHANDLER_NONSELECTED_OVERRIDE = 0x20

MUX_CTRL_USB3 = 0x08
MUX_CTRL_DUMMY = 0x22

AON_GEN_DWC3_RESET_N = 1 << 0
AON_GEN_DWC3_FORCE_CLAMP_EN = 1 << 4

NATIVE_RESET = 1 << 12
DUMMY_PHY_EN = 1 << 15
NATIVE_POWER_DOWN = 0xF


def say(msg):
    # Flush every line: if the next MMIO access wedges the machine, this is the
    # last thing we will ever see, and it has to have reached the terminal.
    print(msg)
    sys.stdout.flush()


def node_regs(idx):
    """Return (xhci_base, pipehandler_base) for usb-drdN, or None."""
    path = f"/arm-io/usb-drd{idx}"
    try:
        node = u.adt[path]
    except KeyError:
        say(f"  {path}: no such node (expected for usb-drd2 on J704, or the "
            f"port m1n1 owns under the HV)")
        return None

    try:
        xhci = node.get_reg(0)[0]
    except Exception as e:
        say(f"  {path}: reg[0] unavailable ({e!r})")
        return None

    # reg[3] is the "pipehandler" block -- see usb_drd_get_regs() in src/usb.c,
    # which takes index 0 and index 3. AppleUsbTypeCBringupDxe only ever reads
    # index 0, which is why it cannot touch the PHY parking at all.
    try:
        pipe = node.get_reg(3)[0]
    except Exception as e:
        say(f"  {path}: reg[3] (pipehandler) unavailable ({e!r})")
        pipe = None

    return xhci, pipe


def show_pipehandler(pipe):
    """Pipehandler registers are always-on; reading them is safe."""
    mux = p.read32(pipe + PIPEHANDLER_MUX_CTRL)
    aon = p.read32(pipe + PIPEHANDLER_AON_GEN)
    ovr = p.read32(pipe + PIPEHANDLER_NONSELECTED_OVERRIDE)

    say(f"    MUX_CTRL             = {mux:#010x}" +
        ("  (DUMMY)" if mux == MUX_CTRL_DUMMY else
         "  (USB3)" if mux == MUX_CTRL_USB3 else "  (?)"))
    say(f"    AON_GEN              = {aon:#010x}"
        f"  DWC3_RESET_N={'1' if aon & AON_GEN_DWC3_RESET_N else '0'}"
        f" FORCE_CLAMP={'1' if aon & AON_GEN_DWC3_FORCE_CLAMP_EN else '0'}")
    say(f"    NONSELECTED_OVERRIDE = {ovr:#010x}"
        f"  DUMMY_PHY_EN={'1' if ovr & DUMMY_PHY_EN else '0'}"
        f" NATIVE_RESET={'1' if ovr & NATIVE_RESET else '0'}"
        f" NATIVE_POWER_DOWN={ovr & NATIVE_POWER_DOWN:#x}")
    return mux, aon, ovr


def port_protocol_map(xhci, hccparams1):
    """
    Walk the xHCI extended capability list and return {port_number: major_rev}.

    Supported Protocol capability (ID 2) layout:
        +0x00  [7:0] CapID, [15:8] NextPtr (dwords), [23:16] MinorRev, [31:24] MajorRev
        +0x08  [7:0] CompatiblePortOffset (1-based), [15:8] CompatiblePortCount
    """
    ports = {}
    xecp = (hccparams1 >> 16) & 0xFFFF
    if xecp == 0:
        say("    no extended capabilities")
        return ports

    off = xecp * 4
    for _ in range(32):                        # bounded: never trust a NextPtr chain
        dw0 = p.read32(xhci + off)
        cap_id = dw0 & 0xFF
        next_ptr = (dw0 >> 8) & 0xFF
        if cap_id == 2:
            major = (dw0 >> 24) & 0xFF
            dw2 = p.read32(xhci + off + 8)
            first = dw2 & 0xFF
            count = (dw2 >> 8) & 0xFF
            name = p.read32(xhci + off + 4)
            name_s = "".join(chr((name >> (8 * i)) & 0xFF) for i in range(4))
            say(f"    xECP@{off:#x}: Supported Protocol '{name_s}' USB {major}.x, "
                f"ports {first}..{first + count - 1}")
            for pn in range(first, first + count):
                ports[pn] = major
        if next_ptr == 0:
            break
        off += next_ptr * 4
    return ports


def read_portsc(xhci, caplen, port, protocol):
    """
    THE RISKY READ. PORTSC for port N (1-based) is at operational base + 0x400 +
    (N-1) * 0x10.
    """
    addr = xhci + caplen + 0x400 + (port - 1) * 0x10
    tag = f"USB {protocol}.x" if protocol else "unknown protocol"
    say(f"    about to read PORTSC port {port} ({tag}) at {addr:#x} ...")
    try:
        val = p.read32(addr)
    except Exception as e:
        say(f"      READ FAILED/TIMED OUT: {e!r}")
        say(f"      ^^ this is the hang. Port {port} ({tag}) PORTSC is unreadable.")
        return None
    say(f"      PORTSC = {val:#010x}"
        f"  CCS={val & 1} PED={(val >> 1) & 1} PP={(val >> 9) & 1}"
        f" Speed={(val >> 10) & 0xF} PLS={(val >> 5) & 0xF}")
    return val


def unpark(pipe):
    say("    UNPARK: clearing NATIVE_RESET and NATIVE_POWER_DOWN, mux -> USB3")
    ovr = p.read32(pipe + PIPEHANDLER_NONSELECTED_OVERRIDE)
    new = (ovr & ~(NATIVE_RESET | DUMMY_PHY_EN | NATIVE_POWER_DOWN))
    say(f"      NONSELECTED_OVERRIDE {ovr:#010x} -> {new:#010x}")
    p.write32(pipe + PIPEHANDLER_NONSELECTED_OVERRIDE, new)
    p.write32(pipe + PIPEHANDLER_MUX_CTRL, MUX_CTRL_USB3)
    say("      done; give the PHY a moment")
    p.nop()


say("=" * 68)
say("xHCI PORTSC probe -- see tools/usb_portsc_probe.py header for rationale")
say(f"UNPARK = {UNPARK}")
say("=" * 68)

for idx in CANDIDATE_PORTS:
    say("")
    say(f"usb-drd{idx}:")
    regs = node_regs(idx)
    if regs is None:
        continue
    xhci, pipe = regs
    say(f"  xHCI base        = {xhci:#x}")
    say(f"  pipehandler base = {pipe:#x}" if pipe else "  pipehandler base = <none>")

    if pipe is not None:
        say("  pipehandler state (always-on domain, safe to read):")
        show_pipehandler(pipe)

    # Capability registers: UEFI already read these successfully, so they are safe.
    say("  xHCI capability registers:")
    caplen_hciver = p.read32(xhci + 0x00)
    caplen = caplen_hciver & 0xFF
    hcsparams1 = p.read32(xhci + 0x04)
    hccparams1 = p.read32(xhci + 0x10)
    maxports = (hcsparams1 >> 24) & 0xFF
    say(f"    CAPLENGTH = {caplen:#x}  HCIVERSION = {(caplen_hciver >> 16) & 0xFFFF:#x}")
    say(f"    HCSPARAMS1 = {hcsparams1:#010x}  MaxPorts = {maxports}")
    say(f"    HCCPARAMS1 = {hccparams1:#010x}")

    say("  supported protocols:")
    proto = port_protocol_map(xhci, hccparams1)

    if UNPARK and pipe is not None:
        if idx in UNPARK_PORTS:
            unpark(pipe)
        else:
            say(f"    UNPARK requested but port {idx} is not in UNPARK_PORTS "
                f"{UNPARK_PORTS} -- not writing anything")

    say("  PORTSC reads (this is where UEFI dies):")
    for port in range(1, maxports + 1):
        if read_portsc(xhci, caplen, port, proto.get(port)) is None:
            say("  stopping this controller: a PORTSC read did not come back")
            break

say("")
say("=" * 68)
say("Interpreting the result:")
say("")
say("  a PORTSC read hung              -> parked PIPE PHY confirmed as at least one")
say("      real problem. Boot J704_UEFI_unpark.fd, which calls Dwc3UnparkPipePhy()")
say("      before DWC3 core init. Re-run with UNPARK = True (and UNPARK_PORTS set")
say("      to a port that is NOT the console) for direct proof.")
say("")
say("  every PORTSC reads fine         -> the port registers are innocent, and the")
say("      UEFI hang is elsewhere. This does NOT mean 'HV mapping': ")
say("      hv/__init__.py maps every /arm-io range 1:1 as TraceMode.OFF (straight")
say("      stage-2 passthrough, no trapping), so a guest read is identical to the")
say("      read this script just did. Get a backtrace instead.")
say("")
say("CAVEAT on the UEFI log: UsbEnumeratePort returns early and SILENTLY when no")
say("port-change bits are set (UsbEnumer.c:932). So 'no port-state line after")
say("UsbRootHubInit' does not prove the hang is in GetPortStatus -- with nothing")
say("plugged in, enumeration can finish quietly and the hang be later. The")
say("backtrace (tools/hv_backtrace.py) is the only thing that actually locates it.")
say("=" * 68)
