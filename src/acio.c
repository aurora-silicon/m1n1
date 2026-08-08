/* SPDX-License-Identifier: MIT */

#include "acio.h"
#include "adt.h"
#include "assert.h"
#include "string.h"
#include "tunables.h"
#include "utils.h"

#define ACIO_COUNT 3
#define ACIO_TUNABLE_RECORD_SIZE 24
#define ACIO_ADT_MAX_DEPTH 8

struct acio_segment_range {
    u64 phys;
    u64 iop_va;
    u64 remap;
    u32 size;
    u32 flags;
} PACKED;
static_assert(sizeof(struct acio_segment_range) == 32, "invalid ACIO segment range");

static int acio_get_reg(const char *path, u32 index, u64 *base, u64 *size)
{
    int trace[8];
    if (adt_path_offset_trace(adt, path, trace) < 0 ||
        adt_get_reg(adt, trace, "reg", index, base, size) < 0)
        return -1;
    return 0;
}

static int acio_require_tunable(int node, const char *name)
{
    u32 size = 0;
    const void *data = adt_getprop(adt, node, name, &size);
    if (!data || !size || size % ACIO_TUNABLE_RECORD_SIZE) {
        printf("acio: missing or malformed %s (%u bytes)\n", name, size);
        return -1;
    }
    return 0;
}

static int acio_apply_one_tunable(const char *path, const char *name, u64 base,
                                  u32 window_size)
{
    int node = adt_path_offset(adt, path);
    u32 size = 0;
    const void *data = node < 0 ? NULL : adt_getprop(adt, node, name, &size);
    if (acio_type5_tunables_validate(data, size, window_size) < 0) {
        printf("acio: refusing malformed or out-of-range %s\n", name);
        return -1;
    }
    return tunables_apply_local_addr(path, name, base);
}

static int acio_find_phandle(int parent, u32 phandle, int depth)
{
    if (depth >= ACIO_ADT_MAX_DEPTH)
        return -1;

    int child = parent;
    ADT_FOREACH_CHILD(adt, child)
    {
        u32 value;
        if (ADT_GETPROP(adt, child, "AAPL,phandle", &value) >= 0 && value == phandle)
            return child;

        int found = acio_find_phandle(child, phandle, depth + 1);
        if (found >= 0)
            return found;
    }
    return -1;
}

static int acio_discover_dart_sids(int acio_node, acio_resources_t *resources)
{
    u32 size = 0;
    const u32 *parents = adt_getprop(adt, acio_node, "iommu-parent", &size);
    if (!parents || size % sizeof(*parents))
        return -1;

    size_t count = size / sizeof(*parents);
    enum acio_sid_partition_mode mode;
    u8 ignored_slot;
    if (acio_type5_mapper_slot(count, ACIO_TYPE5_RING_TX, 0, &mode, &ignored_slot) < 0)
        return -1;

    for (size_t i = 0; i < count; i++) {
        int mapper = acio_find_phandle(0, parents[i], 0);
        u32 sid;
        if (mapper < 0 || !adt_is_compatible(adt, mapper, "iommu-mapper") ||
            ADT_GETPROP(adt, mapper, "reg", &sid) < 0 || sid > 0xff)
            return -1;
        resources->dart_sids[i] = (u8)sid;
    }

    resources->sid_partition_mode = mode;
    resources->dart_sid_count = (u8)count;
    return 0;
}

