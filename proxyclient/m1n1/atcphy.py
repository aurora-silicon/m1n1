# SPDX-License-Identifier: MIT
"""
Apple Type-C PHY (ATCPHY) proxyclient driver for T6020 (J414s / M2 Pro).

Companion to the in-m1n1 driver src/atcphy.{h,c} / src/atcphy_core.{h,c};
see docs/j414s-atcphy.md for the full design note, register provenance and
the safety discussion. Register offsets/bit names below are transcribed
from the same sources as the C driver (Asahi Linux drivers/phy/apple/atc.c
@ 030248d39b40 -- reverse engineered from XNU debug output -- plus a live
J414s ADT capture for the window/reg[] mapping); the inspection helpers
here deliberately reuse the exact offsets the C side uses so a divergence
shows up as a visible mismatch, not silent misdecoding.

Usage from a proxyclient shell/experiment:

    from m1n1.atcphy import ATCPHY, ATCPHYMode
    phy = ATCPHY(u, 2)              # right-side port on J414s
    phy.dump()                      # read-only state dump, safe anytime
    phy.set_orientation(flipped)    # USB2-safe: never touches the PIPE mux
    phy.apply_mode(ATCPHYMode.USB3, flipped, allow_pipe_switch=True)
    phy.power_off()                 # upstream-faithful full power-down

SAFETY: apply_mode with USB3/USB3_DP reprograms the pipehandler PIPE mux
that DWC3's SuperSpeed path depends on. Never do that while a guest OS has
an active xHCI driver bound to the port -- see docs/j414s-atcphy.md sec 6.
The C side refuses unless allow_pipe_switch is explicitly passed.
"""

from enum import IntEnum

__all__ = [
    "ATCPHY", "ATCPHYMode", "ATCPHYDpRate", "ATCPHYBlock",
    "ATCPHYPipePolicy",
    "hpm_status", "hpm_orientation",
]

# Proxy opcodes -- keep in sync with src/proxy.h (P_ATCPHY_* = 0x1500 group).
P_ATCPHY_APPLY_MODE = 0x1500
P_ATCPHY_SET_ORIENTATION = 0x1501
P_ATCPHY_POWER_OFF = 0x1502
P_ATCPHY_GET_REG_BASE = 0x1503
P_ATCPHY_ARM_GUEST_MODE = 0x1504
P_ATCPHY_READ_ORIENTATION = 0x1505
P_ATCPHY_READ_LINK_STATE = 0x1506
P_ACIO_TYPE5_FW_START = 0x1507
P_ACIO_TYPE5_ABORT = 0x1508
P_ACIO_TYPE5_PHASE = 0x1509
P_ACIO_TYPE5_CONFIG_READ32 = 0x150A
P_ACIO_TYPE5_CONFIG_WRITE32 = 0x150B
P_ACIO_TYPE5_ROUTER_CONFIGURE = 0x150C
P_ACIO_TYPE5_STATUS = 0x150D

# Keep in sync with enum acio_type5_runtime_phase (src/acio.h).
ACIO_PHASES = {
    0: "OFF", 1: "PHY_PREPARED", 2: "POWERED", 3: "FW_READY",
    4: "CONTROL_READY", 5: "ROUTER_READY", 6: "TUNNEL_READY",
    7: "PIPE_COMMITTED", 8: "FAILED",
}

