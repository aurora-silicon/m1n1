# SPDX-License-Identifier: MIT
"""AGX preboot initdata handoff (ABI v1): derivation, stamp, ADT publication.

This module is the single host-side source of truth for the physical address
m1n1 fills and Mu reads, and for the identity stamp that makes the difference
between "the blobs are non-zero" and "the blobs are the ones this driver was
built for".

Why the driver's existing gate is not enough
--------------------------------------------
``ntasi_agx_initdata_blob_has_data()`` in AppleAgxGpu answers "is this all
zero?". That separates Mu's zero-filled placeholder from *something*. It does
not separate the G14X/13.5 initdata graph the KMD was compiled against from the
G13/12.3, G14/12.4, G13/13.5 or G14G/13.5 graphs the same generator will
happily emit on other inputs -- and all four of those are non-zero. The
documented outcome of handing AGX firmware the wrong graph is an unrecoverable
crash needing a full system reboot.

So m1n1 stamps each aperture with the generator arm that ran, the silicon it
ran on, the firmware ABI it targeted, and a CRC32 of the exact payload bytes;
the driver refuses anything that is not a bit-exact match for its own compiled
constants. :func:`parse_stamp` and :func:`verify_reservation` below are the
host's copy of that judgement, run BEFORE the guest is started so a mismatch
costs a refusal rather than a boot.

Why a derivation and not a message
----------------------------------
m1n1 fills these pages at EL2, before Mu exists. Unlike the wireless handoff,
Mu does not re-derive the address: ``NtasiResolveAndReserveGpuCarveouts()``
reads ``hw-data-a-base``/``-size`` and friends straight out of the ``/arm-io/sgx``
ADT node. The host owns that ADT (``HV.start()`` rebuilds and uploads
``hv.adt``), so the agreement that matters is between m1n1's device-side
validator and this file. Both compute::

    phys_top = ALIGN_DOWN(boot_args.phys_base, 4 GiB) + boot_args.mem_size_actual
    base     = ALIGN_DOWN(phys_top - TOP_MARGIN - RESERVATION_SIZE, 16 KiB)

m1n1's copy is ``gpu_canonical_reservation_base()`` in ``src/gpu_handoff.c``,
which refuses any other base outright, so a disagreement can only ever produce
a loud refusal -- never blobs published where Mu will not look.

``TOP_MARGIN`` is 4 MiB. It keeps the reservation clear of the top-of-DRAM band
that holds the wireless handoff carveout AND the eight ``/defaults
pmap-io-ranges`` firmware windows measured on this machine (the highest ending
at ``0x103fffbc000``). That is margin, not proof: m1n1 independently refuses a
reservation overlapping an MCC TrustZone carveout or any pmap-io-ranges window,
and fails closed when it cannot read either list.
"""

from __future__ import annotations

import struct
import zlib

#: Aperture geometry. These are the sizes Mu publishes in NTAS0023's _CRS
#: resources 5/6/7 and the sizes AppleAgxGpu validates exactly. They are NOT
#: the payload sizes -- those come from the generator and land in the stamp.
HWDATA_A_OFFSET = 0x00000
HWDATA_A_MAP_SIZE = 0x08000
HWDATA_B_OFFSET = 0x08000
HWDATA_B_MAP_SIZE = 0x04000
GLOBALS_OFFSET = 0x0C000
GLOBALS_MAP_SIZE = 0x18000
RESERVATION_SIZE = 0x24000

PAGE_SIZE = 0x4000
GUARD_SIZE = 0x4000
FOUR_GIB = 1 << 32
MAX_MEM_SIZE_ACTUAL = 1 << 40
TOP_MARGIN = 0x400000

STAMP_SIZE = 160
#: struct gpu_handoff_stamp_v1, src/gpu_handoff_abi.h. Packed, little endian.
STAMP_FORMAT = "<IHH" + "I" * 22 + "Q" * 5 + "I" * 6
SIGNATURE = 0x31474741  # "AGG1"
VERSION = 1
GENERATOR_ABI = 1

ROLE_HWDATA_A = 0
ROLE_HWDATA_B = 1
ROLE_GLOBALS = 2

