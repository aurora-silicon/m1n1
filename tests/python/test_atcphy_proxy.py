import unittest

from m1n1.atcphy import (
    ATCPHY,
    ATCPHYMode,
    ATCPHYPipePolicy,
    P_ATCPHY_APPLY_MODE,
    P_ATCPHY_ARM_GUEST_MODE,
)


class FakeProxy:
    def __init__(self):
        self.calls = []
        self.registers = {}

    def request(self, opcode, *args, **kwargs):
        self.calls.append((opcode, args, kwargs))
        return 0

    def read32(self, address):
        return self.registers.get(address, 0)


def phy_with_proxy(proxy):
    phy = object.__new__(ATCPHY)
    phy.p = proxy
    phy.port = 2
    phy.usb2phy = 0x100000
    phy.core = 0x200000
    phy.pipehandler = 0x300000
    phy.dwc3 = 0x400000
    return phy


class AtcPhyProxyTests(unittest.TestCase):
    def test_deferred_policy_is_forwarded_without_boolean_collapse(self):
        proxy = FakeProxy()
        phy = phy_with_proxy(proxy)

        phy.apply_mode(
            ATCPHYMode.USB3,
            flipped=True,
            pipe_policy=ATCPHYPipePolicy.DEFER,
        )

        opcode, args, kwargs = proxy.calls[-1]
        self.assertEqual(opcode, P_ATCPHY_APPLY_MODE)
        self.assertEqual(args[:4], (2, int(ATCPHYMode.USB3), 1, 2))
        self.assertTrue(kwargs["signed"])

    def test_old_allow_pipe_switch_keyword_remains_abi_compatible(self):
        proxy = FakeProxy()
        phy = phy_with_proxy(proxy)

        phy.apply_mode(ATCPHYMode.USB3, allow_pipe_switch=True)

        self.assertEqual(proxy.calls[-1][1][3], int(ATCPHYPipePolicy.SWITCH))

    def test_arm_carries_deferred_policy_in_arg4(self):
        proxy = FakeProxy()
        phy = phy_with_proxy(proxy)

        phy.arm_guest_mode(
            ATCPHYMode.USB3,
            flipped=True,
            pipe_policy=ATCPHYPipePolicy.DEFER,
        )

        opcode, args, kwargs = proxy.calls[-1]
        self.assertEqual(opcode, P_ATCPHY_ARM_GUEST_MODE)
        self.assertEqual(args, (2, int(ATCPHYMode.USB3), 1, 1, 2))
        self.assertTrue(kwargs["signed"])

    def test_state_includes_dwc3_phy_controls_and_pipe_mux(self):
        proxy = FakeProxy()
        phy = phy_with_proxy(proxy)
        proxy.registers[phy.pipehandler + 0x0C] = 0x08
        proxy.registers[phy.dwc3 + 0xC200] = 1 << 6
        proxy.registers[phy.dwc3 + 0xC2C0] = 1 << 17

        state = phy.state()

        self.assertEqual(state["pipe_mux_ctrl"][1], {"clk": "USB3", "data": "USB3"})
        self.assertEqual(state["dwc3_gusb2phycfg0"][1], ["SUSPHY"])
        self.assertEqual(state["dwc3_gusb3pipectl0"][1], ["SUSPHY"])


if __name__ == "__main__":
    unittest.main()
