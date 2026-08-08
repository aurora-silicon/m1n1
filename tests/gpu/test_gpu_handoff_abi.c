/* SPDX-License-Identifier: MIT */

/*
 * The GPU handoff stamp ABI, judged on its own.
 *
 * These tests exist because of one specific failure mode: once m1n1 can write
 * the AGX calibration apertures, AppleAgxGpu's "not all zero" gate stops being
 * evidence of anything. The stamp is what restores identification, so its
 * geometry and its self-consistency check are load-bearing, not decoration.
 */

#include "gpu_handoff_abi.h"

#include <stdio.h>
#include <string.h>

static int failures;

static void check(int cond, const char *what)
{
    if (!cond) {
        printf("gpu-handoff-abi: FAIL: %s\n", what);
        failures++;
    }
}

/* Payload sizes AppleAgxGpu hardcodes for G14X firmware 13.5, proven equal to
 * the generator's struct sizes by the const asserts in
 * rust/src/gpu/initdata.rs. Duplicated here only so this file can check the
 * geometry without pulling in the Rust side. */
#define PAYLOAD_A 0x6c34U
#define PAYLOAD_B 0x1884U
#define PAYLOAD_G 0x1715cU

static void test_layout(void)
{
    check(sizeof(struct gpu_handoff_stamp_v1) == 160,
          "stamp is exactly 160 packed bytes");
    check(GPU_HANDOFF_V1_STAMP_SIZE == 160, "declared stamp size agrees");

    /* Offsets the Python and Windows mirrors depend on. If any of these move,
     * all three sides must move together. */
    check(offsetof(struct gpu_handoff_stamp_v1, signature) == 0, "signature at 0");
    check(offsetof(struct gpu_handoff_stamp_v1, role) == 12, "role at 12");
    check(offsetof(struct gpu_handoff_stamp_v1, builder_arm) == 48, "builder_arm at 48");
    check(offsetof(struct gpu_handoff_stamp_v1, reservation_base) == 96,
          "reservation_base at 96");
    check(offsetof(struct gpu_handoff_stamp_v1, hwdata_a_crc32) == 136,
          "hwdata_a_crc32 at 136");
    check(offsetof(struct gpu_handoff_stamp_v1, stamp_crc32) == 156,
          "stamp_crc32 is the last field");
}

static void test_stamp_never_overlaps_payload(void)
{
    /*
     * THE geometry invariant. If the stamp sat inside the payload, the
     * driver's has-data scan would read stamp bytes and an all-zero payload
     * would look populated -- which is precisely the correlate-vs-identify
     * defect the stamp exists to remove.
     */
    check(gpu_handoff_v1_stamp_offset(GPU_HANDOFF_V1_HWDATA_A_MAP_SIZE, PAYLOAD_A) >
              PAYLOAD_A,
          "hw_data_a stamp sits past its payload");
    check(gpu_handoff_v1_stamp_offset(GPU_HANDOFF_V1_HWDATA_B_MAP_SIZE, PAYLOAD_B) >
              PAYLOAD_B,
          "hw_data_b stamp sits past its payload");
    check(gpu_handoff_v1_stamp_offset(GPU_HANDOFF_V1_GLOBALS_MAP_SIZE, PAYLOAD_G) >
              PAYLOAD_G,
          "globals stamp sits past its payload");

    check(gpu_handoff_v1_stamp_offset(GPU_HANDOFF_V1_HWDATA_A_MAP_SIZE,
                                      GPU_HANDOFF_V1_HWDATA_A_MAP_SIZE) == 0,
          "a payload filling the aperture is refused, not silently trimmed");
    check(gpu_handoff_v1_stamp_offset(GPU_HANDOFF_V1_HWDATA_A_MAP_SIZE,
                                      GPU_HANDOFF_V1_HWDATA_A_MAP_SIZE -
                                          GPU_HANDOFF_V1_STAMP_SIZE + 1) == 0,
          "a payload one byte into the stamp is refused");
    check(gpu_handoff_v1_stamp_offset(GPU_HANDOFF_V1_HWDATA_A_MAP_SIZE, 0) == 0,
          "an empty payload is refused");
    check(gpu_handoff_v1_stamp_offset(64, 8) == 0, "an undersized aperture is refused");
}

