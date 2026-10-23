/* SPDX-License-Identifier: MIT */

#include "pmu_counter.h"
#include "adt.h"
#include "soc.h"
#include "spmi.h"
#include "string.h"
#include "utils.h"

/*
 * iBoot counts failed boots in the primary PMU's legacy scratchpad (error
 * count byte at info-leg_scrpad + 2) and drops to recoveryOS once it is too
 * high. macOS clears it on a good boot; an m1n1 chain that never reaches an
 * OS which does so must clear it itself, or repeated test boots end in the
 * recovery error screen.
 */
int pmu_clear_panic_counter(void)
{
    int arm_io = adt_path_offset(adt, "/arm-io");
    if (arm_io < 0)
        return -1;

    int bnode = arm_io;
    ADT_FOREACH_CHILD(adt, bnode)
    {
        const char *bus = adt_get_name(adt, bnode);
        if (strncmp(bus, "nub-spmi", 8) && strncmp(bus, "spmi", 4))
            continue;

        int node = bnode;
        ADT_FOREACH_CHILD(adt, node)
        {
            u32 primary = 0, scrpad, reg[1];
            if (!adt_is_compatible(adt, node, "pmu,spmi") ||
                ADT_GETPROP(adt, node, "is-primary", &primary) < 0 || primary != 1)
                continue;
            if (ADT_GETPROP(adt, node, "info-leg_scrpad", &scrpad) < 0 ||
                ADT_GETPROP_ARRAY(adt, node, "reg", reg) < 0) {
                printf("pmu: %s/%s lacks leg_scrpad or reg\n", bus, adt_get_name(adt, node));
                return -1;
            }

            char path[64];
            snprintf(path, sizeof(path), "/arm-io/%s", bus);
            spmi_dev_t *spmi = spmi_init(path);
            if (!spmi)
                return -1;

            u8 addr = reg[0] & 0xff;
            u16 counter = scrpad + 2;
            u8 val = 0, zero = 0;
            int ret = spmi_ext_read_long(spmi, addr, counter, &val, 1);
            if (!ret && val)
                ret = spmi_ext_write_long(spmi, addr, counter, &zero, 1);
            spmi_shutdown(spmi);
            if (ret) {
                printf("pmu: failed to access the panic counter at %s:%02x reg %x\n", path, addr,
                       counter);
                return -1;
            }
            printf("pmu: panic counter at %s:%02x reg %x was %u%s\n", path, addr, counter, val,
                   val ? ", cleared" : "");
            return 0;
        }
    }
    printf("pmu: no primary SPMI PMU found\n");
    return -1;
}
