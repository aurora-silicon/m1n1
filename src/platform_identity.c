/* SPDX-License-Identifier: MIT */

#include "platform_identity.h"
#include "soc.h"
#include "string.h"

#ifndef PLATFORM_IDENTITY_HOST_TEST
#include "adt.h"
#include "utils.h"
#endif

static const char j414s_root_target_type[] = "J414s";
static const char j414s_model[] = "Mac14,9";
static const char j414s_compatible[] = "J414sAP\0Mac14,9\0AppleARM";

/*
 * J813 is the M5 MacBook Air.  Unlike the J414s check below this one does not
 * look at board_id: the MTP addresses it gates were read from this machine's
 * live ADT, and the root identity strings already pin the board.
 */
static const char j813_root_target_type[] = "J813";
static const char j813_model[] = "Mac17,3";
static const char j813_compatible[] = "J813AP\0Mac17,3\0AppleARM";

/* 26A428 J873g ADT and root BuildManifest agree on these identifiers. */
static const char j873g_root_target_type[] = "J873g";
static const char j873g_model[] = "Mac18,5";
static const char j873g_compatible[] = "J873gAP\0Mac18,5\0AppleARM";

static bool property_equals(const void *property, u32 property_len, const void *expected,
                            size_t expected_len)
{
    return property && property_len == expected_len && !memcmp(property, expected, expected_len);
}

bool platform_identity_matches_j414s(const struct platform_identity *identity)
{
    if (!identity)
        return false;

    /* The measured J414s /chosen node has no target-type payload. */
    if (identity->chosen_target_type_len != 0)
        return false;

    return identity->chip_id == T6020 && identity->board_id == 4 &&
           property_equals(identity->root_target_type, identity->root_target_type_len,
                           j414s_root_target_type, sizeof(j414s_root_target_type)) &&
           property_equals(identity->model, identity->model_len, j414s_model,
                           sizeof(j414s_model)) &&
           property_equals(identity->compatible, identity->compatible_len, j414s_compatible,
                           sizeof(j414s_compatible));
}

bool platform_identity_matches_j813(const struct platform_identity *identity)
{
    if (!identity)
        return false;

    return identity->chip_id == T8142 &&
           property_equals(identity->root_target_type, identity->root_target_type_len,
                           j813_root_target_type, sizeof(j813_root_target_type)) &&
           property_equals(identity->model, identity->model_len, j813_model,
                           sizeof(j813_model)) &&
           property_equals(identity->compatible, identity->compatible_len, j813_compatible,
                           sizeof(j813_compatible));
}

bool platform_identity_matches_j873g(const struct platform_identity *identity)
{
    if (!identity)
        return false;

    return identity->chip_id == T8152 && identity->board_id == 0x24 &&
           property_equals(identity->root_target_type, identity->root_target_type_len,
                           j873g_root_target_type, sizeof(j873g_root_target_type)) &&
           property_equals(identity->model, identity->model_len, j873g_model,
                           sizeof(j873g_model)) &&
           property_equals(identity->compatible, identity->compatible_len, j873g_compatible,
                           sizeof(j873g_compatible));
}

#ifndef PLATFORM_IDENTITY_HOST_TEST
static void platform_identity_read(struct platform_identity *identity)
{
    int chosen = adt_path_offset(adt, "/chosen");

    identity->chip_id = chip_id;
    identity->board_id = board_id;

    if (chosen >= 0)
        identity->chosen_target_type =
            adt_getprop(adt, chosen, "target-type", &identity->chosen_target_type_len);

    identity->root_target_type =
        adt_getprop(adt, 0, "target-type", &identity->root_target_type_len);
    identity->model = adt_getprop(adt, 0, "model", &identity->model_len);
    identity->compatible = adt_getprop(adt, 0, "compatible", &identity->compatible_len);
}

bool platform_is_j414s(void)
{
    struct platform_identity identity = {};

    if (adt_path_offset(adt, "/chosen") < 0)
        return false;

    platform_identity_read(&identity);
    return platform_identity_matches_j414s(&identity);
}

bool platform_is_j813(void)
{
    struct platform_identity identity = {};

    platform_identity_read(&identity);
    return platform_identity_matches_j813(&identity);
}

bool platform_is_j873g(void)
{
    struct platform_identity identity = {};

    platform_identity_read(&identity);
    return platform_identity_matches_j873g(&identity);
}
#endif
