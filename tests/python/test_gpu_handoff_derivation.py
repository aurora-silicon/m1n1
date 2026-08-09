# SPDX-License-Identifier: MIT
"""AGX preboot initdata handoff: derivation, stamp mirror, and refusal.

The tests that matter here are the refusals. Once m1n1 can fill the AGX
calibration apertures, "the blobs are non-zero" stops being evidence of
anything, so every case below that a naive gate would ACCEPT is an unrecoverable
GPU firmware crash that this code has to turn into a refusal instead.
"""

import re
import struct
import unittest
import zlib
from pathlib import Path

from m1n1 import gpu_handoff


ROOT = Path(__file__).resolve().parents[2]
ABI_HEADER = ROOT / "src" / "gpu_handoff_abi.h"

#: A plausible J414s layout: 1 TiB RAM base, 16 GiB installed.
PHYS_BASE = 0x10_0000_0000 * 16
MEM_SIZE_ACTUAL = 0x4_0000_0000
GUEST_MEM_SIZE = 0x3_D000_0000


def _c_constant(name):
    text = ABI_HEADER.read_text(encoding="utf-8")
    match = re.search(rf"^#define\s+{name}\s+(0[xX][0-9a-fA-F]+|\d+)U?L?L?\s*$",
                      text, re.MULTILINE)
    if not match:
        raise AssertionError(f"{name} not found in {ABI_HEADER}")
    return int(match.group(1), 0)


def _c_struct_fields():
    """Field names of struct gpu_handoff_stamp_v1, in declaration order."""
    text = ABI_HEADER.read_text(encoding="utf-8")
    body = text.split("struct gpu_handoff_stamp_v1 {", 1)[1].split("\n}", 1)[0]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    return re.findall(r"^\s*u(?:16|32|64)\s+(\w+);", body, re.MULTILINE)


def build_reservation(**overrides):
    """One coherent, correctly stamped reservation, as m1n1 would publish it."""
    base = overrides.pop("base", 0x103F_BDC000)
    payload = {
        "hwdata_a": bytes((0x11 + i) & 0xFF for i in range(gpu_handoff.EXPECTED["hwdata_a_size"])),
        "hwdata_b": bytes((0x22 + i) & 0xFF for i in range(gpu_handoff.EXPECTED["hwdata_b_size"])),
        "globals": bytes((0x33 + i) & 0xFF for i in range(gpu_handoff.EXPECTED["globals_size"])),
    }
    fields = {name: 0 for name in gpu_handoff.STAMP_FIELDS}
    fields.update(
        signature=gpu_handoff.SIGNATURE,
        version=gpu_handoff.VERSION,
        structure_size=gpu_handoff.STAMP_SIZE,
        gpu_rev_id=4,
        num_cores=19,
        os_firmware_0=13,
        os_firmware_1=5,
        reservation_base=base,
        reservation_size=gpu_handoff.RESERVATION_SIZE,
        hwdata_a_base=base + gpu_handoff.HWDATA_A_OFFSET,
        hwdata_b_base=base + gpu_handoff.HWDATA_B_OFFSET,
        globals_base=base + gpu_handoff.GLOBALS_OFFSET,
        hwdata_a_crc32=zlib.crc32(payload["hwdata_a"]) & 0xFFFFFFFF,
        hwdata_b_crc32=zlib.crc32(payload["hwdata_b"]) & 0xFFFFFFFF,
        globals_crc32=zlib.crc32(payload["globals"]) & 0xFFFFFFFF,
    )
    fields.update(gpu_handoff.EXPECTED)
    fields.update(overrides)

    raw = bytearray(gpu_handoff.RESERVATION_SIZE)
    for role, _label, offset, map_size, key in gpu_handoff.APERTURES:
        blob = payload[key]
        raw[offset:offset + len(blob)] = blob
        fields["role"] = role
        fields["stamp_crc32"] = 0
        stamp = bytearray(
            struct.pack(
                gpu_handoff.STAMP_FORMAT,
                *[fields[name] for name in gpu_handoff.STAMP_FIELDS],
            )
        )
        struct.pack_into("<I", stamp, gpu_handoff.STAMP_SIZE - 4,
                         zlib.crc32(bytes(stamp)) & 0xFFFFFFFF)
        stamp_at = offset + gpu_handoff.stamp_offset(map_size, len(blob))
        raw[stamp_at:stamp_at + gpu_handoff.STAMP_SIZE] = stamp
    return bytes(raw)


