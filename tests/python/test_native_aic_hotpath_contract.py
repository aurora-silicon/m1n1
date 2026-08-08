"""Static latency contracts for native-AIC exception paths."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
HV = (ROOT / "src" / "hv_exc.c").read_text(encoding="utf-8")
HV_CORE = (ROOT / "src" / "hv.c").read_text(encoding="utf-8")
HEADER = (ROOT / "src" / "hv.h").read_text(encoding="utf-8")


def source_slice(start: str, end: str) -> str:
    begin = HV.index(start)
    return HV[begin : HV.index(end, begin)]


class NativeAicHotpathContractTests(unittest.TestCase):
    def test_carrier_exception_paths_never_write_uart(self):
        hot_paths = (
            source_slice(
                "void hv_carrier_retire_active_sgis(void)",
                "static bool hv_guest_ipi_doorbell_pending(void)",
            ),
            source_slice(
                "static void hv_carrier_migrate_sgis_to_native(void)",
                "static bool hv_carrier_irq_pending(void)",
            ),
            source_slice(
                "static void hv_update_fiq(struct exc_info *ctx)",
                "bool hv_native_aic_event_read(u64 raw_event, u64 *event)",
            ),
            source_slice(
                "bool hv_native_aic_event_read(u64 raw_event, u64 *event)",
                "static void hv_timer_reflect_guest_rearm(",
            ),
            source_slice(
                "case SYSREG_ISS(ICC_IAR1_EL1):",
                "#ifdef ENABLE_VGIC_MODULE",
            ),
            source_slice(
                "void hv_exc_irq(struct exc_info *ctx)",
                "void hv_exc_fiq(struct exc_info *ctx)",
            ),
        )
        for body in hot_paths:
            self.assertNotIn("printf(", body)

    def test_carrier_evidence_uses_the_bounded_binary_trace(self):
        for code in (
            "HV_NATIVE_AIC_TRACE_CARRIER_VI",
            "HV_NATIVE_AIC_TRACE_CARRIER_IAR",
            "HV_NATIVE_AIC_TRACE_CARRIER_EOI",
            "HV_NATIVE_AIC_TRACE_CARRIER_SYSREG",
            "HV_NATIVE_AIC_TRACE_CARRIER_DEFER",
            "HV_NATIVE_AIC_TRACE_CARRIER_MIGRATE",
            "HV_NATIVE_AIC_TRACE_CARRIER_RETIRE",
        ):
            self.assertIn(code, HEADER)
            self.assertIn(code, HV)

        self.assertIn("if (count <= 8)", HV)
        self.assertIn("!PERCPU(carrier_stack_defer_logged)", HV)

    def test_hcr_writes_do_not_emit_a_record_per_route_change(self):
        start = HV_CORE.index("void hv_write_hcr(u64 val)")
        end = HV_CORE.index("\nu64 hv_get_spsr(void)", start)
        body = HV_CORE[start:end]

        self.assertNotIn("hv_native_aic_trace_record", body)

    def test_production_compiles_routine_interrupt_traces_out(self):
        self.assertIn("#ifdef ENABLE_NATIVE_AIC_HOT_TRACE", HEADER)
        self.assertIn("#define HV_NATIVE_AIC_HOT_TRACE", HEADER)
        for code in (
            "HV_NATIVE_AIC_TRACE_TIMER_FIQ",
            "HV_NATIVE_AIC_TRACE_TIMER_REARM",
            "HV_NATIVE_AIC_TRACE_EVENT_REAL",
            "HV_NATIVE_AIC_TRACE_EVENT",
            "HV_NATIVE_AIC_TRACE_IPI_SEND",
            "HV_NATIVE_AIC_TRACE_IPI_FIQ",
            "HV_NATIVE_AIC_TRACE_IPI_BEGIN",
            "HV_NATIVE_AIC_TRACE_IPI_COMMIT",
            "HV_NATIVE_AIC_TRACE_IRQ_REARM",
        ):
            self.assertIn(f"HV_NATIVE_AIC_HOT_TRACE({code}", HV)

        self.assertIn(
            "hv_native_aic_trace_record(HV_NATIVE_AIC_TRACE_EVENT_RESERVED",
            HV,
        )
        self.assertIn(
            "hv_native_aic_trace_record(HV_NATIVE_AIC_TRACE_TIMER_REPOST",
            HV,
        )

    def test_timer_latency_evidence_covers_each_bridge_boundary(self):
        self.assertIn("struct hv_native_aic_timer_diag", HV)
        self.assertIn("hv_timer_diag_note_fiq(true);", HV)
        self.assertIn("hv_timer_diag_note_fiq(false);", HV)
        self.assertIn("hv_timer_diag_note_event(true);", HV)
        self.assertIn("hv_timer_diag_note_event(false);", HV)
        self.assertIn("hv_timer_diag_note_rearm(physical);", HV)
        self.assertIn("hv_timer_diag_note_ipi_bypass();", HV)

    def test_priority_blocked_timer_can_repost_only_a_live_generation(self):
        self.assertIn("hv_timer_reflect_guest_repost", HV)
        self.assertIn("!hv_native_aic_windows_ready() || !pending", HV)
        self.assertIn("HV_TIMER_REFLECT_REPOST_ALREADY_UNREAD", HV)
        self.assertIn("PERCPU(timer_v_event_unread) = true;", HV)
        self.assertIn("HV_TIMER_REFLECT_CALL_REPOST_V", HV)


if __name__ == "__main__":
    unittest.main()
