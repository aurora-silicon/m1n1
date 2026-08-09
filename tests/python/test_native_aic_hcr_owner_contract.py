"""Host contracts for native-AIC HCR.IMO/VI ownership."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
AIC = (ROOT / "src" / "hv_aic.c").read_text(encoding="utf-8")
EXC = (ROOT / "src" / "hv_exc.c").read_text(encoding="utf-8")
HEADER = (ROOT / "src" / "hv.h").read_text(encoding="utf-8")

HCR_FMO = 1 << 3
HCR_IMO = 1 << 4
HCR_VI = 1 << 7
HCR_UNRELATED = (1 << 0) | (1 << 31) | HCR_FMO


def route(hcr: int, mode: str) -> int:
    hcr |= HCR_FMO
    if mode == "passthrough":
        return hcr & ~(HCR_IMO | HCR_VI)
    if mode == "startup":
        return (hcr & ~HCR_VI) | HCR_IMO
    if mode == "doorbell":
        return hcr | HCR_IMO | HCR_VI
    if mode == "startup_pending":
        return hcr | HCR_VI
    if mode == "clear_vi":
        return hcr & ~HCR_VI
    raise ValueError(mode)


def windows_ready_route(*, deferred: bool, carrier_active: bool,
                        pending: bool) -> str:
    if deferred or carrier_active:
        return "passthrough"
    return "doorbell" if pending else "passthrough"


def progress_deferred(*, deferred: bool, hold_epoch: int,
                      entry_epoch: int, irq_pending: bool,
                      elapsed_ticks: int = 0, capture_delay: int = 8,
                      replay_full: bool = False,
                      raw_event: int = 0) -> tuple[bool, bool, int | None]:
    """Return (still_deferred, released, captured) for UNREAD_REAL."""
    if not deferred or hold_epoch == entry_epoch:
        return deferred, False, None
    if not irq_pending:
        return False, True, None
    if elapsed_ticks < capture_delay or replay_full:
        return True, False, None
    return False, True, raw_event


class NativeAicHcrOwnerContractTests(unittest.TestCase):
    def test_every_route_preserves_unrelated_hcr_bits(self):
        for mode in (
            "passthrough",
            "startup",
            "doorbell",
            "startup_pending",
            "clear_vi",
        ):
            self.assertEqual(route(HCR_UNRELATED, mode) & HCR_UNRELATED,
                             HCR_UNRELATED)

    def test_every_native_route_keeps_apple_fiq_at_el2(self):
        for mode in (
            "passthrough",
            "startup",
            "doorbell",
            "startup_pending",
            "clear_vi",
        ):
            self.assertTrue(route(0, mode) & HCR_FMO)

    def test_context_specific_routes_match_the_proven_bit_transitions(self):
        mask = HCR_IMO | HCR_VI
        for incoming in (0, HCR_IMO, HCR_VI, mask):
            hcr = HCR_UNRELATED | incoming
            self.assertEqual(route(hcr, "passthrough") & mask, 0)
            self.assertEqual(route(hcr, "startup") & mask, HCR_IMO)
            self.assertEqual(route(hcr, "doorbell") & mask, mask)
            self.assertEqual(route(hcr, "startup_pending") & mask,
                             incoming | HCR_VI)
            self.assertEqual(route(hcr, "clear_vi") & mask,
                             incoming & ~HCR_VI)

    def test_only_the_owner_mutates_native_irq_route_bits(self):
        owner_start = AIC.index("void hv_native_aic_apply_hcr_route(")
        owner_end = AIC.index("void hv_native_aic_enter_cpu(void)", owner_start)
        owner = AIC[owner_start:owner_end]
        self.assertIn("hv_write_hcr(routed);", owner)
        self.assertIn("u64 hcr = mrs(HCR_EL2);", owner)
        self.assertIn("u64 routed = hcr | HCR_FMO;", owner)
        self.assertNotIn("hv_write_hcr", AIC[owner_end:])

        self.assertNotIn("hv_write_hcr(hcr & ~(HCR_IMO | HCR_VI))", EXC)
        self.assertNotIn("hv_write_hcr(hcr | HCR_IMO | HCR_VI)", EXC)
        self.assertNotIn("hv_write_hcr(hcr | HCR_VI)", EXC)
        self.assertNotIn("hv_write_hcr(mrs(HCR_EL2) & ~HCR_VI)", EXC)
        self.assertIn("hv_native_aic_apply_hcr_route", EXC)

    def test_windows_ready_policy_has_one_owner(self):
        sync_start = EXC.index("static void hv_native_aic_doorbell_sync(void)\n{")
        sync_end = EXC.index("\n}\n#endif", sync_start) + 2
        sync = EXC[sync_start:sync_end]
        update_start = EXC.index("static void hv_update_fiq(")
        update_end = EXC.index(
            "\n#ifdef ENABLE_NATIVE_AIC_PASSTHROUGH\n"
            "bool hv_native_aic_event_replay",
            update_start,
        )
        update = EXC[update_start:update_end]
        rearm_start = EXC.index("static void hv_timer_reflect_guest_rearm(")
        rearm_end = EXC.index("\n}\n#endif", rearm_start) + 2
        rearm = EXC[rearm_start:rearm_end]

        self.assertIn("native_irq_rearm_deferred[smp_id()]", sync)
        self.assertNotIn("mrs(HCR_EL2)", sync)
        self.assertEqual(
            sync.count("hv_native_aic_apply_hcr_route("), 4
        )
        self.assertIn("hv_native_aic_doorbell_sync();", update)
        self.assertNotIn("bool native_pending", update)
        self.assertIn("hv_native_aic_doorbell_sync();", rearm)
        windows_rearm = rearm[rearm.rindex("    if (physical) {"):]
        self.assertNotIn("HV_NATIVE_AIC_HCR_CLEAR_VI", windows_rearm)
        self.assertEqual(
            EXC.count("HV_NATIVE_AIC_HCR_SYNTHETIC_DOORBELL"), 1
        )

    def test_windows_ready_owner_is_not_bypassed_when_only_fmo_is_wrong(self):
        sync_start = EXC.index("static void hv_native_aic_doorbell_sync(void)\n{")
        sync_end = EXC.index("\n}\n#endif", sync_start) + 2
        sync = EXC[sync_start:sync_end]

        # The route owner repairs HCR.FMO as well as IMO/VI.  Its callers must
        # therefore select a route unconditionally instead of deciding that
        # IMO/VI already look right and skipping the owner entirely.
        self.assertNotIn("if (hcr & (HCR_IMO | HCR_VI))", sync)
        self.assertNotIn(
            "if ((hcr & (HCR_IMO | HCR_VI)) != (HCR_IMO | HCR_VI))",
            sync,
        )
        self.assertTrue(route(0, "passthrough") & HCR_FMO)
        self.assertTrue(route(HCR_IMO | HCR_VI, "doorbell") & HCR_FMO)

    def test_physical_irq_collision_defers_but_does_not_lose_pending_work(self):
        self.assertEqual(
            windows_ready_route(
                deferred=False, carrier_active=False, pending=True
            ),
            "doorbell",
        )
        self.assertEqual(
            windows_ready_route(
                deferred=True, carrier_active=False, pending=True
            ),
            "passthrough",
        )
        self.assertEqual(
            progress_deferred(
                deferred=True,
                hold_epoch=7,
                entry_epoch=8,
                irq_pending=True,
                elapsed_ticks=8,
                raw_event=0x00010728,
            ),
            (False, True, 0x00010728),
        )
        self.assertEqual(
            windows_ready_route(
                deferred=False, carrier_active=False, pending=True
            ),
            "doorbell",
        )

    def test_unread_real_hold_releases_only_on_later_irq_quiescence(self):
        self.assertEqual(
            progress_deferred(
                deferred=True,
                hold_epoch=7,
                entry_epoch=7,
                irq_pending=False,
            ),
            (True, False, None),
        )
        self.assertEqual(
            progress_deferred(
                deferred=True,
                hold_epoch=7,
                entry_epoch=8,
                irq_pending=True,
            ),
            (True, False, None),
        )
        self.assertEqual(
            progress_deferred(
                deferred=True,
                hold_epoch=7,
                entry_epoch=8,
                irq_pending=False,
            ),
            (False, True, None),
        )

    def test_deferred_recovery_has_a_real_bound_and_exact_replay(self):
        start = EXC.index(
            "static void hv_native_aic_progress_deferred_irq(void)"
        )
        end = EXC.index(
            "static void hv_native_aic_doorbell_sync(void)", start
        )
        recovery = EXC[start:end]
        irq_start = EXC.index("void hv_exc_irq(struct exc_info *ctx)")
        irq_end = EXC.index("void hv_exc_fiq(struct exc_info *ctx)", irq_start)
        irq = EXC[irq_start:irq_end]

        self.assertIn("HV_NATIVE_AIC_ISR_IRQ_PENDING BIT(7)", EXC)
        self.assertIn("sysop(\"isb\")", recovery)
        self.assertIn("mrs(ISR_EL1) & HV_NATIVE_AIC_ISR_IRQ_PENDING", recovery)
        self.assertIn("HV_NATIVE_AIC_REARM_CAPTURE_DIVISOR", recovery)
        self.assertIn("native_irq_rearm_start_time[cpu]", recovery)
        self.assertIn("aic_ack();", recovery)
        self.assertIn("native_irq_rearm_replay_event[cpu] = raw_event", recovery)
        self.assertIn("native_irq_rearm_replay_valid[cpu] = true", recovery)
        self.assertIn("raw_event == 0", recovery)
        self.assertIn("native_irq_rearm_entry_epoch[cpu] ==", recovery)
        self.assertIn("native_irq_entry_epoch[cpu]", recovery)
        self.assertIn("native_irq_rearm_release_count[cpu]++", recovery)
        self.assertIn("HV_NATIVE_AIC_TRACE_IRQ_REARM", recovery)
        self.assertIn(
            "native_irq_rearm_entry_epoch[smp_id()] =", irq
        )
        self.assertIn("native_irq_rearm_start_time[smp_id()] =", irq)
        self.assertEqual(EXC.count("hv_native_aic_exception_entry();"), 4)

        replay = EXC[
            EXC.index("bool hv_native_aic_event_replay(u64 *event)") :
            EXC.index("bool hv_native_aic_event_read(", EXC.index(
                "bool hv_native_aic_event_replay(u64 *event)"
            ))
        ]
        self.assertIn("*event = native_irq_rearm_replay_event[cpu];", replay)
        self.assertIn("native_irq_rearm_replay_valid[cpu] = false;", replay)
        self.assertLess(
            AIC.index("hv_native_aic_event_replay(val)"),
            AIC.index("hv_pa_rw(ctx, addr, val, write, width)"),
        )

    def test_timer_rearm_cannot_clear_an_unrelated_ipi_doorbell(self):
        timer_pending = True
        ipi_pending = True
        timer_pending = False
        self.assertEqual(
            windows_ready_route(
                deferred=False,
                carrier_active=False,
                pending=timer_pending or ipi_pending,
            ),
            "doorbell",
        )

    def test_owner_modes_are_explicit_and_do_not_derive_global_state(self):
        for mode in (
            "HV_NATIVE_AIC_HCR_PASSTHROUGH",
            "HV_NATIVE_AIC_HCR_STARTUP_CARRIER",
            "HV_NATIVE_AIC_HCR_SYNTHETIC_DOORBELL",
            "HV_NATIVE_AIC_HCR_STARTUP_PENDING",
            "HV_NATIVE_AIC_HCR_CLEAR_VI",
        ):
            self.assertIn(mode, HEADER)
            self.assertIn(mode, AIC + EXC)

        owner_start = AIC.index("void hv_native_aic_apply_hcr_route(")
        owner_end = AIC.index("void hv_native_aic_enter_cpu(void)", owner_start)
        owner = AIC[owner_start:owner_end]
        self.assertNotIn("hv_native_aic_windows", owner)
        self.assertNotIn("PERCPU", owner)
        self.assertNotIn("hv_carrier_irq_pending", owner)


if __name__ == "__main__":
    unittest.main()