class StampMirrorTests(unittest.TestCase):
    """The Python decoder and the C producer must be the same ABI."""

    def test_struct_format_matches_the_c_header(self):
        self.assertEqual(struct.calcsize(gpu_handoff.STAMP_FORMAT),
                         gpu_handoff.STAMP_SIZE)
        self.assertEqual(gpu_handoff.STAMP_SIZE,
                         _c_constant("GPU_HANDOFF_V1_STAMP_SIZE"))
        self.assertEqual(len(gpu_handoff.STAMP_FIELDS),
                         len(struct.unpack(gpu_handoff.STAMP_FORMAT,
                                           bytes(gpu_handoff.STAMP_SIZE))))
        self.assertEqual(list(gpu_handoff.STAMP_FIELDS), _c_struct_fields())

    def test_constants_match_the_c_header(self):
        for python_value, c_name in (
            (gpu_handoff.SIGNATURE, "GPU_HANDOFF_V1_SIGNATURE"),
            (gpu_handoff.VERSION, "GPU_HANDOFF_V1_VERSION"),
            (gpu_handoff.GENERATOR_ABI, "GPU_HANDOFF_V1_GENERATOR_ABI"),
            (gpu_handoff.RESERVATION_SIZE, "GPU_HANDOFF_V1_RESERVATION_SIZE"),
            (gpu_handoff.HWDATA_A_MAP_SIZE, "GPU_HANDOFF_V1_HWDATA_A_MAP_SIZE"),
            (gpu_handoff.HWDATA_B_MAP_SIZE, "GPU_HANDOFF_V1_HWDATA_B_MAP_SIZE"),
            (gpu_handoff.GLOBALS_MAP_SIZE, "GPU_HANDOFF_V1_GLOBALS_MAP_SIZE"),
            (gpu_handoff.TOP_MARGIN, "GPU_HANDOFF_V1_TOP_MARGIN"),
            (gpu_handoff.BUILDER_G14X_V13_5, "GPU_HANDOFF_V1_BUILDER_G14X_V13_5"),
        ):
            self.assertEqual(python_value, _c_constant(c_name), c_name)

    def test_generator_sizes_are_pinned_in_the_rust_generator(self):
        """The payload sizes this host expects are asserted at m1n1 build time.

        If those const asserts are ever removed, this host would be checking a
        length nothing else enforces.
        """
        source = (ROOT / "rust" / "src" / "gpu" / "initdata.rs").read_text(
            encoding="utf-8"
        )
        for size, type_name in (
            (gpu_handoff.EXPECTED["hwdata_a_size"], "HwDataAG14XV13_5"),
            (gpu_handoff.EXPECTED["hwdata_b_size"], "HwDataBG14XV13_5"),
            (gpu_handoff.EXPECTED["globals_size"], "GlobalsG14XV13_5"),
        ):
            self.assertIn(
                f"mem::size_of::<raw::{type_name}>() == {size:#x}", source
            )