# Keep in sync with enum acio_type5_error / acio_type5_error_name()
# (src/acio.h, src/acio_type5.c). The high byte is the phase group, so an
# unrecognised code still localises the failure instead of decoding to
# nothing.
ACIO_ERRORS = {
    0x0000: "none",
    0x0101: "bad-index", 0x0102: "bad-phase", 0x0103: "resource-discovery",
    0x0201: "power-state5", 0x0202: "power-state7", 0x0203: "cio-reconfig",
    0x0301: "phy-prepare",
    0x0401: "fw-bundle", 0x0402: "fw-copy", 0x0403: "fw-release",
    0x0404: "fw-ready-timeout",
    0x0501: "tunables",
    0x0601: "ctrl-sid-layout", 0x0602: "ctrl-path-count",
    0x0603: "ctrl-dart-init", 0x0604: "ctrl-dart-force-active",
    0x0605: "ctrl-alloc", 0x0606: "ctrl-iova", 0x0607: "ctrl-map",
    0x0608: "ctrl-enable", 0x0609: "ctrl-disable-done",
    0x0701: "cfg-busy", 0x0702: "cfg-pack", 0x0703: "cfg-descriptor",
    0x0704: "cfg-tx-timeout", 0x0705: "cfg-rx-timeout",
    0x0706: "cfg-rx-length", 0x0707: "cfg-rx-pdf", 0x0708: "cfg-rx-reject",
    0x0709: "cfg-ring-advance",
    0x0801: "router-poll-ready", 0x0802: "router-read",
    0x0803: "router-write", 0x0804: "router-poll-ack", 0x0805: "router-state",
    0x0901: "tunnel-state",
}

ACIO_ERROR_GROUPS = {
    0x01: "resources", 0x02: "power", 0x03: "phy", 0x04: "firmware",
    0x05: "tunables", 0x06: "control-ring", 0x07: "config", 0x08: "router",
    0x09: "tunnel",
}


def acio_error_name(code):
    """Decode an ACIO Type5 error code, never raising and never returning None."""
    if code in ACIO_ERRORS:
        return ACIO_ERRORS[code]
    group = ACIO_ERROR_GROUPS.get((code >> 8) & 0xFF)
    if group is not None:
        return f"unknown-{group}-{code:#06x}"
    return f"unknown-{code:#06x}"

# P_ATCPHY_READ_ORIENTATION reply ABI -- keep in sync with the comment on the
# opcode in src/proxy.h.  All-ones means "the read failed"; it cannot collide
# with a valid reply because a valid reply always has bit 32 set and bits
# 63:35 clear.
ATCPHY_ORIENTATION_READ_FAILED = 0xFFFFFFFFFFFFFFFF
ATCPHY_ORIENTATION_VALID = 1 << 32
ATCPHY_ORIENTATION_PLUG_PRESENT = 1 << 33
ATCPHY_ORIENTATION_FLIPPED = 1 << 34

ATCPHY_LINK_STATE_READ_FAILED = 0xFFFFFFFFFFFFFFFF
ATCPHY_LINK_STATE_VALID = 1 << 40
ATCPHY_USB4_DETAIL_VALID = 1 << 63


class ATCPHYMode(IntEnum):
    # matches atcphy_mode_t (src/atcphy_core.h), itself from atc.c enum atcphy_mode
    OFF = 0
    USB2 = 1
    USB3 = 2
    USB3_DP = 3
    TBT = 4
    USB4 = 5
    DP = 6


class ATCPHYDpRate(IntEnum):
    # matches atcphy_dp_rate_t (src/atcphy_core.h)
    RBR = 0
    HBR = 1
    HBR2 = 2
    HBR3 = 3


class ATCPHYBlock(IntEnum):
    # matches atcphy_block_t (src/atcphy_core.h)
    USB2PHY = 0
    CORE = 1
    PIPEHANDLER = 2
    AXI2AF = 3
    LPDPTX = 4


class ATCPHYPipePolicy(IntEnum):
    """What m1n1 may do with the DWC3 PIPE mux for a USB3 mode."""
    REFUSE = 0
    SWITCH = 1
    DEFER = 2


# ACIOPHY_CROSSBAR[4:0] protocol values (atc.c:136-142)
CROSSBAR_PROTOCOLS = {
    0x00: "USB4",
    0x01: "USB4_SWAPPED",
    0x0A: "USB3",
    0x0B: "USB3_SWAPPED",
    0x10: "USB3_DP",
    0x11: "USB3_DP_SWAPPED",
    0x14: "DP",
}

# ACIOPHY_LANE_MODE 3-bit field values (atc.c:156-159)
LANE_MODES = {0: "USB4", 1: "USB3", 2: "DP", 3: "OFF"}

# Pipehandler MUX_CTRL CLK/DATA field values (atc.c:440-447)
PIPE_CLK = {0: "OFF", 1: "USB3", 2: "USB4", 4: "DUMMY"}
PIPE_DATA = {0: "USB3", 1: "USB4", 2: "DUMMY"}