BUILDER_NONE = 0
BUILDER_G13_V12_3 = 1
BUILDER_G14_V12_4 = 2
BUILDER_G13_V13_5 = 3
BUILDER_G14_V13_5 = 4
BUILDER_G14X_V13_5 = 5

BUILDER_NAMES = {
    BUILDER_NONE: "none",
    BUILDER_G13_V12_3: "G13 firmware 12.3",
    BUILDER_G14_V12_4: "G14G firmware 12.4",
    BUILDER_G13_V13_5: "G13 firmware 13.5",
    BUILDER_G14_V13_5: "G14G firmware 13.5",
    BUILDER_G14X_V13_5: "G14X firmware 13.5",
}

#: What AppleAgxGpu 0.8.0.0 was compiled for. Anything else is refused by the
#: driver, so it is refused here too -- before the guest starts.
EXPECTED = {
    "generator_abi": GENERATOR_ABI,
    "builder_arm": BUILDER_G14X_V13_5,
    "chip_id": 0x6020,
    "gpu_gen": 14,
    "gpu_variant": ord("S"),
    "gpu_core": 16,  # GpuCore::G14S
    "firmware_compat_maj": 13,
    "firmware_compat_min": 5,
    "hwdata_a_size": 0x6C34,
    "hwdata_b_size": 0x1884,
    "globals_size": 0x1715C,
    "hwdata_a_map_size": HWDATA_A_MAP_SIZE,
    "hwdata_b_map_size": HWDATA_B_MAP_SIZE,
    "globals_map_size": GLOBALS_MAP_SIZE,
}

#: ADT properties Mu's NtasiReserveGpuAdtCarveout() probes, as bare native
#: UINT64 scalars on /arm-io/sgx.
ADT_PROPERTIES = (
    ("hw-data-a-base", "hw-data-a-size"),
    ("hw-data-b-base", "hw-data-b-size"),
    ("gpu-globals-base", "gpu-globals-size"),
)

APERTURES = (
    (ROLE_HWDATA_A, "hw_data_a", HWDATA_A_OFFSET, HWDATA_A_MAP_SIZE, "hwdata_a"),
    (ROLE_HWDATA_B, "hw_data_b", HWDATA_B_OFFSET, HWDATA_B_MAP_SIZE, "hwdata_b"),
    (ROLE_GLOBALS, "globals", GLOBALS_OFFSET, GLOBALS_MAP_SIZE, "globals"),
)

STAMP_FIELDS = (
    "signature",
    "version",
    "structure_size",
    "generator_abi",
    "role",
    "chip_id",
    "gpu_gen",
    "gpu_variant",
    "gpu_core",
    "gpu_rev_id",
    "num_cores",
    "firmware_compat_maj",
    "firmware_compat_min",
    "builder_arm",
    "os_firmware_0",
    "os_firmware_1",
    "os_firmware_2",
    "hwdata_a_size",
    "hwdata_b_size",
    "globals_size",
    "hwdata_a_map_size",
    "hwdata_b_map_size",
    "globals_map_size",
    "reserved0",
    "reserved1",
    "reservation_base",
    "reservation_size",
    "hwdata_a_base",
    "hwdata_b_base",
    "globals_base",
    "hwdata_a_crc32",
    "hwdata_b_crc32",
    "globals_crc32",
    "reserved2",
    "reserved3",
    "stamp_crc32",
)

__all__ = [
    "GpuHandoffError",
    "RESERVATION_SIZE",
    "STAMP_SIZE",
    "EXPECTED",
    "ADT_PROPERTIES",
    "ram_base",
    "physical_memory_top",
    "boot_args_view",
    "derive_reservation",
    "derive_agreed_reservation",
    "stamp_offset",
    "parse_stamp",
    "verify_reservation",
    "publish_adt_regions",
]


class GpuHandoffError(Exception):
    """The reservation cannot be derived, or what m1n1 published is not usable."""


def ram_base(phys_base):
    """ALIGN_DOWN(phys_base, 4 GiB) -- m1n1's ram_base."""
    return phys_base & ~(FOUR_GIB - 1)


def physical_memory_top(phys_base, mem_size_actual):
    """Identical to m1n1's ``gpu_physical_memory_top()``."""
    return ram_base(phys_base) + mem_size_actual


