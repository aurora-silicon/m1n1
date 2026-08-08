/* SPDX-License-Identifier: MIT */

/*
 * AGX preboot initdata handoff for Windows.
 *
 * WHAT THIS REPLACES
 * ------------------
 * Mu publishes NTAS0023 with eight _CRS resources. Resources 5/6/7
 * (hw_data_a / hw_data_b / globals) have no live source on the chainload path,
 * so firmware backs them with one zero-filled EfiReservedMemoryType block and
 * says so honestly in _DSD (ntasp,preboot-handoff-present = 0). AppleAgxGpu
 * then stops at its calibration gate, which is the intended, non-destructive
 * outcome -- but it is a stop.
 *
 * m1n1 already contains the generator that produces those three blobs
 * (rust/src/gpu/, driven from the ADT power/perf tables). Until now it was
 * reachable from exactly one place, dt_set_gpu() on the Linux device-tree boot
 * path, which never runs here. This file makes it reachable from the proxy.
 *
 * WHAT IS AND IS NOT SAFE ABOUT IT
 * --------------------------------
 * Generating initdata READS the ADT and two GPU ID registers and WRITES DRAM.
 * It does not start the ASC, does not touch the mailbox, and -- deliberately,
 * because disabling the GPU on this machine breaks the boot -- never powers
 * anything down. get_core_counts() enables the /arm-io/sgx power domain if it
 * is not already on, exactly as the Linux path does, and leaves it on.
 *
 * THE PART THAT MATTERS MOST
 * --------------------------
 * Filling those pages makes the driver's existing "is it non-zero?" gate go
 * green on ANY content, including initdata generated for the wrong GPU variant
 * or the wrong firmware ABI -- for which the documented failure mode is an
 * unrecoverable firmware crash. So this file does not merely fill: it stamps
 * each aperture with which generator arm ran, on which silicon, for which
 * firmware, over which exact bytes (CRC32). The driver refuses anything that
 * is not a bit-exact match for what it was compiled against. See
 * gpu_handoff_abi.h.
 */

#include "../config.h"

#include "adt.h"
#include "firmware.h"
#include "gpu_handoff.h"
#include "gpu_handoff_abi.h"
#include "gpu_initdata.h"
#include "mcc.h"
#include "memory.h"
#include "platform_identity.h"
#include "string.h"
#include "types.h"
#include "utils.h"
#include "xnuboot.h"

#if defined(ENABLE_J414S_WINDOWS_GPU_INITDATA_HANDOFF)

enum gpu_handoff_error {
    GPU_HANDOFF_OK = 0,
    GPU_ERR_IDENTITY = -1,
    GPU_ERR_RESERVATION = -2,
    GPU_ERR_RESERVATION_NOT_CANONICAL = -3,
    GPU_ERR_RESERVATION_CLAIMED = -4,
    GPU_ERR_GENERATE = -5,
    GPU_ERR_GEOMETRY = -6,
    GPU_ERR_STAMP = -7,
    GPU_ERR_READBACK = -8,
};

struct gpu_handoff_aperture {
    u64 offset;
    u64 map_size;
    u32 role;
    const char *label;
};

static const struct gpu_handoff_aperture gpu_handoff_apertures[GPU_HANDOFF_V1_ROLE_COUNT] = {
    {GPU_HANDOFF_V1_HWDATA_A_OFFSET, GPU_HANDOFF_V1_HWDATA_A_MAP_SIZE,
     GPU_HANDOFF_V1_ROLE_HWDATA_A, "hw_data_a"},
    {GPU_HANDOFF_V1_HWDATA_B_OFFSET, GPU_HANDOFF_V1_HWDATA_B_MAP_SIZE,
     GPU_HANDOFF_V1_ROLE_HWDATA_B, "hw_data_b"},
    {GPU_HANDOFF_V1_GLOBALS_OFFSET, GPU_HANDOFF_V1_GLOBALS_MAP_SIZE, GPU_HANDOFF_V1_ROLE_GLOBALS,
     "globals"},
};