def _decode_bits(val, bits):
    """bits: {name: bit_index}; returns list of set names."""
    return [name for name, bit in bits.items() if val & (1 << bit)]


class ATCPHY:
    """Driver/inspector for one ATC PHY instance, addressed like the C side."""

    # ADT reg[] indices, confirmed against a live J414s ADT capture
    # (docs/j414s-atcphy.md sec 2.1); MUST match src/atcphy.c.
    ATC_REG_USB2PHY = 0
    ATC_REG_CORE = 3
    ATC_REG_LPDPTX = 20
    ATC_REG_AXI2AF = 24
    DRD_REG_PIPEHANDLER = 3

    def __init__(self, u, port, check_against_m1n1=True):
        self.u = u
        self.p = u.proxy
        self.port = port

        atc = u.adt[f"/arm-io/atc-phy{port}"]
        drd = u.adt[f"/arm-io/usb-drd{port}"]
        self.dwc3 = drd.get_reg(0)[0]
        self.usb2phy = atc.get_reg(self.ATC_REG_USB2PHY)[0]
        self.core = atc.get_reg(self.ATC_REG_CORE)[0]
        self.lpdptx = atc.get_reg(self.ATC_REG_LPDPTX)[0]
        self.axi2af = atc.get_reg(self.ATC_REG_AXI2AF)[0]
        self.pipehandler = drd.get_reg(self.DRD_REG_PIPEHANDLER)[0]

        if check_against_m1n1:
            self._crosscheck()

    def _crosscheck(self):
        """Compare our Python-side ADT resolution against the C driver's.

        This project has been bitten by divergent address decodes before
        (see the 'verify struct offsets, not headers' rule); a mismatch here
        means one side is decoding the ADT wrong and NOTHING should be
        poked until that is understood.
        """
        want = {
            ATCPHYBlock.USB2PHY: self.usb2phy,
            ATCPHYBlock.CORE: self.core,
            ATCPHYBlock.PIPEHANDLER: self.pipehandler,
            ATCPHYBlock.AXI2AF: self.axi2af,
            ATCPHYBlock.LPDPTX: self.lpdptx,
        }
        for block, addr in want.items():
            got = self.p.request(P_ATCPHY_GET_REG_BASE, self.port, int(block))
            if got == 0:
                raise Exception(
                    f"atcphy{self.port}: m1n1 could not resolve block "
                    f"{block.name} (old m1n1 without ATCPHY support?)")
            if got != addr:
                raise Exception(
                    f"atcphy{self.port}: block {block.name} address mismatch: "
                    f"python={addr:#x} m1n1={got:#x} -- do not proceed")

    # ---- proxy-driven actions (run the in-m1n1 C driver) ----

    def set_orientation(self, flipped):
        """USB2-safe orientation programming; never switches the PIPE mux."""
        ret = self.p.request(P_ATCPHY_SET_ORIENTATION, self.port,
                             int(bool(flipped)), signed=True)
        if ret < 0:
            raise Exception(f"atcphy{self.port}: set_orientation failed "
                            "(see m1n1 console for the failing op)")
        return ret

    def apply_mode(self, mode, flipped=False,
                   pipe_policy=ATCPHYPipePolicy.REFUSE, dp_rate=None,
                   *, allow_pipe_switch=None):
        """Full mode application. See module docstring for the safety rule."""
        if allow_pipe_switch is not None:
            if pipe_policy != ATCPHYPipePolicy.REFUSE:
                raise ValueError("pass pipe_policy or allow_pipe_switch, not both")
            pipe_policy = (ATCPHYPipePolicy.SWITCH if allow_pipe_switch
                           else ATCPHYPipePolicy.REFUSE)
        pipe_policy = ATCPHYPipePolicy(pipe_policy)
        ret = self.p.request(
            P_ATCPHY_APPLY_MODE, self.port, int(mode), int(bool(flipped)),
            int(pipe_policy),
            int(dp_rate is not None),
            int(dp_rate) if dp_rate is not None else 0, signed=True)
        if ret < 0:
            raise Exception(f"atcphy{self.port}: apply_mode({mode!r}) failed "
                            "(see m1n1 console for the failing op)")
        return ret

    def power_off(self):
        """Upstream-faithful power-down (usb2 off + DP AUX off + core sleep)."""
        ret = self.p.request(P_ATCPHY_POWER_OFF, self.port, signed=True)
        if ret < 0:
            raise Exception(f"atcphy{self.port}: power_off failed")
        return ret

    def arm_guest_mode(self, mode, flipped=False, armed=True,
                       pipe_policy=ATCPHYPipePolicy.REFUSE):
        """Arm a PHY mode to be re-applied by m1n1's guest-handoff path.

        WHY THIS EXISTS: booting a guest through the hypervisor runs
        hv_init -> usb_iodev_shutdown_except -> usb_phy_handoff_host,
        which re-parks the PIPE mux on the DUMMY backend (usb.c) --
        silently undoing any proxy-applied USB3 state. Arming makes the
        handoff path re-apply (mode, flipped) right after its dummy
        parking, i.e. at the last point before the guest owns the port
        and while no guest xHCI driver is bound yet (the one window
        where a PIPE-mux switch is safe by this project's own rule).

        Fail-safe: nothing is armed by default; arming does not touch
        the hardware until the handoff actually runs. armed=False
        disarms.
        """
        pipe_policy = ATCPHYPipePolicy(pipe_policy)
        ret = self.p.request(P_ATCPHY_ARM_GUEST_MODE, self.port, int(mode),
                             int(bool(flipped)), int(bool(armed)),
                             int(pipe_policy), signed=True)
        if ret < 0:
            raise Exception(f"atcphy{self.port}: arm_guest_mode failed "
                            "(old m1n1 without ATCPHY arm support?)")
        return ret

    def read_orientation(self):
        """Read this port's true cable orientation from its CD3217.

        Returns (plug_present, flipped, raw_status).

        Read-only: the C side issues one SMBus read of STATUS (0x1a) and
        never writes to the PD controller -- this machine's CD3217 rejects
        System Configuration writes and refused an earlier PortInfo
        rewrite, so the whole path is deliberately interrogative.

        Raises on a failed read rather than returning a default. There is
        no safe default: a wrong orientation puts the SuperSpeed pairs on
        the partner's transmit pins, the link never trains, and the device
        silently falls back to USB2 -- which looks exactly like a broken
        port. A caller that wants to proceed anyway must say so explicitly.

        Note this is the C-side (in-m1n1) path. hpm_status()/
        hpm_orientation() at the bottom of this module do the same read
        from Python; they are kept because they need no new m1n1 build,
        and because two independent decodes of the same register is how
        this project catches a bad transcription.
        """
        raw = self.p.request(P_ATCPHY_READ_ORIENTATION, self.port)
        if raw == ATCPHY_ORIENTATION_READ_FAILED or not (
                raw & ATCPHY_ORIENTATION_VALID):
            raise Exception(
                f"atcphy{self.port}: orientation read failed (see the m1n1 "
                "console for which step refused). Do NOT guess an "
                "orientation from this.")
        return (bool(raw & ATCPHY_ORIENTATION_PLUG_PRESENT),
                bool(raw & ATCPHY_ORIENTATION_FLIPPED),
                raw & 0xFFFFFFFF)

    def read_link_state(self):
        """Return one read-only ``(status_byte0, data_status)`` sample.

        Unlike :meth:`read_orientation`, this also reports the negotiated
        transport. Callers selecting the direct DWC3 PIPE must reject USB4,
        Thunderbolt, and DisplayPort contracts even when USB2 is present.
        """
        raw = self.p.request(P_ATCPHY_READ_LINK_STATE, self.port)
        if raw == ATCPHY_LINK_STATE_READ_FAILED or not (
                raw & ATCPHY_LINK_STATE_VALID):
            raise Exception(
                f"atcphy{self.port}: CD3217 link-state read failed; "
                "refusing to infer a transport")
        return ((raw >> 32) & 0xFF, raw & 0xFFFFFFFF)

    def read_usb4_state(self):
        """Return ``(mode_status, eudo, apple_cable_info)`` read-only.

        This is the companion selector to :meth:`read_link_state`.  The C
        side obtains the detail from the same bounded CD321x sample and
        fails rather than fabricating cable/router input.
        """
        raw = self.p.request(P_ATCPHY_READ_LINK_STATE, self.port, 1)
        if raw == ATCPHY_LINK_STATE_READ_FAILED or not (
                raw & ATCPHY_USB4_DETAIL_VALID):
            raise Exception(
                f"atcphy{self.port}: CD3217 USB4 detail read failed; "
                "refusing to infer EUDO/cable state")
        return ((raw >> 32) & 0xFF, raw & 0xFFFFFFFF,
                (raw >> 40) & 0xFFFF)

    def usb4_config_read32(self, route, port, space, offset):
        """Read one USB4 fabric config dword through m1n1's bounded ring.

        Requires a Type5 runtime at CONTROL_READY or later. ``space`` is
        0=hops, 1=adapter, 2=router, 3=counters.
        """
        if not 0 <= int(space) <= 3:
            raise ValueError("USB4 config space must be 0..3")
        raw = self.p.request(P_ACIO_TYPE5_CONFIG_READ32, self.port,
                             int(route), int(port), int(space), int(offset))
        if raw == 0xFFFFFFFFFFFFFFFF or not raw & (1 << 32):
            raise Exception(
                f"acio{self.port}: USB4 config read failed "
                f"(route={route:#x} port={port} space={space} offset={offset:#x})")
        return raw & 0xFFFFFFFF

    def usb4_config_write32(self, route, port, space, offset, value):
        """Write one USB4 fabric config dword through the bounded ring."""
        if not 0 <= int(space) <= 3:
            raise ValueError("USB4 config space must be 0..3")
        ret = self.p.request(P_ACIO_TYPE5_CONFIG_WRITE32, self.port,
                             int(route), int(port), int(space), int(offset),
                             int(value), signed=True)
        if ret < 0:
            raise Exception(
                f"acio{self.port}: USB4 config write failed "
                f"(route={route:#x} port={port} space={space} offset={offset:#x})")
        return ret

    def usb4_router_configure(self, route=0, all_parents_support_usb=True):
        """Run the root-router configuration handshake on the live ring.

        Requires a Type5 runtime at CONTROL_READY. Creates no tunnel and
        never commits the PIPE mux, so this is the last step that can be
        taken before the port is committed to USB4. On failure, read
        :meth:`usb4_status` for the exact phase rather than re-running.
        """
        ret = self.p.request(P_ACIO_TYPE5_ROUTER_CONFIGURE, self.port,
                             int(route), int(bool(all_parents_support_usb)),
                             signed=True)
        if ret < 0:
            st = self.usb4_status()
            raise Exception(
                f"acio{self.port}: root-router configure failed at phase "
                f"{st['phase_name']}: {st['error_name']} "
                f"(detail={st['error_detail']:#x}, router_state={st['router_state']})")
        return ret

    def usb4_status(self):
        """Bounded telemetry snapshot. Touches no hardware; safe anytime."""
        raw = [self.p.request(P_ACIO_TYPE5_STATUS, self.port, sel)
               for sel in range(4)]
        if any(v == 0xFFFFFFFFFFFFFFFF for v in raw):
            raise Exception(f"acio{self.port}: status read rejected "
                            "(old m1n1 without Type5 telemetry?)")
        phase = (raw[0] >> 32) & 0xFFFFFFFF
        error = raw[0] & 0xFFFFFFFF
        return {
            "phase": phase,
            "phase_name": ACIO_PHASES.get(phase, f"UNK_{phase}"),
            "error": error,
            "error_name": acio_error_name(error),
            "error_detail": (raw[1] >> 32) & 0xFFFFFFFF,
            "error_count": raw[1] & 0xFFFFFFFF,
            "config_requests": (raw[2] >> 32) & 0xFFFFFFFF,
            "config_failures": raw[2] & 0xFFFFFFFF,
            "router_state": raw[3] & 0xFFFFFFFF,
        }

    def dump_tunables(self):
        """List the ADT tunable_* properties on this port's atc-phy node.

        Read-only (ADT parse only, no MMIO). Each tunable blob is a
        sequence of 12-byte {offset:24,size:8,mask:32,value:32} RMW
        records; the C driver validates and applies them during
        apply_mode (global set always; USB3/DP/CIO lane sets by mode).
        A present-but-empty blob is a valid no-op.
        """
        node = self.u.adt[f"/arm-io/atc-phy{self.port}"]
        out = {}
        for name in sorted(node._properties.keys()):
            if not name.startswith("tunable"):
                continue
            val = node._properties[name]
            nbytes = len(val) if isinstance(val, (bytes, bytearray)) else None
            if nbytes is None:
                out[name] = ("?", val)
                print(f"  {name:32s} unparsed type {type(val).__name__}")
            else:
                recs = nbytes // 12
                ok = "" if nbytes % 12 == 0 else "  (NOT a multiple of 12!)"
                out[name] = (nbytes, recs)
                print(f"  {name:32s} {nbytes:5d} bytes = {recs:3d} records{ok}")
        if not out:
            print(f"atcphy{self.port}: NO tunable_* properties found -- "
                  "apply_mode would have failed closed; investigate")
        return out

    # ---- read-only inspection (pure MMIO reads, no C driver involved) ----

    def state(self):
        """Read and decode PHY state. Read-only; never writes any register.

        Reads the ATC PHY, pipehandler, and the two side-effect-free DWC3 PHY
        control registers needed to decide whether the deferred handoff took.
        """
        r32 = self.p.read32
        st = {}

        # core power/misc block (core + 0x20000, atc.c:193-202)
        pwr_ctrl = r32(self.core + 0x20000)
        pwr_stat = r32(self.core + 0x20004)
        misc = r32(self.core + 0x20008)
        pwr_bits = {"SLEEP_SMALL": 0, "SLEEP_BIG": 1, "CLAMP_EN": 2,
                    "APB_RESET_N": 3, "PHY_RESET_N": 4}
        st["power_ctrl"] = (pwr_ctrl, _decode_bits(pwr_ctrl, pwr_bits))
        st["power_stat"] = (pwr_stat, _decode_bits(pwr_stat, pwr_bits))
        st["misc"] = (misc, _decode_bits(misc, {"RESET_N": 0, "LANE_SWAP": 2}))

        # crossbar + lane mode (atc.c:134-159)
        xbar = r32(self.core + 0x4C)
        proto = xbar & 0x1F
        st["crossbar"] = (xbar, {
            "protocol": CROSSBAR_PROTOCOLS.get(proto, f"UNK_{proto:#x}"),
            "dp_single_pma": (xbar >> 5) & 0xFFF,
            "dp_both_pma": bool(xbar & (1 << 17)),
        })
        lm = r32(self.core + 0x48)
        st["lane_mode"] = (lm, {
            "rx0": LANE_MODES.get((lm >> 0) & 7, f"UNK{(lm>>0)&7}"),
            "tx0": LANE_MODES.get((lm >> 3) & 7, f"UNK{(lm>>3)&7}"),
            "rx1": LANE_MODES.get((lm >> 6) & 7, f"UNK{(lm>>6)&7}"),
            "tx1": LANE_MODES.get((lm >> 9) & 7, f"UNK{(lm>>9)&7}"),
        })

        st["cfg0"] = (r32(self.core + 0x08), None)
        st["sleep_ctrl"] = (r32(self.core + 0x1B0), None)
        st["auspll_fsm_ctrl"] = (r32(self.core + 0x1014), None)
        st["auspll_cmd_override"] = (r32(self.core + 0x2000), None)
        st["cio3pll_clk_ctrl"] = (r32(self.core + 0x2A00),
                                  _decode_bits(r32(self.core + 0x2A00),
                                               {"PCLK_EN": 1, "REFCLK_EN": 5}))

        # pipehandler (usb-drd reg[3], atc.c:427-462)
        mux = r32(self.pipehandler + 0x0C)
        clk = (mux >> 3) & 7
        data = mux & 7
        st["pipe_mux_ctrl"] = (mux, {
            "clk": PIPE_CLK.get(clk, f"UNK{clk}"),
            "data": PIPE_DATA.get(data, f"UNK{data}"),
        })
        st["pipe_override"] = (r32(self.pipehandler + 0x00), None)
        st["pipe_override_values"] = (r32(self.pipehandler + 0x04), None)
        st["pipe_lock_req"] = (r32(self.pipehandler + 0x10), None)
        st["pipe_lock_ack"] = (r32(self.pipehandler + 0x14), None)
        st["pipe_aon_gen"] = (r32(self.pipehandler + 0x1C), None)
        st["pipe_nonselected_override"] = (r32(self.pipehandler + 0x20), None)

        # DWC3 global PHY controls. These are read-only observations; unlike
        # xHCI PORTSC they contain no write-one-to-clear status bits.
        st["dwc3_gusb2phycfg0"] = (r32(self.dwc3 + 0xC200),
                                    _decode_bits(r32(self.dwc3 + 0xC200),
                                                 {"SUSPHY": 6}))
        st["dwc3_gusb3pipectl0"] = (r32(self.dwc3 + 0xC2C0),
                                     _decode_bits(r32(self.dwc3 + 0xC2C0),
                                                  {"SUSPHY": 17,
                                                   "PHYSOFTRST": 31}))

        # usb2phy (atc-phy reg[0], usb.c:59-77)
        st["usb2_usbctl"] = (r32(self.usb2phy + 0x00), None)
        st["usb2_ctl"] = (r32(self.usb2phy + 0x04),
                          _decode_bits(r32(self.usb2phy + 0x04),
                                       {"RESET": 0, "PORT_RESET": 1,
                                        "APB_RESET_N": 2, "SIDDQ": 3}))
        st["usb2_sig"] = (r32(self.usb2phy + 0x08), None)
        st["usb2_misctune"] = (r32(self.usb2phy + 0x1C), None)

        return st

    def dump(self):
        """Pretty-print state(). Read-only; safe to run at any time."""
        st = self.state()
        print(f"=== atcphy{self.port} "
              f"(core={self.core:#x} usb2phy={self.usb2phy:#x} "
              f"pipehandler={self.pipehandler:#x} dwc3={self.dwc3:#x}) ===")
        for key, (val, decoded) in st.items():
            if decoded is None:
                print(f"  {key:26s} = {val:#010x}")
            elif isinstance(decoded, list):
                print(f"  {key:26s} = {val:#010x}  [{' '.join(decoded) or '-'}]")
            else:
                pretty = " ".join(f"{k}={v}" for k, v in decoded.items())
                print(f"  {key:26s} = {val:#010x}  [{pretty}]")
        return st