def boot_args_view(ba, name):
    """Extract the three fields the derivation depends on from a BootArgs."""
    try:
        return {
            "name": name,
            "phys_base": int(ba.phys_base),
            "mem_size": int(ba.mem_size),
            "mem_size_actual": int(ba.mem_size_actual),
        }
    except AttributeError as exc:
        raise GpuHandoffError(
            f"{name} boot_args is missing a field the GPU initdata handoff "
            f"derivation needs: {exc}"
        ) from exc


def derive_reservation(phys_base, mem_size, mem_size_actual, name="boot_args"):
    """Derive the reservation exactly the way ``src/gpu_handoff.c`` does."""
    if mem_size_actual == 0 or mem_size_actual > MAX_MEM_SIZE_ACTUAL:
        raise GpuHandoffError(f"{name}: mem_size_actual unusable ({mem_size_actual:#x})")

    phys_top = physical_memory_top(phys_base, mem_size_actual)
    span = TOP_MARGIN + RESERVATION_SIZE
    if phys_top <= span:
        raise GpuHandoffError(f"{name}: computed PhysTop {phys_top:#x} is too small")

    base = (phys_top - span) & ~(PAGE_SIZE - 1)

    guest_top_with_margin = phys_base + mem_size + GUARD_SIZE
    if base < guest_top_with_margin:
        raise GpuHandoffError(
            f"{name}: derived base {base:#x} is below guest_top+16KiB "
            f"{guest_top_with_margin:#x}"
        )
    if base & (PAGE_SIZE - 1):
        raise GpuHandoffError(f"{name}: derived base {base:#x} is not 16 KiB aligned")
    if base + RESERVATION_SIZE > phys_top:
        raise GpuHandoffError(
            f"{name}: derived reservation {base:#x}+{RESERVATION_SIZE:#x} "
            f"exceeds PhysTop {phys_top:#x}"
        )

    return base, RESERVATION_SIZE


def derive_agreed_reservation(m1n1_view, mu_view):
    """Derive from both boot_args structs and require bit-for-bit agreement.

    m1n1's validator recomputes from its own ``cur_boot_args``; the guest ADT
    this host writes is read by Mu, which was handed the hypervisor's synthesised
    ``boot_args``. The two only agree because ``ALIGN_DOWN(x, 4 GiB)`` collapses
    both ``phys_base`` values onto the same ``ram_base`` -- a property of the
    current layout, not an invariant. Raise rather than choose a winner.
    """
    m1n1_base, size = derive_reservation(
        m1n1_view["phys_base"],
        m1n1_view["mem_size"],
        m1n1_view["mem_size_actual"],
        m1n1_view.get("name", "m1n1"),
    )
    mu_base, mu_size = derive_reservation(
        mu_view["phys_base"],
        mu_view["mem_size"],
        mu_view["mem_size_actual"],
        mu_view.get("name", "Mu"),
    )

    if (m1n1_base, size) != (mu_base, mu_size):
        raise GpuHandoffError(
            "GPU initdata handoff derivations DISAGREE and the handoff cannot "
            f"be installed: m1n1 would validate against {m1n1_base:#x}+{size:#x} "
            f"(phys_base {m1n1_view['phys_base']:#x}, ram_base "
            f"{ram_base(m1n1_view['phys_base']):#x}, mem_size_actual "
            f"{m1n1_view['mem_size_actual']:#x}) while the guest boot_args give "
            f"{mu_base:#x}+{mu_size:#x} (phys_base {mu_view['phys_base']:#x}, "
            f"ram_base {ram_base(mu_view['phys_base']):#x}, mem_size_actual "
            f"{mu_view['mem_size_actual']:#x}). The two boot_args structs land in "
            "different 4 GiB regions; reduce --proxy-heap-size or fix the "
            "hypervisor guest phys_base before booting."
        )

    return {
        "base": m1n1_base,
        "size": size,
        "physical_memory_top": physical_memory_top(
            m1n1_view["phys_base"], m1n1_view["mem_size_actual"]
        ),
        "guest_memory_top": m1n1_view["phys_base"] + m1n1_view["mem_size"],
        "hwdata_a_base": m1n1_base + HWDATA_A_OFFSET,
        "hwdata_b_base": m1n1_base + HWDATA_B_OFFSET,
        "globals_base": m1n1_base + GLOBALS_OFFSET,
    }


