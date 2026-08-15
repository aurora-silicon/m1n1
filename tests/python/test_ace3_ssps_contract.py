"""Static fail-closed contract for the ACE3 system-power-state (SSPS) bring-up.

J813's USB-C ports come up with the ACE3 PD controller's SYSTEM_POWER_STATE
(logical register 0x20) reading 0x07 -- never initialised.  In that state the
controller accepts power but refuses to source VBUS, so the port never presents
Rp and no passive USB device is ever detected.  One SSPS command with payload 0
(S0) fixes it; these tests pin the pieces that were each measured on hardware
and are each easy to silently break.
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


def _ace3_c() -> str:
    return (ROOT / "src/ace3.c").read_text(encoding="utf-8")


def _ace3_h() -> str:
    return (ROOT / "src/ace3.h").read_text(encoding="utf-8")


class Ace3SspsContract(unittest.TestCase):
    def test_usb_spmi_init_powers_the_ports_on(self):
        usb = (ROOT / "src/usb.c").read_text(encoding="utf-8")
        body = usb.split("void usb_spmi_init(void)", 1)[1].split("\n}", 1)[0]
        self.assertIn(
            "ace3_power_on_ports",
            body,
            "usb_spmi_init() must tell the PD controllers the system is on; "
            "without it the ports are sink-only and no USB device enumerates",
        )
        self.assertIn('#include "ace3.h"', usb)

    def test_ssps_payload_is_s0_and_sleep_is_three(self):
        header = _ace3_h()
        self.assertRegex(header, r"#define ACE3_SYSTEM_POWER_STATE_S0\s+0\b")
        self.assertRegex(header, r"#define ACE3_SYSTEM_POWER_STATE_SLEEP\s+3\b")
        self.assertIn(
            "ACE3_SYSTEM_POWER_STATE_S0",
            _ace3_c(),
            "the bring-up path must request S0, not sleep",
        )

    def test_four_cc_is_literal_order(self):
        """macOS builds SSPS as 0x53505353, whose LE bytes are 'S','S','P','S'.

        Byte-swapping it produces 'SPSS', which the controller answers with
        "!CMD".
        """
        self.assertIn('"SSPS"', _ace3_c())
        self.assertNotIn('"SPSS"', _ace3_c())

    def test_controller_is_woken_before_any_register_access(self):
        """The AP-side slave sleeps at boot and silently ignores writes."""
        body = _ace3_c().split("int ace3_power_on_ports", 1)[1]
        wake = body.index("ace3_wake")
        power = body.index("ace3_set_system_power_state")
        self.assertLess(
            wake, power, "SSPS sent to a sleeping ACE3 is ACKed and discarded"
        )

    def test_wake_waits_for_the_controller_to_answer(self):
        """The wake is not instant.  Sending it and reading immediately reads
        zeros, which surfaces as "register 0x20 is not implemented" -- measured
        on every cold boot, while a chainload hid it because the part was
        already awake."""
        body = _ace3_c().split("static int ace3_wake", 1)[1].split("\n}", 1)[0]
        self.assertIn("timeout_calculate", body, "the wake must wait, not race")
        self.assertIn("ace3_awake", body, "wait for a real answer, not a fixed delay")

    def test_selection_uses_the_reg0_write_command(self):
        """A cached selection can return stale data, e.g. a CMD1 that never
        appears to complete.  The reg-0 write bypasses that cache."""
        body = _ace3_c().split("static int ace3_select", 1)[1].split("\n}", 1)[0]
        self.assertIn("spmi_reg0_write", body)
        self.assertNotIn("spmi_ext_write", body)

    def test_unimplemented_registers_are_rejected_not_read_as_zero(self):
        """Size 0 means the register does not exist (e.g. SLEEP_CONF on ACE3).
        Treating that as a successful read of zeros invents data."""
        for func in ("int ace3_read", "int ace3_write"):
            body = _ace3_c().split(func, 1)[1].split("\n}", 1)[0]
            self.assertIn("if (!size)", body, f"{func} must reject a size-0 register")

    def test_ssps_result_and_readback_are_both_checked(self):
        body = _ace3_c().split("int ace3_set_system_power_state", 1)[1].split("\n}", 1)[0]
        self.assertIn("ACE3_TASK_SUCCESS", body, "DATA1 return code must be checked")
        self.assertIn("readback", body, "the new power state must be read back")

    def test_register_numbers_match_the_ti_map(self):
        header = _ace3_h()
        for name, value in (
            ("ACE3_REG_MODE", "0x03"),
            ("ACE3_REG_CMD1", "0x08"),
            ("ACE3_REG_DATA1", "0x09"),
            ("ACE3_REG_STATUS", "0x1a"),
            ("ACE3_REG_SYSTEM_POWER_STATE", "0x20"),
        ):
            self.assertRegex(header, rf"#define {name}\s+{value}\b")

    def test_task_return_codes_match_tipd(self):
        header = _ace3_h()
        self.assertRegex(header, r"#define ACE3_TASK_SUCCESS\s+0\b")
        self.assertRegex(header, r"#define ACE3_TASK_TIMEOUT\s+1\b")
        self.assertRegex(header, r"#define ACE3_TASK_REJECTED\s+3\b")

    def test_bring_up_is_best_effort(self):
        """A port that cannot be reached must degrade to today's behaviour, not
        abort USB bring-up for every other port."""
        body = _ace3_c().split("int ace3_power_on_ports", 1)[1]
        self.assertGreaterEqual(
            body.count("continue;"), 3, "per-port failures must continue the loop"
        )
        self.assertNotIn("return -1;", body.split("ADT_FOREACH_CHILD", 1)[1])

    def test_secondary_smc_slave_is_not_addressed(self):
        """Each ACE3 exposes a second slave at address+1 owned by the SMC; its
        selection and interrupt state must not be disturbed."""
        body = _ace3_c().split("int ace3_power_on_ports", 1)[1]
        self.assertNotRegex(body, r"sid\s*\+\s*1")
        self.assertIn("reg[0] & 0xf", body.replace(" ", "").replace("reg[0]&0xf", "reg[0] & 0xf"))

    def test_polls_are_bounded(self):
        """An unbounded poll against a wedged controller hangs bring-up."""
        body = _ace3_c()
        self.assertEqual(
            body.count("timeout_calculate"),
            3,
            "the selection, command and wake waits must all be bounded",
        )
        self.assertGreaterEqual(body.count("timeout_expired"), 3)

    def test_object_is_built(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        objects = makefile.split("OBJECTS := ", 1)[1].split("\n\nFP_OBJECTS", 1)[0]
        self.assertIn("ace3.o", objects, "ace3.o missing from OBJECTS")


if __name__ == "__main__":
    unittest.main()