int acio_discover_resources(u32 index, acio_resources_t *resources)
{
    static const char *const required_tunables[] = {
        "top_tunables", "hbw_fabric_tunables", "lbw_fabric_tunables",
        "hi_up_tx_desc_fabric_tunables", "hi_up_tx_data_fabric_tunables",
        "hi_up_rx_desc_fabric_tunables", "hi_up_wr_fabric_tunables",
        "hi_up_merge_fabric_tunables", "hi_dn_merge_fabric_tunables",
        "fw_int_ctl_management_tunables", "pcie_adapter_regs_tunables",
    };
    char acio_path[32];
    char dart_path[32];
    char cpu_path[48];
    char nub_path[64];
    acio_resources_t out = {0};

    if (!resources || index >= ACIO_COUNT)
        return -1;
    snprintf(acio_path, sizeof(acio_path), "/arm-io/acio%u", index);
    snprintf(dart_path, sizeof(dart_path), "/arm-io/dart-acio%u", index);
    snprintf(cpu_path, sizeof(cpu_path), "/arm-io/acio-cpu%u", index);
    snprintf(nub_path, sizeof(nub_path), "/arm-io/acio-cpu%u/iop-acio%u-nub",
             index, index);

    int acio_node = adt_path_offset(adt, acio_path);
    int nub_node = adt_path_offset(adt, nub_path);
    if (acio_node < 0 || nub_node < 0 ||
        !adt_is_compatible(adt, acio_node, "acio") ||
        !adt_is_compatible(adt, nub_node, "iop-nub,rtbuddy-v2"))
        return -1;

    u32 rid = ~0U;
    if (ADT_GETPROP(adt, acio_node, "rid", &rid) < 0 || rid != index ||
        ADT_GETPROP(adt, acio_node, "port-number", &out.port_number) < 0 ||
        out.port_number != index + 1)
        return -1;

    u32 irq_size = 0;
    const u32 *irqs = adt_getprop(adt, acio_node, "interrupts", &irq_size);
    if (!irqs || irq_size != sizeof(out.irqs))
        return -1;
    memcpy(out.irqs, irqs, sizeof(out.irqs));

    u32 gates_size = 0;
    if (!adt_getprop(adt, acio_node, "power-gates", &gates_size) ||
        gates_size != 3 * sizeof(u32) ||
        !adt_getprop(adt, acio_node, "clock-gates", &gates_size) ||
        gates_size != 4 * sizeof(u32))
        return -1;

    if (acio_get_reg(acio_path, 0, &out.nhi_base, &out.nhi_size) < 0 ||
        out.nhi_size < 0x100000 ||
        acio_get_reg(acio_path, 1, &out.pdf_base, &out.pdf_size) < 0 ||
        out.pdf_size < ACIO_NHI_RING_COUNT * ACIO_TYPE5_RING_STRIDE + sizeof(u32) ||
        acio_get_reg(acio_path, 2, &out.rc_base, &out.rc_size) < 0 ||
        out.rc_size < 0x4000 ||
        acio_get_reg(acio_path, 3, &out.hbw_base, &out.hbw_size) < 0 ||
        out.hbw_size < 0x4000 ||
        acio_get_reg(acio_path, 4, &out.lbw_base, &out.lbw_size) < 0 ||
        out.lbw_size < 0x4000 ||
        acio_get_reg(acio_path, 5, &out.pcie_adapter_base,
                     &out.pcie_adapter_size) < 0 ||
        out.pcie_adapter_size < 0x4000 ||
        acio_get_reg(dart_path, 0, &out.dart_base, &out.dart_size) < 0 ||
        out.dart_size < 0x4000 ||
        acio_get_reg(cpu_path, 0, &out.asc_cpu_base, NULL) < 0 ||
        acio_get_reg(cpu_path, 1, &out.asc_mailbox_base, NULL) < 0)
        return -1;

    for (size_t i = 0; i < sizeof(required_tunables) / sizeof(*required_tunables); i++)
        if (acio_require_tunable(acio_node, required_tunables[i]) < 0)
            return -1;
    u32 drom_size = 0;
    if (!adt_getprop(adt, acio_node, "thunderbolt-drom", &drom_size) || !drom_size)
        return -1;

    u32 segments_size = 0;
    const struct acio_segment_range *segments =
        adt_getprop(adt, nub_node, "segment-ranges", &segments_size);
    if (!segments || !segments_size || segments_size % sizeof(*segments))
        return -1;
    for (u32 i = 0; i < segments_size / sizeof(*segments); i++) {
        if (segments[i].iop_va != ACIO_SRAM_IOVA_BASE)
            continue;
        if (!segments[i].size || segments[i].remap != segments[i].iop_va)
            return -1;
        out.sram_iova_base = segments[i].iop_va;
        out.sram_phys_base = segments[i].phys;
        out.sram_size = segments[i].size;
        break;
    }
    if (!out.sram_size)
        return -1;

    /* iBoot describes the normal Type5 boot contract explicitly.  When both
     * flags are present, the ACIO firmware is already resident and the IOP is
     * live.  Its SRAM aperture is not AP-writable in that state; attempting
     * Apple's reset/manual-loader memcpy path against it raises an SError. */
    out.firmware_preloaded_running =
        adt_getprop(adt, nub_node, "pre-loaded", NULL) &&
        adt_getprop(adt, nub_node, "running", NULL);

    /* Host-router adapters come from the ADT, never from a config scan.
     * adapter N type = portmap[N-1]; the adapter count is the portmap
     * length; port-defaults is a parallel array of packed hop/buffer
     * limits.  loadPortMap's own error contract rejects a portmap whose
     * length is not a multiple of 4, so do the same. */
    u32 portmap_size = 0;
    u32 defaults_size = 0;
    const u32 *portmap = adt_getprop(adt, acio_node, "portmap", &portmap_size);
    const u32 *defaults = adt_getprop(adt, acio_node, "port-defaults", &defaults_size);
    if (!portmap || !portmap_size || portmap_size % sizeof(u32) ||
        portmap_size / sizeof(u32) > ACIO_TYPE5_MAX_ADAPTERS) {
        printf("acio%u: missing or malformed portmap (%u bytes)\n", index, portmap_size);
        return -1;
    }
    if (!defaults || defaults_size != portmap_size) {
        printf("acio%u: port-defaults must parallel portmap (%u vs %u bytes)\n",
               index, defaults_size, portmap_size);
        return -1;
    }
    out.adapter_count = (u8)(portmap_size / sizeof(u32));
    for (u32 i = 0; i < out.adapter_count; i++) {
        if (acio_type5_adapter_decode(portmap[i], &defaults[i], &out.adapters[i]) < 0) {
            printf("acio%u: adapter %u has unknown type %#x\n", index, i + 1, portmap[i]);
            return -1;
        }
    }

    if (acio_discover_dart_sids(acio_node, &out) < 0)
        return -1;

    out.index = index;
    *resources = out;
    return 0;
}

