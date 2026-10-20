/* SPDX-License-Identifier: MIT */

#ifndef G18G_IDENTITY_H
#define G18G_IDENTITY_H

#include <stddef.h>
#include <stdint.h>

/*
 * Identity of the J873 (T8152, G18G) GPU firmware TEXT that iBoot loaded.
 *
 * Port of aurora-silicon/m1n1-aurora j873 experiments/j873/installer-prep-26A428/
 * g18g_firmware_identity.py. The same rule: the boot TLV layout must match, the
 * random, loader-filled and boot-filled values are zeroed, and the SHA-256 of the
 * result must equal the pinned digest. The loader-filled values are then checked
 * against what this boot's ADT predicts. It is identity evidence only; it does not
 * qualify the firmware for runtime use.
 */

#define G18G_PROFILE         "G18G-RTKit-3514.0.15.release"
#define G18G_CHOSEN_PROPERTY "aurora,g18g-gpu-firmware-profile"

enum g18g_role {
    G18G_GFX = 0,
    G18G_GFX1 = 1,
    G18G_ROLES = 2,
};

struct g18g_role_profile {
    const char *image;
    const char *uuid;
    size_t text_size;
};

struct g18g_loader_expect {
    uint64_t ptpb; /* gfx-shared-region base plus role index times half its size */
    uint64_t ptps; /* half of the gfx-shared-region size */
    uint64_t pplb; /* gfx-handoff base */
    uint64_t gx2s; /* gfx-shared-l2-region base */
    uint64_t ptvb; /* rtkit-private-vm-region-base */
    uint64_t wrad; /* ASC wrapper register base */
    uint64_t soc;  /* chip id */
};

enum g18g_result {
    G18G_OK = 0,
    G18G_BAD_ROLE = -1,
    G18G_BAD_SIZE = -2,
    G18G_BAD_LAYOUT = -3,
    G18G_BAD_DIGEST = -4,
    G18G_BAD_LOADER_VALUE = -5,
};

const struct g18g_role_profile *g18g_profile(enum g18g_role role);

/* Predict the loader-filled TLVs from the validated ADT layout. */
void g18g_expected_loader(enum g18g_role role, uint64_t shared_base, uint64_t shared_size,
                          uint64_t handoff_base, uint64_t l2_base, uint64_t rtkit_private_vm_base,
                          uint64_t wrapper, uint64_t chip_id, struct g18g_loader_expect *out);

/*
 * Qualify one loaded TEXT image. Returns G18G_OK or a negative g18g_result. When
 * bad_tlv is not NULL it receives the 4-character name of the loader TLV that
 * disagreed for G18G_BAD_LOADER_VALUE (NUL terminated, at least 5 bytes).
 */
int g18g_qualify_text(enum g18g_role role, const uint8_t *text, size_t size,
                      const struct g18g_loader_expect *want, char *bad_tlv);

const char *g18g_result_string(int result);

#endif
