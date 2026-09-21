/* SPDX-License-Identifier: MIT */
/*
 * J514S external display resource handoff, without device activation or MMIO.
 * Adapted from the J713 handoff; T6030 addresses and streams are from J514S ADT.
 *
 * Based on the ASC reservations by Martin Povišer (8be10f5fa94f), external
 * DCP iteration by Janne Grunau (c9ae791b799c), and J514S validation/reservation
 * work by Eryk Wieliczko (759676a651a4, 6fe56f90e336, 1797c626ae39).
 * See docs/display-m3-handoff.md for the complete attribution and ABI.
 */

#include <string.h>

#include "kboot.h"
#include "adt.h"
#include "firmware.h"
#include "malloc.h"
#include "utils.h"

#include "libfdt/libfdt.h"

#define EXT_MAX_SEGMENTS 16
#define EXT_MARKER       "apple,j514s-dcpext-reservations"
#define EXT_SEGMENTS     "apple,j514s-dcpext-segments"

struct ext_range {
    u64 base, size;
};

struct ext_controller {
    const char *alias;
    u64 coproc, dart, scanout_dart;
};

static const struct ext_controller controllers[] = {
    {"dcpext0", 0x2d2c00000, 0x2d130c000, 0x2d1304000},
    {"dcpext1", 0x2d6c00000, 0x2d530c000, 0x2d5304000},
};

static bool range_valid(struct ext_range r, struct ext_range dram)
{
    return r.size && !((r.base | r.size) & (SZ_16K - 1)) && r.base >= dram.base &&
           r.base + r.size > r.base && r.base + r.size <= dram.base + dram.size;
}

static bool overlap(struct ext_range a, struct ext_range b)
{
    return a.base < b.base + b.size && b.base < a.base + a.size;
}

static int read_exact(int node, const char *name, void *out, u32 size)
{
    u32 len = 0;
    const void *value = adt_getprop(adt, node, name, &len);
    if (!value || len != size)
        return -1;
    memcpy(out, value, size);
    return 0;
}

/* The J514S board describes direct physical addresses below its identity bus. */
static bool reg_matches(void *dt, int node, u64 base, u64 minimum)
{
    int len, parent = fdt_parent_offset(dt, node);
    const void *ranges = fdt_getprop(dt, parent, "ranges", &len);
    if (!ranges || len || fdt_parent_offset(dt, parent) != 0 ||
        fdt_address_cells(dt, parent) != 2 || fdt_size_cells(dt, parent) != 2)
        return false;
    const fdt64_t *reg = fdt_getprop(dt, node, "reg", &len);
    return reg && len >= 16 && !(len % 16) && fdt64_ld(reg) == base &&
           fdt64_ld(reg + 1) >= minimum && fdt64_ld(reg + 1) <= 0x88000;
}

static bool disabled(void *dt, int node)
{
    int len;
    const char *status = fdt_getprop(dt, node, "status", &len);
    return status && len == sizeof("disabled") && !memcmp(status, "disabled", len);
}

/* Optional future consumers must not activate on reservations-only metadata. */
static int check_consumers(void *dt, int node, const struct ext_controller *controller)
{
    int len;
    const fdt32_t *mbox = fdt_getprop(dt, node, "mboxes", &len);
    if (mbox) {
        if (len != 4)
            return -1;
        int mailbox = fdt_node_offset_by_phandle(dt, fdt32_ld(mbox));
        if (mailbox < 0 || !disabled(dt, mailbox))
            return -1;
    }
    char alias[32];
    const char *suffixes[] = {"", "_piodma"};
    for (u32 i = 0; i < ARRAY_SIZE(suffixes); i++) {
        snprintf(alias, sizeof(alias), "dispext%c%s",
                 controller->alias[strlen(controller->alias) - 1], suffixes[i]);
        int display = fdt_path_offset(dt, alias);
        if (display < 0)
            continue;
        const fdt32_t *iommu = fdt_getprop(dt, display, "iommus", &len);
        if (!disabled(dt, display) || !iommu || len != 8 || fdt32_ld(iommu + 1) != (i ? 4 : 0))
            return -1;
        int dart = fdt_node_offset_by_phandle(dt, fdt32_ld(iommu));
        if (dart < 0 || !disabled(dt, dart) ||
            !reg_matches(dt, dart, controller->scanout_dart, 0x4000))
            return -1;
    }
    return 0;
}