# ---- HPM (CD3217/TPS6598x PD controller) orientation sensing ----
#
# The physical plug orientation lives in the PD controller, not the PHY.
# STATUS byte0 layout was measured live on this machine's hpm2 (right port,
# i2c0 addr 0x3b): bit0 PLUG_PRESENT, bit4 PLUG_UPSIDE_DOWN, bit5 PORTROLE,
# bit6 DATAROLE, bit7 VCONN (docs/j414s-atcphy.md sec 0). The register
# NUMBER (0x1A) follows the TI TPS6598x convention as used by Linux's tipd
# driver (TPS_REG_STATUS) -- not independently re-verified on this machine,
# flagged here so a wrong decode is suspected FIRST if byte0 looks insane.
# Reads are SMBus block reads: first returned byte is the payload length.

TPS_REG_STATUS = 0x1A

# Hardware-measured corroboration anchors (grade A, 2026-07-29 session):
# the right-side port's PD controller is hpm2 at I2C address 0x3b. If the
# ADT decode below disagrees with this, the DECODE is wrong -- refuse to
# talk to the bus rather than address a random device.
HPM_KNOWN_ADDRS = {2: 0x3B}


def _prop_to_int(val):
    """ADT property value -> int, for untemplated little-endian props."""
    if isinstance(val, int):
        return val
    if isinstance(val, (bytes, bytearray)):
        if len(val) == 0:
            raise ValueError("empty property")
        return int.from_bytes(val, "little")
    raise ValueError(f"unhandled ADT property type {type(val).__name__}")