int acio_apply_tunables(const acio_resources_t *resources)
{
    static const struct {
        const char *name;
        u32 offset;
    } nhi[] = {
        {"hi_up_tx_desc_fabric_tunables", 0xe8000},
        {"hi_up_tx_data_fabric_tunables", 0xec000},
        {"hi_up_rx_desc_fabric_tunables", 0xf0000},
        {"hi_up_wr_fabric_tunables", 0xf4000},
        {"hi_up_merge_fabric_tunables", 0xf8000},
        {"hi_dn_merge_fabric_tunables", 0xfc000},
        {"fw_int_ctl_management_tunables", 0x04000},
    };
    char path[32];

    if (!resources || resources->index >= ACIO_COUNT)
        return -1;
    snprintf(path, sizeof(path), "/arm-io/acio%u", resources->index);

    if (acio_apply_one_tunable(path, "top_tunables", resources->rc_base, 0x4000) < 0 ||
        acio_apply_one_tunable(path, "hbw_fabric_tunables", resources->hbw_base, 0x4000) < 0 ||
        acio_apply_one_tunable(path, "lbw_fabric_tunables", resources->lbw_base, 0x4000) < 0)
        return -1;

    for (size_t i = 0; i < sizeof(nhi) / sizeof(*nhi); i++) {
        if ((u64)nhi[i].offset + 0x4000 > resources->nhi_size ||
            acio_apply_one_tunable(path, nhi[i].name,
                                   resources->nhi_base + nhi[i].offset,
                                   0x4000) < 0)
            return -1;
    }
    return 0;
}

int acio_apply_pcie_adapter_tunables(const acio_resources_t *resources)
{
    char path[32];
    if (!resources || resources->index >= ACIO_COUNT)
        return -1;
    snprintf(path, sizeof(path), "/arm-io/acio%u", resources->index);
    return acio_apply_one_tunable(path, "pcie_adapter_regs_tunables",
                                  resources->pcie_adapter_base, 0x4000);
}


int acio_resources_find_adapter(const acio_resources_t *resources,
                                enum acio_type5_adapter_kind kind, bool up)
{
    if (!resources)
        return -1;
    for (u32 i = 0; i < resources->adapter_count; i++) {
        if (resources->adapters[i].kind == kind && resources->adapters[i].up == up)
            return (int)(i + 1); /* adapters are numbered 1..count; 0 is the router */
    }
    return -1;
}