static int adt_resource(const char *path, u64 base)
{
    int trace[8];
    u64 address, size;
    int node = adt_path_offset_trace(adt, path, trace);
    if (node < 0 || adt_get_reg(adt, trace, "reg", 0, &address, &size) < 0 || address != base ||
        size < SZ_16K)
        return -1;
    return node;
}

/* Adapted from the covering-reservation check in kboot_gpu_m3.c. */
static int reserve_range(void *dt, struct ext_range range)
{
    int count = fdt_num_mem_rsv(dt);
    if (count < 0)
        return count;
    for (int i = 0; i < count; i++) {
        u64 base, size;
        if (fdt_get_mem_rsv(dt, i, &base, &size) || base + size < base)
            return -1;
        if (base <= range.base && base + size >= range.base + range.size)
            return 0;
    }
    return fdt_add_mem_rsv(dt, range.base, range.size);
}

/* Reuse shared TEXT reservations without modifying their existing IOMMU tuples. */
static int reserve_memory(void *dt, struct ext_range range, bool tables, u32 *phandle)
{
    int parent = fdt_path_offset(dt, "/reserved-memory"), node;
    if (parent < 0 || fdt_address_cells(dt, parent) != 2 || fdt_size_cells(dt, parent) != 2)
        return -1;
    int found = -1;
    fdt_for_each_subnode(node, dt, parent)
    {
        int len;
        const fdt64_t *reg = fdt_getprop(dt, node, "reg", &len);
        if (!reg)
            continue;
        if (len <= 0 || len % 16)
            return -1;
        for (int i = 0; i < len / 16; i++) {
            struct ext_range existing = {fdt64_ld(reg + i * 2), fdt64_ld(reg + i * 2 + 1)};
            /* ISP and GPU handoffs fill zero/zero DT placeholders after
             * display setup. These entries do not claim any RAM yet.
             */
            if (!existing.base && !existing.size)
                continue;
            if (!existing.size || existing.base + existing.size <= existing.base)
                return -1;
            if (!overlap(range, existing))
                continue;
            /* kboot_prepare_dt() protects a broad chainload interval before
             * display setup. Depending on the boot layout it also contains
             * inherited DCPEXT allocations. Keep that no-map reservation intact
             * and add the precise device reservation beneath its coverage.
             * It is not another device claiming these pages.
             */
            if (len == 16 && existing.base <= range.base &&
                existing.base + existing.size >= range.base + range.size &&
                !fdt_node_check_compatible(dt, node, "linux-enablement-mac,m1n1-chainload") &&
                fdt_getprop(dt, node, "no-map", NULL) && !disabled(dt, node))
                continue;
            if (len != 16 || existing.base != range.base || existing.size != range.size ||
                fdt_node_check_compatible(dt, node, tables ? "apple,dart-mem" : "apple,asc-mem") ||
                !fdt_getprop(dt, node, "no-map", NULL) || disabled(dt, node) || found >= 0)
                return -1;
            found = node;
        }
    }
    if (found < 0) {
        char name[64];
        snprintf(name, sizeof(name), "%s@%lx", tables ? "dcpext-pagetables" : "asc-firmware",
                 range.base);
        found = fdt_add_subnode(dt, parent, name);
        if (found < 0)
            return found;
        fdt64_t reg[] = {cpu_to_fdt64(range.base), cpu_to_fdt64(range.size)};
        if (fdt_setprop(dt, found, "reg", reg, sizeof(reg)) ||
            fdt_setprop_string(dt, found, "compatible",
                               tables ? "apple,dart-mem" : "apple,asc-mem") ||
            fdt_setprop_empty(dt, found, "no-map"))
            return -1;
    }
    *phandle = fdt_get_phandle(dt, found);
    if (!*phandle &&
        (fdt_generate_phandle(dt, phandle) || fdt_setprop_u32(dt, found, "phandle", *phandle)))
        return -1;
    return reserve_range(dt, range);
}

