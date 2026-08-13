/* SPDX-License-Identifier: MIT */

#include "hv_aic_alias.h"
#include "soc.h"
#include "utils.h"

struct hv_aic_alias {
    u32 published;
    u32 physical;
};

/*
 * J813 (T8142) only.
 *
 * dockchannel-mtp carries the built-in keyboard and trackpad.  Its ADT
 * `interrupts` property is <1277>, read back from the live device tree on this
 * machine, and 1277 is not a legal Windows GSIV -- see hv_aic_alias.h.
 *
 * 995 is free.  Walking every ADT node that carries an `interrupts` property
 * gives 249 distinct lines in 32..1019, and none of them is 995; the whole
 * 990..999 block is unused.  That has to be checked rather than assumed,
 * because injection is otherwise the identity: a published number that is also
 * a live physical line would deliver some other device's interrupts to the
 * guest under this device's INTID.  J414s publishes 37..46 for its aliases,
 * but 37 is taken on this SoC, so those numbers are not portable here.
 *
 * m1n1's own reserved timer software IRQs sit at aic->nr_irq - 2 * MAX_CPUS,
 * i.e. the very top of AIC space, so they cannot collide with a low published
 * number either.
 *
 * ans is the internal NVMe storage controller, and the same problem: its line
 * is `interrupts`[`nvme-interrupt-idx`] = interrupts[4] = 1155, also above the
 * arbiter's ceiling.  996 was picked by the same walk, re-run on this machine
 * for the ans work: 443 distinct lines appear in ADT `interrupts` properties,
 * 249 of them inside [32,1024), and 1155 is claimed by /arm-io/ans alone.  Of
 * the 980..999 block only 984 is taken, so 996 is free and sits next to the
 * mtp alias where both stay reviewable.  scratchpad adt_free_gsiv.py re-runs
 * the check; it is RAM-only, because a stray MMIO read latches an L2C error on
 * this SoC.
 */
static const struct hv_aic_alias hv_aic_aliases_t8142[] = {
    {995, 1277}, /* dockchannel-mtp: keyboard + trackpad */
    {996, 1155}, /* ans: internal NVMe storage */
};

static const struct hv_aic_alias *hv_aic_alias_table(u32 *count)
{
    /*
     * Runtime-gated, not compile-time: the same m1n1 binary is chainloaded
     * onto other targets, and an alias is only ever correct for the SoC whose
     * device tree it was read from.
     */
    if (chip_id == T8142) {
        *count = ARRAY_SIZE(hv_aic_aliases_t8142);
        return hv_aic_aliases_t8142;
    }

    *count = 0;
    return NULL;
}

u32 hv_aic_alias_to_physical(u32 published)
{
    u32 count;
    const struct hv_aic_alias *table = hv_aic_alias_table(&count);

    for (u32 i = 0; i < count; i++) {
        if (table[i].published == published)
            return table[i].physical;
    }

    return published;
}

bool hv_aic_alias_is_owned(u32 published)
{
    u32 count;
    const struct hv_aic_alias *table = hv_aic_alias_table(&count);

    for (u32 i = 0; i < count; i++) {
        if (table[i].published == published)
            return true;
    }

    return false;
}

u32 hv_aic_alias_to_published(u32 physical)
{
    u32 count;
    const struct hv_aic_alias *table = hv_aic_alias_table(&count);

    for (u32 i = 0; i < count; i++) {
        if (table[i].physical == physical)
            return table[i].published;
    }

    return physical;
}
