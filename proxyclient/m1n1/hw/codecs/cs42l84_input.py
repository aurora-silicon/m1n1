# SPDX-License-Identifier: MIT
"""CS42L84 headset input lifecycle for the native 48kHz mono TDM profile.

The caller supplies bounded read(address, width)/write(address, width, value)
access, power/reset management, and receiver ownership gates. Gates raise or
return False when their prerequisite is not satisfied. This class opens
no transport and configures no receiver. TX1 carries the ADC in a 32-bit word
with 24 significant bits, slot0/rising edge, using a 3.072MHz external BCLK.
After any uncertainty the caller must retain resources and recover the device;
this class will issue no further codec accesses.
"""
import time

from .cs42l84 import CS42L84Regs, E_BUS_SOURCE, E_MCLK_FREQ, E_MCLK_SRC


class CS42L84Input:
    def __init__(self, read, write, persist=lambda state: None, *,
                 clock=time.monotonic, sleep=time.sleep):
        self.read = read
        self.write = write
        self.persist = persist
        self.clock = clock
        self.sleep = sleep
        self.state = {"operations": [], "uncertain": False, "tx_enabled": False}
        self.saved = {}
        self.expected = {}
        self._started = False

    def _operation(self, kind, function, **details):
        if self.state["uncertain"]:
            raise RuntimeError("Codec state uncertain; retain resources")
        record = dict(kind=kind, pending=True, **details)
        self.state["operations"].append(record)
        self.state["uncertain"] = True
        try:
            self.persist(self.state)
            value = function()
            record.update(pending=False, value=value)
            self.state["uncertain"] = False
            self.persist(self.state)
            return value
        except Exception as exc:
            self.state["uncertain"] = True
            record["error"] = str(exc)
            self.state["recovery"] = "Retain resources; no further codec accesses"
            try:
                self.persist(self.state)
            except Exception:
                pass
            raise

    def _gate(self, name, validator):
        def validate():
            value = validator()
            if value is False:
                raise RuntimeError("Caller prerequisite gate failed")
            return value
        return self._operation(name, validate)

    def _fail(self, message):
        self.state["uncertain"] = True
        self.state["recovery"] = "Retain resources; no further codec accesses"
        self.persist(self.state)
        raise RuntimeError(message)

    def _read(self, address, width):
        def read():
            value = self.read(address, width)
            if type(value) is not int or not 0 <= value < 1 << width:
                raise ValueError("Malformed codec register value")
            return value
        return self._operation("read", read, address=address, width=width)

    def _bits(self, name, mask, value, *, address=None, width=None):
        if address is None:
            address, cls = CS42L84Regs.lookup_name(name)
            width = cls.__WIDTH__
        if value & ~mask or mask >= 1 << width:
            raise ValueError("Codec update exceeds its field")
        key = (address, width)
        old = self._read(address, width)
        if key in self.expected and old != self.expected[key]:
            self._fail("Codec changed before update")
        self.saved.setdefault(key, old)
        desired = (old & ~mask) | value
        self._operation("write", lambda: self.write(address, width, desired),
                        address=address, width=width, written=desired)
        self.expected[key] = desired
        if self._read(address, width) != desired:
            self._fail("Codec input readback mismatch")

    def _fields(self, name, **values):
        _, cls = CS42L84Regs.lookup_name(name)
        mask = 0
        for field in values:
            spec = getattr(cls, field)
            high, low = (spec, spec) if isinstance(spec, int) else spec[:2]
            mask |= ((1 << (high - low + 1)) - 1) << low
        self._bits(name, mask, int(cls(**values)))

    def _sleep(self, seconds):
        self._operation("delay", lambda: self.sleep(seconds), seconds=seconds)

    def _detect(self):
        # Upstream MIC_DET_CTRL1 threshold field is not yet in the register map.
        self._bits("MIC_DET_CTRL1", 0x3f, 0x2c, address=0x1475, width=8)
        self._fields("HS_SWITCH_CTRL", REF_HS3=1, REF_HS4=1,
                     HSB_FILT_HS3=1, HSB_FILT_HS4=1, GNDHS_HS3=1, GNDHS_HS4=1,
                     HSB_HS3=0, HSB_HS4=0)
        self._fields("HS_DET_CTRL2", SET=2, CTRL=0)
        self._fields("HS_CLAMP_DISABLE", HS_CLAMP_DISABLE=1)
        self._fields("MISC_DET_CTRL", HSBIAS_CTRL=3, DETECT_MODE=0)
        self._fields("MISC_DET_CTRL", PDN_MIC_LVL_DET=0)
        self._sleep(.05)
        self._fields("HS_SWITCH_CTRL", REF_HS3=1, REF_HS4=0,
                     HSB_FILT_HS3=1, HSB_FILT_HS4=0, GNDHS_HS3=1, GNDHS_HS4=0,
                     HSB_HS3=0, HSB_HS4=1)
        self._fields("HS_DET_CTRL2", SET=0)
        self._fields("MISC_DET_CTRL", DETECT_MODE=3)
        self._sleep(.05)
        status = self._read(0x147d, 8)
        if status & 3 != 2:
            self._fail("Headset microphone was not detected")
        self.state["detected_status2"] = status
        self._fields("MISC_DET_CTRL", PDN_MIC_LVL_DET=1)

    def prepare(self, require_clock):
        """Configure input with TX1 off; caller must verify the matching receiver profile."""
        if self._started or self.state["uncertain"]:
            raise RuntimeError("Codec preparation is one-shot")
        self._started = True
        try:
            identity = [self._read(address, 8) for address in range(3)]
            if (identity[0] << 12) | (identity[1] << 4) | (identity[2] >> 4) != 0x42a84:
                self._fail("Unexpected headset codec identity")
            initial = {address: self._read(address, 8)
                       for address in (0x1801, 0x5020, 0x5024, 0x800, 0x602)}
            if (initial[0x1801] != 0 or initial[0x5020] != 0 or initial[0x5024] != 0
                    or initial[0x800] & 1 or initial[0x602] & 1):
                self._fail("Fresh disabled input/output and BCLK reference required")
            self.expected.update({(address, 8): value for address, value in initial.items()})
            self.saved[(0x5024, 8)] = 0
            latch = self._read(0x1477, 8)
            self.state["mic_latch_before"] = latch
            self.expected[(0x1477, 8)] = latch
            self._fields("MIC_DET_CTRL4", LATCH_TO_VP=1)
            self._detect()
            self._fields("ASP_CTRL", TDM_MODE=1)
            self._fields("ASP_TX1_CTRL", EDGE=1, SLOT_START=0, WIDTH=31)
            self._fields("BUS_ASP_TX_SRC", CH1=E_BUS_SOURCE.ADC, CH2=E_BUS_SOURCE.EMPTY)
            self._fields("PLL_CTRL", EN=0)
            self._fields("ASP_FSYNC_CTRL23", BCLK_PERIOD=64)
            self._fields("CCM_CTRL3", REFCLK_DIV=0)
            self._bits("PLL_DIV_INT", 0xff, 0x40)
            for address in range(0x804, 0x807):
                self._bits("PLL_DIV_FRAC", 0xff, 0, address=address, width=8)
            self._fields("PLL_CTRL", MODE=3)
            self._bits("PLL_DIVOUT", 0xff, 0x10)
            self._fields("CCM_SAMP_RATE", RATE=4)
            if self._read(0x1801, 8) != self.expected[(0x1801, 8)]:
                self._fail("Input/output state changed before freeze")
            if self._read(6, 8) != 0:
                self._fail("Fresh unfrozen codec required")
            self._bits("FREEZE", 0xff, 1)
            self._bits("ADC_CTRL1", 0xff, 0x78)
            self._bits("ADC_CTRL4", 0xff, 0x38)
            self._bits("FREEZE", 0xff, 0)
            self._fields("MSM_BLOCK_EN2", ADC_EN=1, BUS_EN=1, ASP_EN=1)
            self._fields("ASP_CTRL", BCLK_EN=1)
            self._gate("external_clock_gate", require_clock)
            self._fields("CCM_CTRL4", REFCLK_EN=1)
            self._fields("PLL_CTRL", EN=1)
            deadline = self.clock() + .00125
            while True:
                status = self._read(0x40e, 8)
                if status & 0x20:
                    self._fail("Codec PLL error")
                if status & 0x10:
                    break
                if self.clock() >= deadline:
                    self._fail("Codec PLL lock timed out")
                self._sleep(.00025)
            self._fields("CCM_CTRL1", MCLK_SRC=E_MCLK_SRC.PLL,
                         MCLK_FREQ=E_MCLK_FREQ.F_12_288KHZ)
            self._sleep(.00015)
            self.state["prepared"] = True
            self.persist(self.state)
        except Exception:
            self.state["uncertain"] = True
            raise

    def enable_tx1(self, require_receiver_started):
        """Enable TX only after the caller proves its owned receiver is running."""
        if not self.state.get("prepared") or self.state["tx_enabled"] or self.state.get("restoring"):
            raise RuntimeError("Prepared codec with disabled TX1 required")
        self._gate("receiver_started_gate", require_receiver_started)
        if self._read(0x1801, 8) != self.expected[(0x1801, 8)] or self._read(0x5020, 8) != 0:
            self._fail("Input/output state changed before TX1 enable")
        self._bits("ASP_TX_EN", 1, 1)
        self.state["tx_enabled"] = True
        try:
            self.persist(self.state)
        except Exception:
            self.state["uncertain"] = True
            raise

    def disable_tx1(self):
        if not self.state.get("prepared") or self.state.get("restoring"):
            raise RuntimeError("Prepared codec required")
        self._bits("ASP_TX_EN", 1, 0)
        self.state["tx_enabled"] = False
        try:
            self.persist(self.state)
        except Exception:
            self.state["uncertain"] = True
            raise

    def _restore_word(self, key, original):
        address, width = key
        if self._read(address, width) != self.expected[key]:
            self._fail("Codec changed before restoration")
        if address == 0x1474:
            # Restore detection mode before bias; keep input supplies/clocks live.
            for mask in (0x18, 0x06, 0xe1):
                if (self.expected[key] ^ original) & mask:
                    self._bits("MISC_DET_CTRL", mask, original & mask)
        else:
            self._operation("write", lambda: self.write(address, width, original),
                            address=address, width=width, written=original)
        if self._read(address, width) != original:
            self._fail("Codec restoration mismatch")
        self.expected[key] = original

    def restore(self, require_receiver_stopped):
        """After TX1 and receiver stop, restore bias before clock/supply shutdown."""
        if self.state["uncertain"] or self.state.get("restoring") or not self.state.get("prepared"):
            raise RuntimeError("Retain codec after incomplete preparation")
        self.state["restoring"] = True
        self._gate("receiver_stopped_gate", require_receiver_stopped)
        if (self.state["tx_enabled"] or self.expected[(0x5024, 8)] != 0
                or self._read(0x5024, 8) != 0):
            self._fail("Fresh disabled TX1 required before restoration")
        if self._read(0x1801, 8) != self.expected[(0x1801, 8)] or self._read(0x5020, 8) != 0:
            self._fail("Input/output state changed before restoration")
        detection = {0x1475, 0x1812, 0x1811, 0x1813, 0x1474}
        for key, original in reversed(list(self.saved.items())):
            if key[0] in detection:
                self._restore_word(key, original)
        self._bits("ASP_TX_EN", 1, 0)
        self._fields("CCM_CTRL1", MCLK_SRC=E_MCLK_SRC.RCO, MCLK_FREQ=0)
        self._sleep(.00015)
        self._fields("PLL_CTRL", EN=0)
        self._fields("CCM_CTRL4", REFCLK_EN=0)
        self._fields("MSM_BLOCK_EN2", ADC_EN=0, BUS_EN=0, ASP_EN=0)
        self._fields("ASP_CTRL", BCLK_EN=0)
        for key, original in reversed(list(self.saved.items())):
            if key[0] not in detection:
                self._restore_word(key, original)
        self.state.update(restored=True, prepared=False, tx_enabled=False)
        try:
            self.persist(self.state)
        except Exception:
            self.state["uncertain"] = True
            raise