static u64 gpu_physical_memory_top(void)
{
    return ALIGN_DOWN(cur_boot_args.phys_base, BIT(32)) + mem_size_actual;
}

/*
 * The single address m1n1 and the host must agree on.
 *
 * Unlike the wireless handoff, Mu does NOT derive this: it reads
 * hw-data-a-base/-size and friends out of the /arm-io/sgx ADT node, which the
 * host writes into the guest ADT before HV.start() uploads it. So the
 * agreement here is between m1n1 and the *host*, and both compute it the same
 * way from the same boot_args:
 *
 *     phys_top = ALIGN_DOWN(boot_args.phys_base, 4 GiB) + mem_size_actual
 *     base     = ALIGN_DOWN(phys_top - TOP_MARGIN - RESERVATION_SIZE, 16 KiB)
 *
 * TOP_MARGIN keeps the whole reservation clear of the top-of-DRAM band that
 * holds the wireless handoff carveout and the eight /defaults pmap-io-ranges
 * windows measured on this machine. That is margin, not proof; the proof is
 * gpu_range_is_claimed() below, which fails closed.
 */
static u64 gpu_canonical_reservation_base(void)
{
    u64 physical_top = gpu_physical_memory_top();
    u64 span = GPU_HANDOFF_V1_TOP_MARGIN + GPU_HANDOFF_V1_RESERVATION_SIZE;

    if (physical_top <= span)
        return 0;

    return ALIGN_DOWN(physical_top - span, GPU_HANDOFF_V1_PAGE_SIZE);
}

static bool gpu_ranges_overlap(u64 a_base, u64 a_size, u64 b_base, u64 b_size)
{
    if (!a_size || !b_size)
        return false;
    return a_base < b_base + b_size && b_base < a_base + a_size;
}

/*
 * Prove the derived address is not already owned by firmware.
 *
 * Same two claim classes, and the same fail-closed policy, as
 * wlan_range_is_claimed() in src/wireless_handoff.c: MCC TrustZone carveouts
 * are unmapped from m1n1's own page tables (a memset into one is an
 * unrecoverable EL2 data abort), and /defaults pmap-io-ranges windows stay
 * mapped, so a write there succeeds and silently corrupts live firmware state.
 *
 * Duplicated rather than shared because the wireless version is compiled only
 * with ENABLE_J414S_WINDOWS_WIRELESS_HANDOFF and the two handoffs must be
 * independently disableable. The duplication is 30 lines of pure predicate
 * with no state; the alternative -- one handoff silently not checking because
 * the other was compiled out -- is worse.
 */
static bool gpu_range_is_claimed(u64 base, u64 size)
{
    const u32 *ranges;
    u32 length = 0;
    int node;

    if (mcc_carveout_count == 0) {
        printf("gpu-handoff: no MCC carveouts enumerated; cannot prove %#llx+%#llx is free\n",
               (unsigned long long)base, (unsigned long long)size);
        return true;
    }

    for (size_t index = 0; index < mcc_carveout_count; index++) {
        if (gpu_ranges_overlap(base, size, mcc_carveouts[index].base,
                               mcc_carveouts[index].size)) {
            printf("gpu-handoff: reservation %#llx+%#llx overlaps TZ carveout %#llx+%#llx\n",
                   (unsigned long long)base, (unsigned long long)size,
                   (unsigned long long)mcc_carveouts[index].base,
                   (unsigned long long)mcc_carveouts[index].size);
            return true;
        }
    }

    node = adt_path_offset(adt, "/defaults");
    if (node < 0) {
        printf("gpu-handoff: no /defaults node; cannot prove %#llx+%#llx is free\n",
               (unsigned long long)base, (unsigned long long)size);
        return true;
    }
    ranges = adt_getprop(adt, node, "pmap-io-ranges", &length);
    if (!ranges || length < 24) {
        printf("gpu-handoff: no pmap-io-ranges; cannot prove %#llx+%#llx is free\n",
               (unsigned long long)base, (unsigned long long)size);
        return true;
    }

    /* Six u32 per entry: base_lo, base_hi, size_lo, size_hi, flags, unused. */
    for (u32 entry = 0; (entry + 6) * 4 <= length; entry += 6) {
        u64 range_base = ranges[entry] | ((u64)ranges[entry + 1] << 32);
        u64 range_size = ranges[entry + 2] | ((u64)ranges[entry + 3] << 32);

        if (gpu_ranges_overlap(base, size, range_base, range_size)) {
            printf("gpu-handoff: reservation %#llx+%#llx overlaps firmware range %#llx+%#llx\n",
                   (unsigned long long)base, (unsigned long long)size,
                   (unsigned long long)range_base, (unsigned long long)range_size);
            return true;
        }
    }

    return false;
}

