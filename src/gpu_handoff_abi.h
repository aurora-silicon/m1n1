/* SPDX-License-Identifier: MIT */

/*
 * AGX preboot initdata handoff, ABI v1 -- the *identifying* stamp.
 *
 * WHY THIS EXISTS AT ALL
 * ----------------------
 * AppleAgxGpu.sys decides whether the three calibration blobs it was handed
 * are real by asking "are they all zero?" (ntasi_agx_initdata_blob_has_data()
 * in cores/agx-initdata-core/agx_initdata.c). That question separates "Mu's
 * zero-filled placeholder" from "something". It does NOT separate "the
 * initdata graph this driver was built for" from "an initdata graph generated
 * for another GPU variant or another firmware version" -- and both of those
 * are non-zero.
 *
 * m1n1's generator is version-gated across five (gpu_gen, gpu_variant,
 * firmware) arms (rust/src/gpu/initdata.rs). Four of the five produce a
 * DIFFERENT structure layout for the same three names. Handing firmware the
 * wrong one is not a soft failure: the documented outcome is an unrecoverable
 * ASC firmware crash that needs a full system reboot.
 *
 * So the moment m1n1 starts filling those pages, "non-zero" stops being
 * evidence of anything. This stamp is what replaces it: m1n1 records WHICH
 * generator arm ran, on WHICH silicon, for WHICH firmware ABI, over exactly
 * WHICH bytes (CRC32), and the driver refuses anything that is not a bit-exact
 * match for what it was compiled against.
 *
 * WHERE IT LIVES
 * --------------
 * One stamp is written at the END of each of the three published apertures,
 * at `aperture_base + aperture_map_size - GPU_HANDOFF_V1_STAMP_SIZE`.
 *
 * That location is deliberate and it is provably outside the payload:
 *
 *   aperture      map size   payload (G14X/13.5)   stamp offset
 *   hw_data_a     0x08000    0x06c34               0x07f60
 *   hw_data_b     0x04000    0x01884               0x03f60
 *   globals       0x18000    0x1715c               0x17f60
 *
 * The driver's has-data scan covers only the payload length, so the stamp can
 * never be what makes an otherwise-empty aperture look "non-zero". The three
 * stamps are byte-identical apart from `role` and `stamp_crc32`, which is what
 * proves the three apertures are ONE generation run rather than a mixture.
 *
 * Mirrored, field for field, by the Windows driver in
 * AuroraSilicon/drivers/AppleAgxGpu/cores/agx-initdata-core/agx_initdata.h
 * (struct ntasi_agx_handoff_stamp_v1) and by the host in
 * proxyclient/m1n1/gpu_handoff.py. All three must be changed together, and
 * GPU_HANDOFF_V1_GENERATOR_ABI must be bumped when they are.
 */

#ifndef GPU_HANDOFF_ABI_H
#define GPU_HANDOFF_ABI_H

#ifdef GPU_HANDOFF_ABI_HOST_TEST
#include <stddef.h>
#include <stdint.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
#else
#include "types.h"
#endif

/* "AGG1" */
#define GPU_HANDOFF_V1_SIGNATURE 0x31474741U
#define GPU_HANDOFF_V1_VERSION   1U
#define GPU_HANDOFF_V1_STAMP_SIZE 160U

/*
 * Bumped on ANY change to what the rust GPU generator emits, to this struct, or to
 * the aperture geometry below. An exact-match requirement on both sides makes
 * a stale m1n1 paired with a new driver (or the reverse) a loud refusal rather
 * than a silently mismatched initdata graph.
 */
#define GPU_HANDOFF_V1_GENERATOR_ABI 1U

/* Which aperture a given stamp was written into. */
#define GPU_HANDOFF_V1_ROLE_HWDATA_A 0U
#define GPU_HANDOFF_V1_ROLE_HWDATA_B 1U
#define GPU_HANDOFF_V1_ROLE_GLOBALS  2U
#define GPU_HANDOFF_V1_ROLE_COUNT    3U

/*
 * Which arm of rust_fill_gpu_initdata()'s (gpu_gen, gpu_variant, compat_maj,
 * compat_min) match actually produced the bytes. This is the field that says
 * what the data IS; every other identity field only says what the machine was.
 */
#define GPU_HANDOFF_V1_BUILDER_NONE      0U
#define GPU_HANDOFF_V1_BUILDER_G13_V12_3 1U
#define GPU_HANDOFF_V1_BUILDER_G14_V12_4 2U
#define GPU_HANDOFF_V1_BUILDER_G13_V13_5 3U
#define GPU_HANDOFF_V1_BUILDER_G14_V13_5 4U
#define GPU_HANDOFF_V1_BUILDER_G14X_V13_5 5U

/*
 * Aperture geometry. These are the sizes Mu publishes in NTAS0023's _CRS
 * resources 5/6/7 (NTASI_GPU_HWDATA_A_SIZE and friends in
 * mu/Silicon/Apple/AppleSiliconPkg/Drivers/AcpiPlatformDxe/AcpiPlatform.c) and
 * the sizes AppleAgxGpu validates exactly (NTASI_AGX_T6020_*_RESERVATION_SIZE
 * in cores/agx-resource-core/agx_resource.h). They are NOT the payload sizes;
 * the payload sizes come from rust_gpu_initdata_size() at run time and are
 * recorded in the stamp.
 */