class DerivationTests(unittest.TestCase):
    def test_derivation_clears_the_wireless_reservation_and_the_top_band(self):
        base, size = gpu_handoff.derive_reservation(
            PHYS_BASE, GUEST_MEM_SIZE, MEM_SIZE_ACTUAL
        )
        phys_top = gpu_handoff.physical_memory_top(PHYS_BASE, MEM_SIZE_ACTUAL)
        self.assertEqual(size, gpu_handoff.RESERVATION_SIZE)
        self.assertEqual(base % gpu_handoff.PAGE_SIZE, 0)
        # Entirely below the 4 MiB band that holds the firmware pmap-io-ranges
        # windows AND the wireless handoff's [phys_top-0x10000, phys_top).
        self.assertLessEqual(base + size, phys_top - gpu_handoff.TOP_MARGIN)
        self.assertGreater(base, PHYS_BASE + GUEST_MEM_SIZE)

    def test_derivation_cannot_overlap_the_wireless_reservation(self):
        from m1n1 import wireless_handoff

        gpu_base, gpu_size = gpu_handoff.derive_reservation(
            PHYS_BASE, GUEST_MEM_SIZE, MEM_SIZE_ACTUAL
        )
        wlan_base, wlan_size = wireless_handoff.derive_reservation(
            PHYS_BASE, GUEST_MEM_SIZE, MEM_SIZE_ACTUAL
        )
        self.assertFalse(
            gpu_base < wlan_base + wlan_size and wlan_base < gpu_base + gpu_size,
            f"GPU {gpu_base:#x}+{gpu_size:#x} overlaps wireless "
            f"{wlan_base:#x}+{wlan_size:#x}",
        )

    def test_disagreeing_boot_args_raise_instead_of_picking_a_winner(self):
        m1n1_view = {
            "name": "m1n1",
            "phys_base": PHYS_BASE,
            "mem_size": GUEST_MEM_SIZE,
            "mem_size_actual": MEM_SIZE_ACTUAL,
        }
        mu_view = dict(m1n1_view, name="Mu",
                       phys_base=PHYS_BASE + (1 << 32),
                       mem_size=GUEST_MEM_SIZE - (1 << 32))
        with self.assertRaises(gpu_handoff.GpuHandoffError):
            gpu_handoff.derive_agreed_reservation(m1n1_view, mu_view)

    def test_stamp_never_lands_inside_the_payload(self):
        for _role, _label, _off, map_size, key in gpu_handoff.APERTURES:
            payload = gpu_handoff.EXPECTED[f"{key}_size"]
            self.assertGreater(gpu_handoff.stamp_offset(map_size, payload), payload)
        with self.assertRaises(gpu_handoff.GpuHandoffError):
            gpu_handoff.stamp_offset(0x8000, 0x8000)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.agreed = {
            "base": 0x103F_BDC000,
            "size": gpu_handoff.RESERVATION_SIZE,
            "hwdata_a_base": 0x103F_BDC000 + gpu_handoff.HWDATA_A_OFFSET,
            "hwdata_b_base": 0x103F_BDC000 + gpu_handoff.HWDATA_B_OFFSET,
            "globals_base": 0x103F_BDC000 + gpu_handoff.GLOBALS_OFFSET,
        }

    def test_a_correct_handoff_is_accepted(self):
        stamp = gpu_handoff.verify_reservation(build_reservation(), self.agreed)
        self.assertEqual(stamp["builder_arm"], gpu_handoff.BUILDER_G14X_V13_5)
        self.assertEqual(stamp["num_cores"], 19)

    def test_wrong_variant_or_firmware_is_refused_though_non_zero(self):
        """THE test. Every case here passes a "not all zero" gate.

        Each override is applied to all three stamps at once, i.e. a fully
        self-consistent handoff that simply is not ours -- perturbing one
        aperture would be caught by the coherence check instead and would
        prove nothing about identity.
        """
        for override, why in (
            ({"builder_arm": gpu_handoff.BUILDER_G14_V13_5},
             "G14G/13.5: same firmware, different GPU variant"),
            ({"builder_arm": gpu_handoff.BUILDER_G13_V12_3}, "G13/12.3"),
            ({"firmware_compat_min": 3}, "firmware 13.3"),
            ({"firmware_compat_maj": 12, "firmware_compat_min": 4}, "firmware 12.4"),
            ({"gpu_variant": ord("C")}, "G14C"),
            ({"gpu_gen": 13}, "G13 generation"),
            ({"gpu_core": 17}, "G14C core"),
            ({"chip_id": 0x6021}, "T6021 silicon"),
            ({"generator_abi": 2}, "a newer generator ABI"),
            ({"hwdata_a_size": 0x6C30}, "a different HwDataA length"),
            ({"globals_map_size": 0x14000}, "a different globals aperture"),
        ):
            with self.subTest(why=why):
                raw = build_reservation(**override)
                with self.assertRaises(gpu_handoff.GpuHandoffError):
                    gpu_handoff.verify_reservation(raw, self.agreed)

    def test_mu_placeholder_is_refused(self):
        with self.assertRaises(gpu_handoff.GpuHandoffError):
            gpu_handoff.verify_reservation(
                bytes(gpu_handoff.RESERVATION_SIZE), self.agreed
            )

    def test_tampered_payload_is_refused(self):
        raw = bytearray(build_reservation())
        raw[gpu_handoff.GLOBALS_OFFSET + 0x100] ^= 0xFF
        with self.assertRaises(gpu_handoff.GpuHandoffError):
            gpu_handoff.verify_reservation(bytes(raw), self.agreed)

    def test_apertures_from_different_runs_are_refused(self):
        good = build_reservation()
        other = build_reservation(num_cores=18)
        raw = bytearray(good)
        raw[gpu_handoff.GLOBALS_OFFSET:] = other[gpu_handoff.GLOBALS_OFFSET:]
        with self.assertRaises(gpu_handoff.GpuHandoffError):
            gpu_handoff.verify_reservation(bytes(raw), self.agreed)

    def test_addresses_we_would_publish_must_match_what_m1n1_filled(self):
        raw = build_reservation(base=0x103F_BD8000)
        with self.assertRaises(gpu_handoff.GpuHandoffError):
            gpu_handoff.verify_reservation(raw, self.agreed)


