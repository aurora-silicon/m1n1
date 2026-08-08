# SPDX-License-Identifier: MIT
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class GpuSourceContractTests(unittest.TestCase):
    def test_kboot_rejects_invalid_carveouts_and_perf_tables(self) -> None:
        source = (ROOT / "src" / "kboot_gpu.c").read_text(encoding="utf-8")

        self.assertIn('ADT_GETPROP(adt, sgx, prop, &size) < 0 || !size', source)
        self.assertIn("static int validate_aux_perf_states", source)
        self.assertIn('validate_aux_perf_states("cs-perf-states"', source)
        self.assertIn('validate_aux_perf_states("afr-perf-states"', source)
        # The perf-states-sram presence check. The table now lives in
        # `struct gpu_initdata_source` so the Linux DT path and the Windows
        # preboot handoff read it through the same code; the check itself is
        # unchanged.
        self.assertIn('chip_id != T8103 && !src->perf_states_sram', source)

    def test_both_boot_paths_share_one_reader_of_the_power_tables(self) -> None:
        """Two copies of the ADT gathering would be two chances to disagree.

        dt_set_gpu() (Linux DT) and gpu_initdata_generate() (the Windows
        preboot handoff) hand the SAME generator the SAME machine's tables. If
        they ever gathered them separately, the two could drift and produce
        initdata that differs subtly -- whose failure mode is an unrecoverable
        GPU firmware crash, not a wrong pixel. So there is exactly one reader.
        """
        source = (ROOT / "src" / "kboot_gpu.c").read_text(encoding="utf-8")

        self.assertEqual(
            source.count("static int gpu_collect_initdata_source("), 1
        )
        self.assertEqual(source.count("gpu_collect_initdata_source(&src)"), 2)
        self.assertEqual(
            source.count("rust_fill_gpu_initdata_stamped(&ins"), 1,
            "the generator must be invoked from exactly one place",
        )
        # And the handoff must refuse rather than truncate if the generated
        # payload does not fit the published aperture.
        self.assertIn("does not fit", source)

        aux_bounds = source.index("if (i >= count)")
        aux_index = source.index("ps->states[i + j * ps->count]")
        self.assertLess(aux_bounds, aux_index)

        main_bounds = source.index("if (i >= perf_state_count)")
        main_index = source.index("perf_states[i + j * perf_state_count]")
        self.assertLess(main_bounds, main_index)

    def test_handoff_uses_register_values_and_current_context(self) -> None:
        source = (ROOT / "proxyclient" / "m1n1" / "fw" / "agx" / "handoff.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("self.reg.CUR_CTX.val = 0xffffffff", source)
        self.assertIn("if self.reg.TURN.val != 0:", source)
        self.assertIn("while self.reg.TURN.val != 0:", source)
        self.assertNotIn("self.reg.UNK =", source)

    def test_uat_narrow_writes_and_dirty_range_retirement(self) -> None:
        source = (ROOT / "proxyclient" / "m1n1" / "hw" / "uat.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("self.uat.p.write8(self.translate(addr, 1), data)", source)
        self.assertIn("self.uat.p.write16(self.translate(addr, 2), data)", source)
        self.assertNotIn("daat", source)
        self.assertNotIn(".write6(", source)
        self.assertIn("self.dirty_ranges.clear()", source)


if __name__ == "__main__":
    unittest.main()
