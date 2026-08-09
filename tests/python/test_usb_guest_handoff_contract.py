"""Static fail-closed contract for the pre-guest USB/ATCPHY handoff."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]


class UsbGuestHandoffContract(unittest.TestCase):
    def test_handoff_failure_propagates_through_hv_init_and_proxy(self):
        usb = (ROOT / "src/usb.c").read_text(encoding="utf-8")
        hv = (ROOT / "src/hv.c").read_text(encoding="utf-8")
        proxy = (ROOT / "src/proxy.c").read_text(encoding="utf-8")
        client = (ROOT / "proxyclient/m1n1/proxy.py").read_text(encoding="utf-8")
        runner = (ROOT / "proxyclient/m1n1/hv/__init__.py").read_text(encoding="utf-8")

        self.assertRegex(
            usb,
            r"if \(usb_phy_handoff_host\(i\) < 0\) \{[\s\S]*?return -1;",
            "an ambiguous PHY state must abort controller release",
        )
        failure = hv.index("if (usb_iodev_shutdown_except(uartproxy_iodev) < 0)")
        self.assertLess(failure, hv.index("pcie_shutdown();"))
        self.assertIn("reply->retval = (u64)(s64)hv_init();", proxy)
        self.assertRegex(
            client,
            r"def hv_init\(self\):\s+return self\.request\(self\.P_HV_INIT, signed=True\)",
        )
        self.assertRegex(runner, r"if self\.p\.hv_init\(\) != 0:\s+raise RuntimeError")

    def test_handoff_error_is_not_log_only(self):
        usb = (ROOT / "src/usb.c").read_text(encoding="utf-8")
        body = usb.split("int usb_iodev_shutdown_except", 1)[1].split(
            "void usb_iodev_vuart_setup", 1
        )[0]
        self.assertNotRegex(
            body,
            r"if \(usb_phy_handoff_host\(i\) < 0\)\s+printf\(",
        )

    def test_armed_phy_reapply_failure_aborts_handoff(self):
        usb = (ROOT / "src/usb.c").read_text(encoding="utf-8")
        body = usb.split("static int usb_phy_handoff_host", 1)[1].split(
            "dwc3_dev_t *usb_iodev_bringup", 1
        )[0]
        self.assertRegex(
            body,
            r"if \(atcphy_reapply_guest_mode\(idx\) < 0\) \{[\s\S]*?return -1;",
            "an undefined SuperSpeed backend must prevent guest entry",
        )

    def test_dwc3_stays_reset_and_clamped_for_mu_dart_transition(self):
        usb = (ROOT / "src/usb.c").read_text(encoding="utf-8")
        body = usb.split("static int usb_phy_handoff_host", 1)[1].split(
            "dwc3_dev_t *usb_iodev_bringup", 1
        )[0]
        reapply = body.index("atcphy_reapply_guest_mode(idx)")
        held_check = body.index("DWC3 did not remain reset+clamped", reapply)
        self.assertGreater(held_check, reapply)
        self.assertIn("PIPEHANDLER_AON_GEN_DWC3_FORCE_CLAMP_EN", body[reapply:])
        self.assertIn("PIPEHANDLER_AON_GEN_DWC3_RESET_N", body[reapply:])
        self.assertNotRegex(
            body[reapply:held_check],
            r"clear32\([^;]*PIPEHANDLER_AON_GEN_DWC3_FORCE_CLAMP_EN",
        )
        self.assertNotRegex(
            body[reapply:held_check],
            r"set32\([^;]*PIPEHANDLER_AON_GEN_DWC3_RESET_N",
        )

    def test_usb2_phy_is_off_in_host_mode_at_the_mu_boundary(self):
        usb = (ROOT / "src/usb.c").read_text(encoding="utf-8")
        body = usb.split("static int usb_phy_handoff_host", 1)[1].split(
            "dwc3_dev_t *usb_iodev_bringup", 1
        )[0]
        reapply = body.index("atcphy_reapply_guest_mode(idx)")
        final_isolation = body.index(
            "write32(regs.atc + USB2PHY_USBCTL, USB2PHY_USBCTL_ISOLATION)",
            reapply,
        )
        final_host = body.index("set32(regs.atc + USB2PHY_SIG, USB2PHY_SIG_HOST)", reapply)
        held_check = body.index("DWC3 did not remain reset+clamped", reapply)
        self.assertLess(reapply, final_isolation)
        self.assertLess(final_isolation, final_host)
        self.assertLess(final_host, held_check)
        self.assertNotIn("USB2PHY_USBCTL_RUN", body[final_isolation:held_check])


if __name__ == "__main__":
    unittest.main()