static int gpu_validate_reservation(u64 base, u64 size)
{
    u64 physical_top = gpu_physical_memory_top();
    u64 guest_top = cur_boot_args.phys_base + cur_boot_args.mem_size;
    u64 canonical = gpu_canonical_reservation_base();

    if (size != GPU_HANDOFF_V1_RESERVATION_SIZE || (base & (GPU_HANDOFF_V1_PAGE_SIZE - 1)) ||
        base > ~0ULL - size)
        return GPU_ERR_RESERVATION;

    /*
     * Above the reduced boot_args top proves Mu cannot hand this memory to
     * Windows as SystemMemory; below physical top proves it is real DRAM.
     */
    if (base < guest_top + SZ_16K || base + size > physical_top)
        return GPU_ERR_RESERVATION;

    if (!canonical || base != canonical) {
        printf("gpu-handoff: reservation %#llx is not the canonical derivation %#llx "
               "(phys_base %#llx, mem_size_actual %#llx, phys_top %#llx); the ADT would "
               "point Mu elsewhere\n",
               (unsigned long long)base, (unsigned long long)canonical,
               (unsigned long long)cur_boot_args.phys_base, (unsigned long long)mem_size_actual,
               (unsigned long long)physical_top);
        return GPU_ERR_RESERVATION_NOT_CANONICAL;
    }

    if (gpu_range_is_claimed(base, size))
        return GPU_ERR_RESERVATION_CLAIMED;

    return GPU_HANDOFF_OK;
}

static void gpu_build_stamp(struct gpu_handoff_stamp_v1 *stamp,
                            const struct gpu_initdata_ident *ident, u64 base, u32 role,
                            u32 crc_a, u32 crc_b, u32 crc_globals)
{
    memset(stamp, 0, sizeof(*stamp));

    stamp->signature = GPU_HANDOFF_V1_SIGNATURE;
    stamp->version = GPU_HANDOFF_V1_VERSION;
    stamp->structure_size = GPU_HANDOFF_V1_STAMP_SIZE;
    stamp->generator_abi = GPU_HANDOFF_V1_GENERATOR_ABI;
    stamp->role = role;

    stamp->chip_id = ident->chip_id;
    stamp->gpu_gen = ident->gpu_gen;
    stamp->gpu_variant = ident->gpu_variant;
    stamp->gpu_core = ident->gpu_core;
    stamp->gpu_rev_id = ident->gpu_rev_id;
    stamp->num_cores = ident->num_cores;

    stamp->firmware_compat_maj = ident->compat_maj;
    stamp->firmware_compat_min = ident->compat_min;
    stamp->builder_arm = ident->builder_arm;

    stamp->os_firmware_0 = os_firmware.num[0];
    stamp->os_firmware_1 = os_firmware.num[1];
    stamp->os_firmware_2 = os_firmware.num[2];

    stamp->hwdata_a_size = ident->data_a_size;
    stamp->hwdata_b_size = ident->data_b_size;
    stamp->globals_size = ident->globals_size;

    stamp->hwdata_a_map_size = GPU_HANDOFF_V1_HWDATA_A_MAP_SIZE;
    stamp->hwdata_b_map_size = GPU_HANDOFF_V1_HWDATA_B_MAP_SIZE;
    stamp->globals_map_size = GPU_HANDOFF_V1_GLOBALS_MAP_SIZE;

    stamp->reservation_base = base;
    stamp->reservation_size = GPU_HANDOFF_V1_RESERVATION_SIZE;
    stamp->hwdata_a_base = base + GPU_HANDOFF_V1_HWDATA_A_OFFSET;
    stamp->hwdata_b_base = base + GPU_HANDOFF_V1_HWDATA_B_OFFSET;
    stamp->globals_base = base + GPU_HANDOFF_V1_GLOBALS_OFFSET;

    stamp->hwdata_a_crc32 = crc_a;
    stamp->hwdata_b_crc32 = crc_b;
    stamp->globals_crc32 = crc_globals;

    gpu_handoff_v1_stamp_seal(stamp);
}

