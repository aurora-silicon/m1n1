/* SPDX-License-Identifier: MIT */
#include <assert.h>
#include <string.h>

#define TARGET T8152
#include "soc.h"
#include "platform_identity.h"

_Static_assert(EARLY_UART_BASE == 0x331200000ULL, "J873g translated UART address");

int main(void)
{
    static const char target[] = "J873g";
    static const char model[] = "Mac18,5";
    static const char compatible[] = "J873gAP\0Mac18,5\0AppleARM";
    const struct platform_identity expected = {
        .chip_id = 0x8152,
        .board_id = 0x24,
        .root_target_type = target,
        .root_target_type_len = sizeof(target),
        .model = model,
        .model_len = sizeof(model),
        .compatible = compatible,
        .compatible_len = sizeof(compatible),
    };
    struct platform_identity changed = expected;
    assert(platform_identity_matches_j873g(&expected));
    assert(!platform_identity_matches_j873g(NULL));
    assert(!platform_identity_matches_j813(&expected));
    assert(!platform_identity_matches_j414s(&expected));

    changed.chip_id = 0x6050; /* The adjacent J873s firmware is not M6. */
    assert(!platform_identity_matches_j873g(&changed));
    changed = expected;
    changed.board_id = 0x02;
    assert(!platform_identity_matches_j873g(&changed));
    changed = expected;
    changed.root_target_type = "J873s";
    assert(!platform_identity_matches_j873g(&changed));
    changed = expected;
    changed.model = "Mac17,3";
    changed.model_len = sizeof("Mac17,3");
    assert(!platform_identity_matches_j873g(&changed));
    changed = expected;
    changed.model = NULL;
    assert(!platform_identity_matches_j873g(&changed));

    /* Every truncation and every changed byte must fail the exact identity. */
    for (u32 i = 0; i < sizeof(compatible); i++) {
        char altered[sizeof(compatible)];
        memcpy(altered, compatible, sizeof(altered));
        altered[i] ^= 1;
        changed = expected;
        changed.compatible = altered;
        assert(!platform_identity_matches_j873g(&changed));
        changed = expected;
        changed.compatible_len = i;
        assert(!platform_identity_matches_j873g(&changed));
    }
    return 0;
}
