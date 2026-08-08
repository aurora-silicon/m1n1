#!/usr/bin/env python3
"""Model the bounded native-AIC UNREAD_REAL capture/replay fallback."""

import unittest


HCR_IMO = 1 << 4
HCR_VI = 1 << 7
RESERVED = 0x000107F0


class DeferredCaptureModel:
    def __init__(self, capture_delay=8):
        self.capture_delay = capture_delay
        self.entry = 0
        self.started = 0
        self.deferred = False
        self.replay = None
        self.synthetic = []
        self.physical_reads = 0
        self.capture_reads = 0
        self.empty_captures = 0
        self.reserved_captures = 0
        self.hcr = 0

    def collision(self, now):
        self.entry += 1
        self.started = now
        self.deferred = True
        self.sync(now=now, irq_pending=True, raw_event=None)

    def post_synthetic(self, token):
        self.synthetic.append(token)
        self.route()

    def route(self):
        if self.deferred:
            self.hcr &= ~(HCR_IMO | HCR_VI)
        elif self.replay is not None or self.synthetic:
            self.hcr |= HCR_IMO | HCR_VI
        else:
            self.hcr &= ~(HCR_IMO | HCR_VI)

    def sync(self, *, now, irq_pending, raw_event=None):
        if self.deferred and self.entry != 0:
            if not irq_pending:
                self.deferred = False
            elif (
                now - self.started >= self.capture_delay
                and self.replay is None
                and raw_event is not None
            ):
                self.capture_reads += 1
                self.deferred = False
                if raw_event == 0:
                    self.empty_captures += 1
                elif raw_event == RESERVED:
                    self.reserved_captures += 1
                else:
                    self.replay = raw_event
        self.route()

    def later_entry(self, *, now, irq_pending, raw_event=None):
        self.entry += 1
        self.sync(now=now, irq_pending=irq_pending, raw_event=raw_event)

    def read_event(self, physical_raw=0):
        self.entry += 1
        if self.replay is not None:
            event = self.replay
            self.replay = None
            self.route()
            return event

        self.physical_reads += 1
        if physical_raw:
            self.deferred = False
            self.route()
            return physical_raw
        if self.synthetic:
            event = self.synthetic.pop(0)
            self.route()
            return event
        self.route()
        return 0


class DeferredCaptureTests(unittest.TestCase):
    def test_asserted_irq_cannot_suppress_ipi_forever(self):
        model = DeferredCaptureModel(capture_delay=8)
        model.post_synthetic("ipi")
        model.collision(now=100)
        self.assertEqual(model.hcr & (HCR_IMO | HCR_VI), 0)

        model.later_entry(now=107, irq_pending=True, raw_event=0x00010728)
        self.assertTrue(model.deferred)
        self.assertEqual(model.capture_reads, 0)

        model.later_entry(now=108, irq_pending=True, raw_event=0x00010728)
        self.assertFalse(model.deferred)
        self.assertEqual(model.capture_reads, 1)
        self.assertEqual(model.hcr & (HCR_IMO | HCR_VI), HCR_IMO | HCR_VI)
        self.assertEqual(model.read_event(physical_raw=0x000102A5),
                         0x00010728)
        self.assertEqual(model.physical_reads, 0)
        self.assertEqual(model.read_event(physical_raw=0x000102A5),
                         0x000102A5)
        self.assertEqual(model.read_event(), "ipi")

    def test_empty_capture_breaks_stale_isr_hold_without_inventing_token(self):
        model = DeferredCaptureModel(capture_delay=4)
        model.post_synthetic("timer")
        model.collision(now=20)
        model.later_entry(now=24, irq_pending=True, raw_event=0)
        self.assertFalse(model.deferred)
        self.assertIsNone(model.replay)
        self.assertEqual(model.empty_captures, 1)
        self.assertEqual(model.read_event(), "timer")

    def test_existing_replay_slot_prevents_another_destructive_capture(self):
        model = DeferredCaptureModel(capture_delay=1)
        model.replay = 0x0001050C
        model.collision(now=10)
        model.later_entry(now=100, irq_pending=True, raw_event=0x000102A5)
        self.assertTrue(model.deferred)
        self.assertEqual(model.capture_reads, 0)
        self.assertEqual(model.hcr & (HCR_IMO | HCR_VI), 0)

    def test_quiescence_keeps_the_zero_read_fast_path(self):
        model = DeferredCaptureModel(capture_delay=8)
        model.post_synthetic("ipi")
        model.collision(now=1)
        model.later_entry(now=2, irq_pending=False)
        self.assertFalse(model.deferred)
        self.assertEqual(model.capture_reads, 0)
        self.assertEqual(model.read_event(), "ipi")

    def test_reserved_capture_is_consumed_and_never_replayed(self):
        model = DeferredCaptureModel(capture_delay=1)
        model.post_synthetic("ipi")
        model.collision(now=50)
        model.later_entry(now=51, irq_pending=True, raw_event=RESERVED)
        self.assertEqual(model.reserved_captures, 1)
        self.assertIsNone(model.replay)
        self.assertEqual(model.read_event(), "ipi")


if __name__ == "__main__":
    unittest.main()