def stamp_offset(map_size, payload_size):
    """Offset of the stamp inside an aperture; mirrors gpu_handoff_v1_stamp_offset()."""
    if map_size < STAMP_SIZE:
        raise GpuHandoffError(f"aperture {map_size:#x} is smaller than the stamp")
    offset = map_size - STAMP_SIZE
    if payload_size == 0 or payload_size > offset:
        raise GpuHandoffError(
            f"payload {payload_size:#x} would overlap the stamp at {offset:#x}"
        )
    return offset


def parse_stamp(raw):
    """Decode one stamp and check its self-consistency (not its identity)."""
    if len(raw) != STAMP_SIZE:
        raise GpuHandoffError(f"stamp is {len(raw)} bytes, expected {STAMP_SIZE}")
    values = struct.unpack(STAMP_FORMAT, raw)
    stamp = dict(zip(STAMP_FIELDS, values))

    if stamp["signature"] != SIGNATURE:
        raise GpuHandoffError(
            f"stamp signature {stamp['signature']:#010x}, expected {SIGNATURE:#010x} "
            "-- m1n1 did not write this aperture"
        )
    if stamp["version"] != VERSION or stamp["structure_size"] != STAMP_SIZE:
        raise GpuHandoffError(
            f"stamp version {stamp['version']}/size {stamp['structure_size']}, "
            f"expected {VERSION}/{STAMP_SIZE}"
        )
    for name in ("reserved0", "reserved1", "reserved2", "reserved3"):
        if stamp[name] != 0:
            raise GpuHandoffError(f"stamp {name} is {stamp[name]:#x}, expected 0")

    sealed = bytearray(raw)
    struct.pack_into("<I", sealed, STAMP_SIZE - 4, 0)
    recomputed = zlib.crc32(bytes(sealed)) & 0xFFFFFFFF
    if stamp["stamp_crc32"] == 0 or stamp["stamp_crc32"] != recomputed:
        raise GpuHandoffError(
            f"stamp CRC32 {stamp['stamp_crc32']:#010x}, recomputed {recomputed:#010x}"
        )
    return stamp