static int publish_controller(void *dt, const struct ext_controller *controller,
                              struct ext_range dram)
{
    int node = fdt_path_offset(dt, controller->alias), len;
    if (node < 0)
        return 0;
    const void *optin = fdt_getprop(dt, node, "apple,j514s-dcpext-reserve", &len);
    if (!optin)
        return 0;
    if (len || !disabled(dt, node) || fdt_node_check_compatible(dt, node, "apple,t6030-dcpext") ||
        !reg_matches(dt, node, controller->coproc, SZ_16K))
        return -1;
    const fdt32_t *iommu = fdt_getprop(dt, node, "iommus", &len);
    if (!iommu || len != 8 || fdt32_ld(iommu + 1) != 5)
        return -1;
    int dart_node = fdt_node_offset_by_phandle(dt, fdt32_ld(iommu));
    if (dart_node < 0 || !disabled(dt, dart_node) ||
        fdt_node_check_compatible(dt, dart_node, "apple,t6030-dart") ||
        !reg_matches(dt, dart_node, controller->dart, 0x4000) ||
        check_consumers(dt, node, controller))
        return -1;
    const fdt32_t *cells = fdt_getprop(dt, dart_node, "#iommu-cells", &len);
    if (!cells || len != 4 || fdt32_ld(cells) != 1)
        return -1;
    /* This provisional ABI owns this list. Do not discard another handoff's
     * memory references or leave mismatched memory-region-names behind.
     */
    if (fdt_getprop(dt, node, "memory-region-names", NULL))
        return -1;
    if (fdt_getprop(dt, node, "memory-region", NULL)) {
        const fdt32_t *version = fdt_getprop(dt, node, EXT_MARKER, &len);
        if (!version || len != 4 || fdt32_ld(version) != 1)
            return -1;
    }

    char path[64];
    snprintf(path, sizeof(path), "/arm-io/%s", controller->alias);
    if (adt_resource(path, controller->coproc) < 0)
        return -1;
    snprintf(path, sizeof(path), "/arm-io/%s/iop-%s-nub", controller->alias, controller->alias);
    int nub = adt_path_offset(adt, path);
    if (nub < 0)
        return -1;
    u32 bytes = 0;
    const struct adt_segment_ranges *segments = adt_getprop(adt, nub, "segment-ranges", &bytes);
    if (!segments || !bytes || bytes % sizeof(*segments) ||
        bytes / sizeof(*segments) > EXT_MAX_SEGMENTS)
        return -1;
    u32 count = bytes / sizeof(*segments), uuid_size = 0;
    const char *uuid = adt_getprop(adt, nub, "uuid", &uuid_size);
    if (!uuid || uuid_size != 37 || uuid[36])
        return -1;
    for (u32 i = 0; i < 36; i++) {
        bool hyphen = i == 8 || i == 13 || i == 18 || i == 23;
        if (hyphen ? uuid[i] != '-'
                   : !((uuid[i] >= '0' && uuid[i] <= '9') || (uuid[i] >= 'A' && uuid[i] <= 'F') ||
                       (uuid[i] >= 'a' && uuid[i] <= 'f')))
            return -1;
    }

    struct ext_range ranges[EXT_MAX_SEGMENTS + 3];
    fdt32_t encoded[EXT_MAX_SEGMENTS * 8];
    for (u32 i = 0; i < count; i++) {
        const struct adt_segment_ranges *s = &segments[i];
        ranges[i] = (struct ext_range){s->phys, ALIGN_UP((u64)s->size, SZ_16K)};
        if (!range_valid(ranges[i], dram) || s->remap & (SZ_16K - 1) ||
            s->remap + ranges[i].size < s->remap)
            return -1;
        /* Preserve the ADT descriptor as metadata, NOT as iommu-addresses. */
        fdt64_st((fdt64_t *)&encoded[i * 8], s->phys);
        fdt64_st((fdt64_t *)&encoded[i * 8 + 2], s->iova);
        fdt64_st((fdt64_t *)&encoded[i * 8 + 4], s->remap);
        encoded[i * 8 + 6] = cpu_to_fdt32(s->size);
        encoded[i * 8 + 7] = cpu_to_fdt32(s->unk);
    }