static void test_crc32_is_zlib(void)
{
    /* The standard CRC-32 check value. The host stamps with Python's
     * zlib.crc32 and the Windows driver with its own copy; pinning all three
     * to this constant pins them to the same arithmetic rather than to each
     * other's bugs. */
    check(gpu_handoff_v1_crc32("123456789", 9) == 0xcbf43926U,
          "CRC32 is the standard zlib/PNG CRC-32");
    check(gpu_handoff_v1_crc32("", 0) == 0, "CRC32 of nothing is 0");
}

static void seal_valid(struct gpu_handoff_stamp_v1 *stamp, u32 role)
{
    memset(stamp, 0, sizeof(*stamp));
    stamp->signature = GPU_HANDOFF_V1_SIGNATURE;
    stamp->version = GPU_HANDOFF_V1_VERSION;
    stamp->structure_size = GPU_HANDOFF_V1_STAMP_SIZE;
    stamp->generator_abi = GPU_HANDOFF_V1_GENERATOR_ABI;
    stamp->role = role;
    stamp->builder_arm = GPU_HANDOFF_V1_BUILDER_G14X_V13_5;
    gpu_handoff_v1_stamp_seal(stamp);
}

static void test_stamp_validate(void)
{
    struct gpu_handoff_stamp_v1 stamp;

    seal_valid(&stamp, GPU_HANDOFF_V1_ROLE_GLOBALS);
    check(gpu_handoff_v1_stamp_validate(&stamp, GPU_HANDOFF_V1_ROLE_GLOBALS) == 0,
          "a sealed stamp validates for its own role");
    check(gpu_handoff_v1_stamp_validate(&stamp, GPU_HANDOFF_V1_ROLE_HWDATA_A) != 0,
          "a stamp does not validate for another aperture's role");
    check(gpu_handoff_v1_stamp_validate(NULL, GPU_HANDOFF_V1_ROLE_GLOBALS) != 0,
          "a null stamp is refused");
    check(gpu_handoff_v1_stamp_validate(&stamp, GPU_HANDOFF_V1_ROLE_COUNT) != 0,
          "an out-of-range role is refused");

    stamp.signature ^= 1;
    check(gpu_handoff_v1_stamp_validate(&stamp, GPU_HANDOFF_V1_ROLE_GLOBALS) != 0,
          "a wrong signature is refused");
    stamp.signature ^= 1;

    stamp.version = GPU_HANDOFF_V1_VERSION + 1;
    check(gpu_handoff_v1_stamp_validate(&stamp, GPU_HANDOFF_V1_ROLE_GLOBALS) != 0,
          "a future stamp version is refused, not read as v1");
    stamp.version = GPU_HANDOFF_V1_VERSION;

    stamp.reserved2 = 1;
    check(gpu_handoff_v1_stamp_validate(&stamp, GPU_HANDOFF_V1_ROLE_GLOBALS) != 0,
          "a nonzero reserved field is refused");
    stamp.reserved2 = 0;

    /* Tampering without resealing is the realistic corruption case. */
    stamp.builder_arm = GPU_HANDOFF_V1_BUILDER_G13_V12_3;
    check(gpu_handoff_v1_stamp_validate(&stamp, GPU_HANDOFF_V1_ROLE_GLOBALS) != 0,
          "an edited-but-unsealed stamp is refused");
    gpu_handoff_v1_stamp_seal(&stamp);
    check(gpu_handoff_v1_stamp_validate(&stamp, GPU_HANDOFF_V1_ROLE_GLOBALS) == 0,
          "self-consistency deliberately does NOT judge identity -- that is "
          "the consumer's job, because only it knows what it was built for");

    memset(&stamp, 0, sizeof(stamp));
    check(gpu_handoff_v1_stamp_validate(&stamp, GPU_HANDOFF_V1_ROLE_HWDATA_A) != 0,
          "an all-zero region is not a valid stamp");
}

int main(void)
{
    test_layout();
    test_stamp_never_overlaps_payload();
    test_crc32_is_zlib();
    test_stamp_validate();

    if (failures) {
        printf("gpu-handoff-abi: %d check(s) FAILED\n", failures);
        return 1;
    }
    printf("gpu-handoff-abi: stamp geometry and self-consistency contract passed\n");
    return 0;
}