#define GPU_HANDOFF_V1_HWDATA_A_OFFSET   0x00000ULL
#define GPU_HANDOFF_V1_HWDATA_A_MAP_SIZE 0x08000ULL
#define GPU_HANDOFF_V1_HWDATA_B_OFFSET   0x08000ULL
#define GPU_HANDOFF_V1_HWDATA_B_MAP_SIZE 0x04000ULL
#define GPU_HANDOFF_V1_GLOBALS_OFFSET    0x0c000ULL
#define GPU_HANDOFF_V1_GLOBALS_MAP_SIZE  0x18000ULL
#define GPU_HANDOFF_V1_RESERVATION_SIZE  0x24000ULL
#define GPU_HANDOFF_V1_PAGE_SIZE         0x4000ULL

/*
 * How far below the top of physical DRAM the reservation is placed.
 *
 * The wireless handoff already owns [phys_top - 0x10000, phys_top), and the
 * measured J414s ADT puts eight /defaults pmap-io-ranges windows in the top
 * 4 MiB of DRAM (the highest ending at 0x103fffbc000; see the comment on
 * wlan_range_is_claimed() in src/wireless_handoff.c). Sitting immediately
 * below that whole band -- rather than immediately below the wireless
 * reservation -- buys 4 MiB of margin from the one class of address that is
 * both writable and live firmware state. It is still checked, not assumed:
 * gpu_handoff_init() fails closed against MCC TZ carveouts and pmap-io-ranges.
 */
#define GPU_HANDOFF_V1_TOP_MARGIN 0x400000ULL

struct gpu_handoff_stamp_v1 {
    u32 signature;
    u16 version;
    u16 structure_size;
    u32 generator_abi;
    u32 role;

    /* Silicon identity, from the rust GPU HWCONFIG tables for this chip. */
    u32 chip_id;
    u32 gpu_gen;
    u32 gpu_variant;
    u32 gpu_core;
    u32 gpu_rev_id;
    u32 num_cores;

    /* Firmware ABI the generator targeted (m1n1's `compat`, not os_firmware). */
    u32 firmware_compat_maj;
    u32 firmware_compat_min;

    /* Which generator arm ran. See GPU_HANDOFF_V1_BUILDER_*. */
    u32 builder_arm;

    /* The macOS firmware m1n1 detected, for the log; NOT part of the gate. */
    u32 os_firmware_0;
    u32 os_firmware_1;
    u32 os_firmware_2;

    /* Payload lengths rust_gpu_initdata_size() reported for this arm. */
    u32 hwdata_a_size;
    u32 hwdata_b_size;
    u32 globals_size;

    /* Published aperture lengths (must equal the GPU_HANDOFF_V1_*_MAP_SIZE). */
    u32 hwdata_a_map_size;
    u32 hwdata_b_map_size;
    u32 globals_map_size;

    u32 reserved0;
    u32 reserved1;

    u64 reservation_base;
    u64 reservation_size;
    u64 hwdata_a_base;
    u64 hwdata_b_base;
    u64 globals_base;

    /* CRC32 (reflected, poly 0xedb88320, zlib-compatible) of each PAYLOAD. */
    u32 hwdata_a_crc32;
    u32 hwdata_b_crc32;
    u32 globals_crc32;

    u32 reserved2;
    u32 reserved3;

    /* CRC32 over this struct with this field zeroed. Never zero when valid. */
    u32 stamp_crc32;
} __attribute__((packed));

_Static_assert(sizeof(struct gpu_handoff_stamp_v1) == GPU_HANDOFF_V1_STAMP_SIZE,
               "GPU handoff ABI v1 stamp size");
_Static_assert(GPU_HANDOFF_V1_HWDATA_A_OFFSET + GPU_HANDOFF_V1_HWDATA_A_MAP_SIZE ==
                   GPU_HANDOFF_V1_HWDATA_B_OFFSET,
               "hw_data_a aperture must abut hw_data_b");
_Static_assert(GPU_HANDOFF_V1_HWDATA_B_OFFSET + GPU_HANDOFF_V1_HWDATA_B_MAP_SIZE ==
                   GPU_HANDOFF_V1_GLOBALS_OFFSET,
               "hw_data_b aperture must abut globals");
_Static_assert(GPU_HANDOFF_V1_GLOBALS_OFFSET + GPU_HANDOFF_V1_GLOBALS_MAP_SIZE ==
                   GPU_HANDOFF_V1_RESERVATION_SIZE,
               "globals aperture must end the reservation");
_Static_assert((GPU_HANDOFF_V1_RESERVATION_SIZE % GPU_HANDOFF_V1_PAGE_SIZE) == 0,
               "reservation must be a whole number of 16 KiB pages");

u32 gpu_handoff_v1_crc32(const void *data, u32 length);

/*
 * Offset of the stamp inside an aperture of `map_size` bytes. Returns 0 if the
 * payload would reach into the stamp, which the caller must treat as fatal.
 */
u64 gpu_handoff_v1_stamp_offset(u64 map_size, u64 payload_size);

/*
 * Self-consistency only: signature, version, size, role, reserved fields and
 * the stamp's own CRC32. It deliberately does NOT judge the identity fields --
 * that is the consumer's job, because only the consumer knows what it was
 * built for.
 *
 * Returns 0 on success, negative otherwise.
 */
int gpu_handoff_v1_stamp_validate(const struct gpu_handoff_stamp_v1 *stamp, u32 expected_role);

/* Compute and install stamp->stamp_crc32. */
void gpu_handoff_v1_stamp_seal(struct gpu_handoff_stamp_v1 *stamp);

#endif