class _FakeSgx:
    def __init__(self):
        self._properties = {}

    def getprop(self, name, default=None):
        return self._properties.get(name, default)

    def __setattr__(self, attr, value):
        if attr.startswith("_"):
            object.__setattr__(self, attr, value)
            return
        self._properties[attr.replace("_", "-").replace("--", "_")] = value


class AdtPublicationTests(unittest.TestCase):
    def setUp(self):
        self.sgx = _FakeSgx()
        self.adt = {"arm-io": {"sgx": self.sgx}}
        self.agreed = {
            "base": 0x103F_BDC000,
            "hwdata_a_base": 0x103F_BDC000,
            "hwdata_b_base": 0x103F_BE4000,
            "globals_base": 0x103F_BE8000,
        }

    def test_properties_are_the_names_and_encoding_mu_probes(self):
        written = gpu_handoff.publish_adt_regions(self.adt, self.agreed)
        self.assertEqual(
            sorted(written),
            ["gpu-globals-base", "gpu-globals-size", "hw-data-a-base",
             "hw-data-a-size", "hw-data-b-base", "hw-data-b-size"],
        )
        # NtasiGpuDtNodeU64() requires >= 8 bytes and dereferences a UINT64*.
        for name, expected in (
            ("hw-data-a-base", self.agreed["hwdata_a_base"]),
            ("hw-data-a-size", gpu_handoff.HWDATA_A_MAP_SIZE),
            ("hw-data-b-base", self.agreed["hwdata_b_base"]),
            ("hw-data-b-size", gpu_handoff.HWDATA_B_MAP_SIZE),
            ("gpu-globals-base", self.agreed["globals_base"]),
            ("gpu-globals-size", gpu_handoff.GLOBALS_MAP_SIZE),
        ):
            raw = self.sgx.getprop(name)
            self.assertEqual(len(raw), 8, name)
            self.assertEqual(struct.unpack("<Q", raw)[0], expected, name)

    def test_an_existing_live_source_is_never_overwritten(self):
        self.sgx._properties["hw-data-a-base"] = struct.pack("<Q", 1)
        with self.assertRaises(gpu_handoff.GpuHandoffError):
            gpu_handoff.publish_adt_regions(self.adt, self.agreed)


if __name__ == "__main__":
    unittest.main()