    /* ADT pt-region ranges describe table storage, not the contents of PTEs. */
    const u8 streams[] = {5, 0, 4};
    for (u32 i = 0; i < ARRAY_SIZE(streams); i++) {
        snprintf(path, sizeof(path), "/arm-io/dart-%s", controller->alias);
        if (i)
            snprintf(path, sizeof(path), "/arm-io/dart-dispext%c",
                     controller->alias[strlen(controller->alias) - 1]);
        int dart = adt_resource(path, i ? controller->scanout_dart : controller->dart);
        char property[24];
        u64 endpoints[2];
        snprintf(property, sizeof(property), "pt-region-%u", streams[i]);
        if (dart < 0 || read_exact(dart, property, endpoints, sizeof(endpoints)) ||
            endpoints[1] <= endpoints[0])
            return -1;
        ranges[count + i] = (struct ext_range){endpoints[0], endpoints[1] - endpoints[0]};
        if (!range_valid(ranges[count + i], dram))
            return -1;
    }
    for (u32 i = 0; i < count + 3; i++)
        for (u32 j = 0; j < i; j++)
            if (overlap(ranges[i], ranges[j]))
                return -1;

    u32 phandles[EXT_MAX_SEGMENTS + 3];
    for (u32 i = 0; i < count + 3; i++) {
        int ret = reserve_memory(dt, ranges[i], i >= count, &phandles[i]);
        if (ret) {
            printf("FDT: %s cannot reserve %lx+%lx (error %d)\n", controller->alias,
                   ranges[i].base, ranges[i].size, ret);
            return -1;
        }
        phandles[i] = cpu_to_fdt32(phandles[i]);
    }
    node = fdt_path_offset(dt, controller->alias);
    if (node < 0 || fdt_setprop(dt, node, "memory-region", phandles, (count + 3) * sizeof(u32)) ||
        fdt_setprop(dt, node, EXT_SEGMENTS, encoded, count * 32) ||
        fdt_setprop_string(dt, node, "apple,firmware-uuid", uuid))
        return -1;
    /* T6030 has no qualified older ABI alias: retain the actual firmware version. */
    if (firmware_set_fdt(dt, node, "apple,firmware-version", &os_firmware) ||
        firmware_set_fdt(dt, node, "apple,firmware-compat", &os_firmware) ||
        fdt_setprop_u32(dt, node, EXT_MARKER, 1))
        return -1;
    return 0;
}

int dt_set_display_m3_external(void *dt)
{
    bool requested = false;
    for (u32 i = 0; i < ARRAY_SIZE(controllers); i++) {
        int node = fdt_path_offset(dt, controllers[i].alias);
        if (node >= 0 && fdt_getprop(dt, node, "apple,j514s-dcpext-reserve", NULL))
            requested = true;
    }
    if (!requested)
        return 0; /* Existing DTs retain their internal-display-only handoff. */
    if (chip_id != T6030 || fdt_node_check_compatible(dt, 0, "apple,j514s") ||
        fdt_node_check_compatible(dt, 0, "apple,t6030"))
        return -1;
    int chosen = adt_path_offset(adt, "/chosen");
    struct ext_range dram;
    if (chosen < 0 || read_exact(chosen, "dram-base", &dram.base, sizeof(dram.base)) ||
        read_exact(chosen, "dram-size", &dram.size, sizeof(dram.size)) || !dram.size ||
        dram.base + dram.size <= dram.base)
        return -1;

    /* Commit both controllers together; a malformed input/FDT failure cannot
     * corrupt the existing internal handoff or leave half-published metadata.
     */
    int size = fdt_totalsize(dt), ret = -1;
    void *copy = malloc(size);
    if (!copy)
        return -1;
    if (fdt_open_into(dt, copy, size))
        goto out;
    for (u32 i = 0; i < ARRAY_SIZE(controllers); i++) {
        if (publish_controller(copy, &controllers[i], dram)) {
            printf("FDT: J514S %s reservation rejected; original DT retained\n",
                   controllers[i].alias);
            goto out;
        }
    }
    memcpy(dt, copy, size);
    printf("FDT: J514S external display memory reserved; controllers remain disabled\n");
    ret = 0;
out:
    free(copy);
    return ret;
}
