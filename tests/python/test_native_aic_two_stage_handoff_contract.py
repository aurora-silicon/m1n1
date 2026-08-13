"""Source contracts for the J813 AIC3 local-unit to IoUnit handoff."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
EXC = (ROOT / "src" / "hv_exc.c").read_text(encoding="utf-8")


class NativeAicTwoStageHandoffContractTests(unittest.TestCase):
    def test_carrier_timer_ids_match_the_t8142_mu_gtdt(self):
        self.assertIn("#define HV_GIC_TIMER_P_INTID     17", EXC)
        self.assertIn("#define HV_GIC_TIMER_V_INTID     18", EXC)
        self.assertNotIn("#define HV_GIC_TIMER_P_INTID     30", EXC)
        self.assertNotIn("#define HV_GIC_TIMER_V_INTID     27", EXC)

    def test_local_unit_progress_is_kept_outside_the_pcpu_debugger_abi(self):
        self.assertIn("static u32 native_aic_last_progress[MAX_CPUS];", EXC)
        self.assertIn("static bool native_aic_local_unit_ready[MAX_CPUS];", EXC)
        pcpu_start = EXC.index("struct hv_pcpu_data {")
        pcpu_end = EXC.index("} ALIGNED(64);", pcpu_start)
        pcpu = EXC[pcpu_start:pcpu_end]
        self.assertNotIn("native_aic_last_progress", pcpu)
        self.assertNotIn("native_aic_local_unit_ready", pcpu)

    def test_step_35_marks_only_the_local_unit_boundary(self):
        progress = EXC.index("ctx->regs[1] == HV_TIMER_REFLECT_CALL_PROGRESS")
        ready = EXC.index(
            "ctx->regs[1] == HV_TIMER_REFLECT_CALL_CONTROLLER_READY", progress
        )
        body = EXC[progress:ready]
        self.assertIn("native_aic_last_progress[cpu] = step;", body)
        self.assertIn("step == NTASI_STEP_LOCAL_UNIT_READY", body)
        self.assertIn("native_aic_local_unit_ready[cpu] = true;", body)

    def test_first_ready_smc_keeps_the_carrier_and_later_smc_falls_through(self):
        start = EXC.index(
            "ctx->regs[1] == HV_TIMER_REFLECT_CALL_CONTROLLER_READY"
        )
        end = EXC.index("#endif", start)
        body = EXC[start:end]
        early = body.index(
            "native_aic_last_progress[cpu] == NTASI_STEP_LOCAL_UNIT_READY"
        )
        handoff = body.index("hv_native_aic_windows_controller_ready(")
        self.assertLess(early, handoff)
        self.assertIn(
            "ctx->regs[0] = HV_TIMER_REFLECT_READY_ACCEPTED;",
            body[early:handoff],
        )
        self.assertIn("return true;", body[early:handoff])

    def test_local_ready_is_the_only_igrpen1_override(self):
        self.assertGreaterEqual(
            EXC.count(
                "(!hv_vgic3_get_igrpen1() &&\n"
                "             !hv_native_aic_carrier_local_ready())"
            ),
            1,
        )
        self.assertGreaterEqual(
            EXC.count(
                "(!hv_vgic3_get_igrpen1() &&\n"
                "         !hv_native_aic_carrier_local_ready())"
            ),
            1,
        )
        helper = EXC[
            EXC.index("static bool hv_native_aic_carrier_local_ready(void)") :
            EXC.index("struct hv_native_aic_timer_diag", EXC.index(
                "static bool hv_native_aic_carrier_local_ready(void)"
            ))
        ]
        self.assertIn("native_aic_local_unit_ready[cpu]", helper)
        self.assertIn("!hv_native_aic_windows_ready()", helper)

        pending = EXC[
            EXC.index("static bool hv_carrier_irq_pending(void)") :
            EXC.index("static void hv_update_fiq(struct exc_info *ctx)")
        ]
        self.assertIn(
            "hv_native_aic_carrier_local_ready() || selected.priority < pmr",
            pending,
        )

    def test_bsp_carrier_timer_readiness_is_monotonic_until_handoff(self):
        rearm = EXC[
            EXC.index("static void hv_timer_reflect_guest_rearm(") :
            EXC.index("static u64 hv_timer_reflect_guest_repost(")
        ]
        self.assertIn(
            "PERCPU(carrier_timer_ready) |=\n"
            "            guest_cpu_ready && smp_id() == boot_cpu_idx;",
            rearm,
        )
        self.assertNotIn(
            "PERCPU(carrier_timer_ready) = guest_cpu_ready &&",
            rearm,
        )

        startup = EXC[
            EXC.index(
                "else if (hv_native_aic_windows_active() && "
                "!hv_gic_cpuif_active()) {"
            ) :
            EXC.index("} else if (hv_gic_cpuif_active()) {")
        ]
        self.assertIn("hv_native_aic_carrier_local_ready()", startup)
        self.assertIn("smp_id() == boot_cpu_idx", startup)
        self.assertIn("PERCPU(carrier_stack_ready)", startup)
        self.assertIn("PERCPU(carrier_timer_ready) = true;", startup)

    def test_carrier_iar_uses_stack_proof_not_transient_x18(self):
        start = EXC.rindex("case SYSREG_ISS(ICC_IAR1_EL1):")
        end = EXC.index("case SYSREG_ISS(ICC_IGRPEN1_EL1):", start)
        iar = EXC[start:end]
        self.assertIn("!PERCPU(carrier_stack_ready)", iar)
        self.assertIn("!PERCPU(carrier_delivery_proven)", iar)
        self.assertNotIn("regs[18] == 0", iar)

    def test_live_gic_remains_the_only_cpu_interface_owner(self):
        helper_start = EXC.index(
            "static bool hv_handle_native_aic_carrier_cpuif("
        )
        helper_end = EXC.index(
            "static bool hv_handle_t8142_gic_cpuif(", helper_start
        )
        helper = EXC[helper_start:helper_end]
        self.assertIn("case SYSREG_ISS(ICC_IAR1_EL1):", helper)
        self.assertIn("regs[rt] = hv_carrier_do_iar1();", helper)
        self.assertIn("case SYSREG_ISS(ICC_EOIR1_EL1):", helper)
        self.assertIn("hv_carrier_do_eoir1", helper)
        self.assertIn("reg == SYSREG_ISS(ICC_IGRPEN1_EL1)", helper)
        self.assertIn("gic_cpuif_brought_up[cpu]", helper)

        # Pre-rewritten absent-register sites enter this T8142 backend through
        # hv_handle_t8142_sysreg_assist(), rather than the ordinary MSR trap.
        t8142_start = EXC.index("static bool hv_handle_t8142_gic_cpuif(")
        t8142_end = EXC.index("struct hv_gic_cpuif *s", t8142_start)
        t8142_prefix = EXC[t8142_start:t8142_end]
        self.assertIn("hv_handle_native_aic_carrier_cpuif(", t8142_prefix)

        # Probes must still fall through so the image scanner recognizes and
        # rewrites absent ICC_* accesses without consuming carrier state.
        self.assertIn("if (probe || !hv_native_aic_windows_active()", helper)

        normal_start = EXC.index("static bool hv_handle_t8142_gic_cpuif(")
        normal_end = EXC.index("static bool hv_t8142_sysreg_assist_impl(")
        normal = EXC[normal_start:normal_end]
        self.assertIn("hv_vgic3_set_igrpen1(s->igrpen1);", normal)
        self.assertIn("case SYSREG_ISS(ICC_IAR1_EL1):", normal)
        self.assertIn("case SYSREG_ISS(ICC_EOIR1_EL1):", normal)
        self.assertGreaterEqual(normal.count("else if (!probe)"), 5)
        group_start = normal.index("case SYSREG_ISS(ICC_IGRPEN1_EL1):")
        group_end = normal.index("case SYSREG_ISS(ICC_SRE_EL1):", group_start)
        group = normal[group_start:group_end]
        self.assertIn("} else if (!probe) {", group)
        self.assertNotIn("if (!probe && s->igrpen1", group)

        direct_start = EXC.index("static bool hv_handle_msr_unlocked(")
        direct_end = EXC.index("switch (reg) {", direct_start)
        direct = EXC[direct_start:direct_end]
        self.assertLess(
            direct.index("hv_handle_t8142_gic_cpuif("),
            direct.index("hv_handle_native_aic_carrier_cpuif("),
        )

    def test_native_startup_prefers_the_live_gic_timer_path(self):
        self.assertIn(
            "else if (hv_native_aic_windows_active() && "
            "!hv_gic_cpuif_active()) {",
            EXC,
        )
        gic = EXC.index("} else if (hv_gic_cpuif_active()) {")
        tail = EXC[gic : EXC.index("} else if (hv_native_aic_mu_timer_active())", gic)]
        self.assertIn("hv_gic_cpuif_timer_pend(true);", tail)
        self.assertIn("hv_gic_cpuif_timer_pend(false);", tail)


if __name__ == "__main__":
    unittest.main()
