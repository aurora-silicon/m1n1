"""Host contracts for retaining Windows ARM64 register state across Apple WFI."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
CHICKENS = (ROOT / "src" / "chickens.c").read_text(encoding="utf-8")
HV = (ROOT / "src" / "hv.c").read_text(encoding="utf-8")
EXC = (ROOT / "src" / "hv_exc.c").read_text(encoding="utf-8")
ASM = (ROOT / "src" / "hv_asm.S").read_text(encoding="utf-8")


class NativeAicWfiRegisterContractTests(unittest.TestCase):
    def test_cpu_init_clears_retention_disable_bit(self):
        start = CHICKENS.index("if (cpu_features->cyc_ovrd) {")
        end = CHICKENS.index("\n    }", start)
        policy = CHICKENS[start:end]

        self.assertIn("CYC_OVRD_DISABLE_WFI_RET", policy)
        self.assertIn("CYC_OVRD_WFI_MODE(2)", policy)
        self.assertNotIn(
            "CYC_OVRD_WFI_MODE(2) | CYC_OVRD_DISABLE_WFI_RET", policy
        )

    def test_guest_entry_reasserts_retention_on_every_cpu(self):
        start = HV.index("static void hv_configure_guest_wfi_mode(void)")
        end = HV.index("\n}\n\nint hv_init", start)
        policy = HV[start:end]

        self.assertIn(
            "CYC_OVRD_WFI_MODE_MASK | CYC_OVRD_DISABLE_WFI_RET", policy
        )
        self.assertIn("CYC_OVRD_WFI_MODE(mode)", policy)
        self.assertGreaterEqual(HV.count("hv_configure_guest_wfi_mode();"), 2)

    def test_guest_writes_cannot_disable_retention_or_restore_mode_zero(self):
        start = EXC.index("case SYSREG_ISS(SYS_IMP_APL_CYC_OVRD):")
        end = EXC.index("/* IPI handling */", start)
        trap = EXC[start:end]

        native = trap[trap.index("#ifdef ENABLE_NATIVE_AIC_PASSTHROUGH"):
                      trap.index("#else")]
        self.assertIn("CYC_OVRD_DISABLE_WFI_RET", native)
        self.assertIn("CYC_OVRD_FIQ_MODE_MASK", native)
        self.assertIn("CYC_OVRD_WFI_MODE_MASK", native)
        self.assertIn("value |= CYC_OVRD_WFI_MODE(2);", trap)
        self.assertNotIn("return false;", native)
        self.assertIn("HV_NATIVE_AIC_TRACE_WFI_POLICY", native)

    def test_el2_exception_entry_restores_guest_x18_verbatim(self):
        entry = ASM[ASM.index("_hv_entry:"):ASM.index(".globl _hv_return")]
        leave = ASM[ASM.index("_hv_return:"):ASM.index(".globl _v_hv_sync")]

        self.assertIn("stp x18, x19", entry)
        self.assertIn("ldp x18, x19", leave)

    def test_startup_carrier_observes_but_never_rewrites_windows_x18(self):
        start = EXC.index("static void hv_windows_update_carrier_readiness(")
        end = EXC.index("\nenum hv_carrier_queue", start)
        readiness = EXC[start:end]

        self.assertIn("hv_native_aic_windows_active()", readiness)
        self.assertIn("hv_native_aic_windows_ready()", readiness)
        self.assertIn("ctx->regs[18] == 0", readiness)
        self.assertIn("mrs(TPIDR_EL1) & ~0xfffULL", readiness)
        self.assertIn("ctx->regs[18] != guest_pcr", readiness)
        self.assertIn("panic_stack_ready && interrupt_stack_ready", readiness)
        self.assertNotIn("ctx->regs[18] = guest_pcr", readiness)
        self.assertNotIn("HV_NATIVE_AIC_TRACE_X18", EXC)


if __name__ == "__main__":
    unittest.main()