def verify_reservation(raw, agreed):
    """Judge the exact bytes m1n1 published, the way the driver will.

    ``raw`` is the whole reservation as read back over the proxy. Returns the
    hw_data_a stamp on success; raises on ANY mismatch. Nothing here is
    advisory -- every check below has a counterpart the Windows driver applies
    before it will start the ASC, and a disagreement here means the guest would
    either refuse to start the GPU or be handed the wrong initdata graph.
    """
    if len(raw) != RESERVATION_SIZE:
        raise GpuHandoffError(
            f"reservation read was {len(raw)} bytes, expected {RESERVATION_SIZE}"
        )

    base = agreed["base"]
    stamps = {}
    payload_crcs = {}

    for role, label, offset, map_size, size_key in APERTURES:
        aperture = raw[offset:offset + map_size]
        payload_size = EXPECTED[f"{size_key}_size"]
        off = stamp_offset(map_size, payload_size)
        stamp = parse_stamp(aperture[off:off + STAMP_SIZE])
        if stamp["role"] != role:
            raise GpuHandoffError(
                f"{label}: stamp role {stamp['role']}, expected {role} -- the "
                "three apertures are not one generation run"
            )
        stamps[label] = stamp
        payload_crcs[label] = zlib.crc32(aperture[:payload_size]) & 0xFFFFFFFF

    reference = stamps["hw_data_a"]

    # 1. Identity: exactly what AppleAgxGpu was compiled for, or nothing.
    mismatches = [
        f"{name}={reference[name]:#x}, expected {wanted:#x}"
        for name, wanted in EXPECTED.items()
        if reference[name] != wanted
    ]
    if mismatches:
        raise GpuHandoffError(
            "GPU initdata identity mismatch; AppleAgxGpu would be handed an "
            "initdata graph it was not built for (generator arm "
            f"{reference['builder_arm']} = "
            f"{BUILDER_NAMES.get(reference['builder_arm'], 'unknown')}): "
            + ", ".join(mismatches)
        )

    # 2. Geometry: the addresses m1n1 says it produced must be the ones we are
    #    about to publish in the ADT.
    geometry = {
        "reservation_base": base,
        "reservation_size": RESERVATION_SIZE,
        "hwdata_a_base": agreed["hwdata_a_base"],
        "hwdata_b_base": agreed["hwdata_b_base"],
        "globals_base": agreed["globals_base"],
    }
    bad_geometry = [
        f"{name}={reference[name]:#x}, expected {wanted:#x}"
        for name, wanted in geometry.items()
        if reference[name] != wanted
    ]
    if bad_geometry:
        raise GpuHandoffError(
            "GPU initdata geometry mismatch; the ADT would point Mu at "
            "different addresses than m1n1 filled: " + ", ".join(bad_geometry)
        )

    # 3. All three stamps describe ONE run: identical apart from role and CRC.
    for label, stamp in stamps.items():
        differing = [
            name
            for name in STAMP_FIELDS
            if name not in ("role", "stamp_crc32") and stamp[name] != reference[name]
        ]
        if differing:
            raise GpuHandoffError(
                f"{label} stamp disagrees with hw_data_a on {differing}; the "
                "apertures are a mixture of generation runs"
            )

    # 4. Content: the payloads are the exact bytes the stamp says they are.
    bad_crc = [
        f"{label}={payload_crcs[label]:#010x}, stamped "
        f"{reference[f'{key}_crc32']:#010x}"
        for _role, label, _off, _map, key in APERTURES
        if payload_crcs[label] != reference[f"{key}_crc32"]
    ]
    if bad_crc:
        raise GpuHandoffError(
            "GPU initdata payload CRC mismatch: " + ", ".join(bad_crc)
        )

    # 5. And only now, the weak check the driver would have used on its own:
    #    non-zero. Kept last, and kept explicit, so nobody reads its passing as
    #    evidence of anything the checks above did not already establish.
    for _role, label, offset, _map, key in APERTURES:
        payload = raw[offset:offset + EXPECTED[f"{key}_size"]]
        if not any(payload):
            raise GpuHandoffError(
                f"{label} payload is all zero despite a valid stamp; refusing"
            )

    return reference


def publish_adt_regions(adt, agreed):
    """Add the six /arm-io/sgx properties Mu probes for the preboot handoff.

    Mu's ``NtasiReserveGpuAdtCarveout()`` reads each as a bare native UINT64
    (``NtasiGpuDtNodeU64()`` requires at least 8 bytes and dereferences a
    ``UINT64 *``), so they are written as 8-byte little-endian scalars -- the
    same encoding as the ``gpu-region-base``/``-size`` properties already on
    that node.

    Returns the mapping that was written. Raises if ``/arm-io/sgx`` is missing,
    or if any of the six already exists -- an existing property would mean this
    boot has a live source for the handoff and silently overwriting it would
    hide that.
    """
    try:
        sgx = adt["arm-io"]["sgx"]
    except (KeyError, AttributeError) as exc:
        raise GpuHandoffError(
            "guest ADT has no /arm-io/sgx node; Mu cannot be told where the "
            "GPU preboot handoff is"
        ) from exc

    regions = (
        (ADT_PROPERTIES[0], agreed["hwdata_a_base"], HWDATA_A_MAP_SIZE),
        (ADT_PROPERTIES[1], agreed["hwdata_b_base"], HWDATA_B_MAP_SIZE),
        (ADT_PROPERTIES[2], agreed["globals_base"], GLOBALS_MAP_SIZE),
    )

    existing = [
        name
        for (base_prop, size_prop), _base, _size in regions
        for name in (base_prop, size_prop)
        if sgx.getprop(name) is not None
    ]
    if existing:
        raise GpuHandoffError(
            f"/arm-io/sgx already carries {existing}; this boot has a live "
            "preboot-handoff source and must not be overwritten"
        )

    written = {}
    for (base_prop, size_prop), base, size in regions:
        setattr(sgx, base_prop, struct.pack("<Q", base))
        setattr(sgx, size_prop, struct.pack("<Q", size))
        written[base_prop] = base
        written[size_prop] = size
    return written
