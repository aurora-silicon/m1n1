"""Source contracts for the M5/T8142 synthetic architectural PMU."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
EXC = (ROOT / "src" / "hv_exc.c").read_text(encoding="utf-8")
HV = (ROOT / "src" / "hv.c").read_text(encoding="utf-8")
HV_H = (ROOT / "src" / "hv.h").read_text(encoding="utf-8")
REGS = (ROOT / "src" / "arm_cpu_regs.h").read_text(encoding="utf-8")
SMP = (ROOT / "src" / "smp.c").read_text(encoding="utf-8")
HV_PY = (ROOT / "proxyclient" / "m1n1" / "hv" / "__init__.py").read_text(encoding="utf-8")
HV_TYPES_PY = (ROOT / "proxyclient" / "m1n1" / "hv" / "types.py").read_text(encoding="utf-8")


class T8142PmuContractTests(unittest.TestCase):
    def test_primary_and_secondary_cores_trap_the_architectural_pmu(self) -> None:
        self.assertIn("mdcr |= MDCR_EL2_TPMCR | MDCR_EL2_TPM;", HV)
        self.assertIn("mdcr &= ~(MDCR_EL2_HPMN | MDCR_EL2_HPME);", HV)
        self.assertIn("hv_secondary_info.mdcr = mrs(MDCR_EL2);", HV)
        self.assertIn("msr(MDCR_EL2, info->mdcr);", HV)
        self.assertIn("#define MDCR_EL2_TPMCR BIT(5)", REGS)
        self.assertIn("#define MDCR_EL2_TPM   BIT(6)", REGS)

    def test_t8142_uses_a_monotonic_cycle_alias(self) -> None:
        start = EXC.index("static bool hv_handle_t8142_pmu(")
        end = EXC.index("static bool hv_handle_msr_unlocked(", start)
        handler = EXC[start:end]
        self.assertIn("SYS_PMCCNTR_EL0", handler)
        self.assertIn("SYS_IMP_APL_CNTVCT_ALIAS_EL0", handler)

    def test_t8142_handler_never_touches_removed_apple_pmc_registers(self) -> None:
        start = EXC.index("static bool hv_handle_t8142_pmu(")
        end = EXC.index("static bool hv_handle_msr_unlocked(", start)
        handler = EXC[start:end]
        for removed in (
            "SYS_IMP_APL_PMCR0",
            "SYS_IMP_APL_PMC0",
            "SYS_IMP_APL_PMCR1",
            "SYS_IMP_APL_PMSR",
        ):
            self.assertNotIn(removed, handler)

    def test_windows_preboot_pmu_writes_are_consumed_at_el2(self) -> None:
        start = EXC.index("static bool hv_handle_t8142_pmu(")
        end = EXC.index("static bool hv_handle_msr_unlocked(", start)
        handler = EXC[start:end]
        for register in (
            "SYS_PMCR_EL0",
            "SYS_PMCCFILTR_EL0",
            "SYS_PMCNTENCLR_EL0",
            "SYS_PMCNTENSET_EL0",
            "SYS_PMINTENCLR_EL1",
            "SYS_PMOVSCLR_EL0",
            "SYS_PMUSERENR_EL0",
        ):
            self.assertIn(register, handler)
        self.assertIn("if (hv_handle_t8142_pmu(reg, is_read, rt, regs))", EXC)

    def test_el1_undefined_sysregs_are_forwarded_to_the_same_el2_backend(self) -> None:
        self.assertIn("HV_SYSREG_ASSIST_CALL_MAGIC", EXC)
        self.assertIn("static bool hv_handle_t8142_sysreg_assist(", EXC)
        self.assertIn("hv_handle_t8142_pmu(reg, is_read, rt, regs)", EXC)
        smc = EXC[EXC.index("static bool hv_handle_smc(") :]
        assist = smc.index("ctx->regs[0] == HV_SYSREG_ASSIST_CALL_MAGIC")
        native_aic = smc.index("#ifdef ENABLE_NATIVE_AIC_PASSTHROUGH")
        self.assertLess(assist, native_aic)
        self.assertIn("handled forwarded EL1 sysreg instruction", smc)
        self.assertIn("case ESR_EC_HVC:", smc)
        hvc = smc[smc.index("case ESR_EC_HVC:") :]
        self.assertLess(hvc.index("handled = hv_handle_smc(ctx);"), hvc.index("case ESR_EC_IMPDEF:"))
        self.assertIn("if (ec != ESR_EC_HVC)", smc)

    def test_exception_hook_tracks_the_real_boot_cpu(self) -> None:
        self.assertIn("self.boot_cpu_id = cpu_node.cpu_id", HV_PY)
        self.assertIn("self.ctx.cpu_id != self.boot_cpu_id", HV_PY)
        self.assertNotIn("if self.ctx.cpu_id != 0:", HV_PY)

    def test_sp_repair_retries_exception_hook_after_mu_sets_vbar(self) -> None:
        self.assertIn("HV_SYSREG_ASSIST_READY", HV_H)
        self.assertIn("HV_SYSREG_ASSIST_READY", EXC)
        repair = EXC.index("repaired truncated Mu SP_EL0")
        rendezvous = EXC.index("HV_SYSREG_ASSIST_READY", repair)
        self.assertGreater(rendezvous, repair)
        self.assertIn("SYSREG_ASSIST_READY = 10", HV_TYPES_PY)
        self.assertIn("code == HV_EVENT.SYSREG_ASSIST_READY", HV_PY)
        self.assertIn(
            "START.HV, HV_EVENT.SYSREG_ASSIST_READY, self.handle_exception", HV_PY
        )
        self.assertIn("self.patch_exception_handling()", HV_PY)

    def test_t8142_cpu_release_preserves_hardware_proven_pmgr_edge(self) -> None:
        start = SMP.index("static bool smp_start_cpu(")
        end = SMP.index("\nstatic void smp_stop_cpu(", start)
        release = SMP[start:end]
        self.assertIn("cpu_start_base += die * PMGR_DIE_OFFSET;", release)
        self.assertIn(
            "write32(cpu_start_base + 0x4, cpu_start_bit(index, cluster, core));",
            release,
        )
        self.assertIn(
            "write32(cpu_start_base + 0x8 + 4 * cluster, 1 << core);", release
        )
        self.assertIn("smp_wait_cpu(index);", release)

        secondaries = SMP[SMP.index("void smp_start_secondaries") :]
        self.assertIn(
            "smp_start_cpu(i, die, cluster, core, cpu_impl_reg[0], "
            "pmgr_reg + cpu_start_off);",
            secondaries,
        )
        self.assertIn("t8142_prepared_mask |= BIT(i);", secondaries)
        self.assertIn(
            "write32(t8142_misc_cores_reg + 0x4, t8142_global_mask);",
            secondaries,
        )
        self.assertIn(
            "write32(t8142_misc_cores_reg + 0x8, t8142_cluster_masks[0]);",
            secondaries,
        )
        self.assertIn(
            "write32(t8142_misc_cores_reg + 0xc, t8142_cluster_masks[1]);",
            secondaries,
        )
        self.assertLess(
            secondaries.index("smp_start_cpu(i, die, cluster, core"),
            secondaries.index("write32(t8142_misc_cores_reg + 0x4"),
        )


if __name__ == "__main__":
    unittest.main()