int gpu_handoff_init(u64 reservation_base, u64 reservation_size)
{
    struct gpu_initdata_ident ident;
    struct gpu_handoff_stamp_v1 stamp;
    u64 stamp_offset[GPU_HANDOFF_V1_ROLE_COUNT];
    u32 payload_size[GPU_HANDOFF_V1_ROLE_COUNT];
    u32 crc[GPU_HANDOFF_V1_ROLE_COUNT];
    void *aperture[GPU_HANDOFF_V1_ROLE_COUNT];
    int status;

    if (!platform_is_j414s()) {
        printf("gpu-handoff: exact J414s platform identity mismatch\n");
        return GPU_ERR_IDENTITY;
    }

    status = gpu_validate_reservation(reservation_base, reservation_size);
    if (status) {
        printf("gpu-handoff: invalid reservation %#llx+%#llx (%d)\n",
               (unsigned long long)reservation_base, (unsigned long long)reservation_size, status);
        return status;
    }

    for (u32 index = 0; index < GPU_HANDOFF_V1_ROLE_COUNT; index++)
        aperture[index] = (void *)(reservation_base + gpu_handoff_apertures[index].offset);

    /*
     * The whole reservation is zeroed here as well as inside
     * gpu_initdata_generate(), so the stamp area and any inter-aperture
     * padding are deterministic even if generation fails halfway.
     */
    memset((void *)reservation_base, 0, (size_t)reservation_size);

    if (gpu_initdata_generate(aperture[0], GPU_HANDOFF_V1_HWDATA_A_MAP_SIZE, aperture[1],
                              GPU_HANDOFF_V1_HWDATA_B_MAP_SIZE, aperture[2],
                              GPU_HANDOFF_V1_GLOBALS_MAP_SIZE, &ident)) {
        memset((void *)reservation_base, 0, (size_t)reservation_size);
        dc_civac_range((void *)reservation_base, (size_t)reservation_size);
        printf("gpu-handoff: initdata generation failed; reservation left zeroed so the "
               "driver still sees placeholders rather than half a graph\n");
        return GPU_ERR_GENERATE;
    }

    payload_size[0] = ident.data_a_size;
    payload_size[1] = ident.data_b_size;
    payload_size[2] = ident.globals_size;

    for (u32 index = 0; index < GPU_HANDOFF_V1_ROLE_COUNT; index++) {
        stamp_offset[index] = gpu_handoff_v1_stamp_offset(gpu_handoff_apertures[index].map_size,
                                                          payload_size[index]);
        if (!stamp_offset[index]) {
            memset((void *)reservation_base, 0, (size_t)reservation_size);
            dc_civac_range((void *)reservation_base, (size_t)reservation_size);
            printf("gpu-handoff: %s payload 0x%x leaves no room for the stamp in 0x%llx\n",
                   gpu_handoff_apertures[index].label, payload_size[index],
                   (unsigned long long)gpu_handoff_apertures[index].map_size);
            return GPU_ERR_GEOMETRY;
        }
        crc[index] = gpu_handoff_v1_crc32(aperture[index], payload_size[index]);
    }

    for (u32 index = 0; index < GPU_HANDOFF_V1_ROLE_COUNT; index++) {
        gpu_build_stamp(&stamp, &ident, reservation_base, gpu_handoff_apertures[index].role, crc[0],
                        crc[1], crc[2]);
        memcpy((u8 *)aperture[index] + stamp_offset[index], &stamp, sizeof(stamp));
    }

    /*
     * Push it all to DRAM. Mu and Windows read these pages through their own
     * mappings and neither of them participates in m1n1's cache maintenance.
     */
    dc_civac_range((void *)reservation_base, (size_t)reservation_size);

    /* Read the published bytes back the way a consumer will, and re-judge. */
    for (u32 index = 0; index < GPU_HANDOFF_V1_ROLE_COUNT; index++) {
        const struct gpu_handoff_stamp_v1 *published =
            (const struct gpu_handoff_stamp_v1 *)((u8 *)aperture[index] + stamp_offset[index]);

        if (gpu_handoff_v1_stamp_validate(published, gpu_handoff_apertures[index].role) ||
            gpu_handoff_v1_crc32(aperture[index], payload_size[index]) != crc[index]) {
            memset((void *)reservation_base, 0, (size_t)reservation_size);
            dc_civac_range((void *)reservation_base, (size_t)reservation_size);
            printf("gpu-handoff: %s stamp/payload readback failed; reservation zeroed\n",
                   gpu_handoff_apertures[index].label);
            return GPU_ERR_READBACK;
        }
    }

    if (ident.builder_arm != GPU_HANDOFF_V1_BUILDER_G14X_V13_5)
        printf("gpu-handoff: WARNING: generator arm %u is not G14X/13.5; AppleAgxGpu 0.8.0.0 "
               "will refuse this handoff by design\n",
               ident.builder_arm);

    printf("gpu-handoff: published %#llx+%#llx -- hw_data_a %#llx+0x%llx (payload 0x%x crc "
           "0x%08x), hw_data_b %#llx+0x%llx (payload 0x%x crc 0x%08x), globals %#llx+0x%llx "
           "(payload 0x%x crc 0x%08x)\n",
           (unsigned long long)reservation_base, (unsigned long long)reservation_size,
           (unsigned long long)(reservation_base + GPU_HANDOFF_V1_HWDATA_A_OFFSET),
           (unsigned long long)GPU_HANDOFF_V1_HWDATA_A_MAP_SIZE, payload_size[0], crc[0],
           (unsigned long long)(reservation_base + GPU_HANDOFF_V1_HWDATA_B_OFFSET),
           (unsigned long long)GPU_HANDOFF_V1_HWDATA_B_MAP_SIZE, payload_size[1], crc[1],
           (unsigned long long)(reservation_base + GPU_HANDOFF_V1_GLOBALS_OFFSET),
           (unsigned long long)GPU_HANDOFF_V1_GLOBALS_MAP_SIZE, payload_size[2], crc[2]);
    printf("gpu-handoff: stamp v%u abi %u builder arm %u, chip %#x gen %u variant %c core %u "
           "rev %u (%u cores), firmware ABI %u.%u; canonical derivation confirmed (phys_top "
           "%#llx, guest_top %#llx)\n",
           GPU_HANDOFF_V1_VERSION, GPU_HANDOFF_V1_GENERATOR_ABI, ident.builder_arm, ident.chip_id,
           ident.gpu_gen, (char)ident.gpu_variant, ident.gpu_core, ident.gpu_rev_id,
           ident.num_cores, ident.compat_maj, ident.compat_min,
           (unsigned long long)gpu_physical_memory_top(),
           (unsigned long long)(cur_boot_args.phys_base + cur_boot_args.mem_size));

    return GPU_HANDOFF_OK;
}

#else

int gpu_handoff_init(u64 reservation_base, u64 reservation_size)
{
    UNUSED(reservation_base);
    UNUSED(reservation_size);
    return 0;
}

#endif