def _hpm_i2c_addr(u, hpm_index, i2c_path="/arm-io/i2c0"):
    """Resolve an HPM's I2C bus address from the ADT.

    The address lives in the hpm node's `hpm-iic-addr` property -- the
    exact property m1n1's own working C driver reads (tps6598x_init,
    src/tps6598x.c:36). The hpm nodes have NO `reg` property, and this
    project's standing lesson is that raw ADT `reg` is not an address
    anyway (the i2c0 raw-reg trap: bogus 0x19b040000 vs the real
    0x39b040000 from get_reg(0)) -- so nothing here touches `reg`.
    """
    target = f"hpm{hpm_index}"
    node = u.adt[i2c_path]

    hpm_node = None
    # ADT layout: i2c0 -> hpmBusManagerX ("usbc,manager") -> hpmN. Walk one
    # level of managers plus direct children, matching usb.c's discovery.
    candidates = list(node)
    for mgr in node:
        candidates.extend(list(mgr))
    for child in candidates:
        if child.name == target:
            hpm_node = child
            break
    if hpm_node is None:
        seen = sorted(c.name for c in candidates)
        raise Exception(
            f"{i2c_path}: no '{target}' node found (children seen: {seen})")

    try:
        raw = hpm_node.hpm_iic_addr  # ADT property "hpm-iic-addr"
    except AttributeError:
        props = sorted(hpm_node._properties.keys())
        raise Exception(
            f"{hpm_node._path}: no 'hpm-iic-addr' property (properties "
            f"present: {props}) -- cannot determine the I2C address, "
            f"refusing to guess")

    try:
        addr = _prop_to_int(raw)
    except ValueError as e:
        raise Exception(
            f"{hpm_node._path}: cannot decode 'hpm-iic-addr' value "
            f"{raw!r}: {e}")

    if not 0x08 <= addr <= 0x77:
        raise Exception(
            f"{hpm_node._path}: decoded I2C address {addr:#x} is outside "
            f"the valid 7-bit range 0x08-0x77 -- decode is wrong, refusing")

    known = HPM_KNOWN_ADDRS.get(hpm_index)
    if known is not None and addr != known:
        raise Exception(
            f"{hpm_node._path}: decoded I2C address {addr:#04x} contradicts "
            f"the hardware-measured value {known:#04x} for hpm{hpm_index} -- "
            f"decode is wrong, refusing to talk to the bus")

    return addr


def hpm_status(u, hpm_index, i2c_path="/arm-io/i2c0"):
    """Block-read the HPM STATUS register; returns (byte0, decoded_dict).

    WARNING: this talks on the same I2C bus as the HPM backing the m1n1
    proxy's own console port (hpm0). It only READS one register, which the
    earlier hardware session did repeatedly without issue, but treat any
    write extension of this helper as proxy-threatening until proven.
    """
    from m1n1.hw.i2c import I2C

    u.proxy.pmgr_adt_clocks_enable(i2c_path)
    i2c = I2C(u, i2c_path)
    addr = _hpm_i2c_addr(u, hpm_index, i2c_path)
    print(f"hpm{hpm_index}: ADT-resolved I2C address {addr:#04x} on {i2c_path}")

    raw = i2c.read_reg(addr, TPS_REG_STATUS, 9)
    length = raw[0]
    if length == 0 or length > 8:
        raise Exception(f"hpm{hpm_index}: implausible STATUS block length "
                        f"{length} (raw={raw.hex()})")
    b0 = raw[1]
    decoded = {
        "plug_present": bool(b0 & (1 << 0)),
        "upside_down": bool(b0 & (1 << 4)),
        "port_role_source": bool(b0 & (1 << 5)),
        "data_role_host": bool(b0 & (1 << 6)),
        "vconn": bool(b0 & (1 << 7)),
        "raw": raw[1:1 + length].hex(),
    }
    return b0, decoded


def hpm_orientation(u, hpm_index, i2c_path="/arm-io/i2c0"):
    """Returns (plug_present, flipped) for the given HPM."""
    _, dec = hpm_status(u, hpm_index, i2c_path)
    return dec["plug_present"], dec["upside_down"]
