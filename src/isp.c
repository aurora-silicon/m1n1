/* SPDX-License-Identifier: MIT */

#include "adt.h"
#include "dapf.h"
#include "dart.h"
#include "firmware.h"
#include "isp.h"
#include "pmgr.h"
#include "soc.h"
#include "utils.h"
#include "xnuboot.h"

#define ISP_ASC_VERSION 0x1800000

#define ISP_VER_T8103 0xb0090
#define ISP_VER_T6000 0xb3091
#define ISP_VER_T8112 0xc1090
#define ISP_VER_T6020 0xc3091
#define ISP_VER_T8122 0xf1001
#define ISP_VER_T603X 0xf3001
#define ISP_VER_T8140 0x110000

#define ISP_PMGR_T8140_CPU   0x4008
#define ISP_PMGR_T8140_CORE0 0x4010
#define ISP_PMGR_T8140_CORE1 0x4018
#define ISP_PMGR_T8140_FE    0x4020

// Candidate from the ISP-2 measurement plan; release requires its A/B receipts.
#define ISP_T8140_HEAP_TOP 0x2200000
#define ISP_T8140_SEG_END  0x21ec000

// PMGR offset to enable to get the version info to work
#define ISP_PMGR_T8103 0x4018
#define ISP_PMGR_T6000 0x8
#define ISP_PMGR_T6020 0x4008
#define ISP_PMGR_T6031 0x4030

static bool isp_initialized = false;
static u64 heap_phys, heap_iova, heap_size, heap_top;

int isp_get_heap(u64 *phys, u64 *iova, u64 *size)
{
    if (!isp_initialized)
        return -1;

    *phys = heap_phys;
    *iova = heap_iova | isp_iova_base();
    *size = heap_size;
    return 0;
}

u64 isp_iova_base(void)
{
    switch (chip_id) {
        case 0x6020 ... 0x6fff:
        case 0x8122:
            return 0x10000000000;
        default:
            return 0;
    }
}

int isp_init(void)
{
    int err = -1;
    bool t8140 = chip_id == T8140;
    bool powered = false;
    unsigned int local_powered = 0;
    u32 ver_rev = 0;
    u64 selected_top = 0;
    u64 selected_iova = 0;
    u64 selected_size = 0;
    u64 selected_phys = 0;
    const char *reason = "initialization failed";

    const char *isp_path = "/arm-io/isp";
    const char *dart_path = "/arm-io/dart-isp";

    int adt_path[8], adt_isp_path[8];
    int isp_node = adt_path_offset_trace(adt, isp_path, adt_isp_path);
    int node = adt_path_offset_trace(adt, dart_path, adt_path);
    if (node < 0 || isp_node < 0) {
        isp_path = "/arm-io/isp0";
        dart_path = "/arm-io/dart-isp0";
        isp_node = adt_path_offset_trace(adt, isp_path, adt_isp_path);
        node = adt_path_offset_trace(adt, dart_path, adt_path);
    }
    isp_initialized = false;
    if (node < 0 || isp_node < 0)
        return 0;

    powered = true;
    if (pmgr_adt_power_enable(isp_path) < 0)
        goto out;

    u64 isp_base;
    u64 pmgr_base;
    err = adt_get_reg(adt, adt_isp_path, "reg", 0, &isp_base, NULL);
    if (err)
        goto out;

    err = adt_get_reg(adt, adt_isp_path, "reg", 1, &pmgr_base, NULL);
    if (err)
        goto out;
    err = -1;

    if (t8140) {
        const u32 offsets[] = {ISP_PMGR_T8140_CPU, ISP_PMGR_T8140_CORE0, ISP_PMGR_T8140_CORE1,
                               ISP_PMGR_T8140_FE};
        for (unsigned int i = 0; i < ARRAY_SIZE(offsets); i++) {
            reason = "local PMGR power failed";
            local_powered++;
            if (pmgr_set_mode(pmgr_base + offsets[i], PMGR_PS_ACTIVE))
                goto out;
        }
    }

    u32 pmgr_off;
    switch (chip_id) {
        case T8103:
        case T8112:
        case T8122:
            pmgr_off = ISP_PMGR_T8103;
            break;
        case T6000 ... T6002:
            pmgr_off = ISP_PMGR_T6000;
            break;
        case T6020 ... T6022:
        case T6030:
            pmgr_off = ISP_PMGR_T6020;
            break;
        case T6031 ... T6034:
            pmgr_off = ISP_PMGR_T6031;
            break;
        case T8140:
            pmgr_off = ISP_PMGR_T8140_CPU;
            break;
        default:
            reason = "unsupported SoC";
            goto out;
    }

    if (!t8140) {
        reason = "local PMGR power failed";
        local_powered = 1;
        if (pmgr_set_mode(pmgr_base + pmgr_off, PMGR_PS_ACTIVE))
            goto out;
    }

    ver_rev = read32(isp_base + ISP_ASC_VERSION);
    printf("isp: Version 0x%x\n", ver_rev);
    if (t8140 && ver_rev != ISP_VER_T8140) {
        reason = "unexpected T8140 revision";
        goto out;
    }

    /* TODO: confirm versions */
    switch (ver_rev) {
        case ISP_VER_T8103:
        case ISP_VER_T8112:
            switch (os_firmware.version) {
                case V12_3 ... V12_4:
                    selected_top = 0x1800000;
                    break;
                case V13_5:
                    selected_top = 0x1000000;
                    break;
                default:
                    reason = "unsupported firmware";
                    goto out;
            }
            break;
        case ISP_VER_T6000:
            switch (os_firmware.version) {
                case V12_3:
                    selected_top = 0xe00000;
                    break;
                case V13_5:
                case V13_6_1:
                    selected_top = 0xf00000;
                    break;
                default:
                    reason = "unsupported firmware";
                    goto out;
            }
            break;
        case ISP_VER_T6020:
            switch (os_firmware.version) {
                case V13_5:
                case V13_6_1:
                    selected_top = 0xf00000;
                    break;
                default:
                    reason = "unsupported firmware";
                    goto out;
            }
            break;
        case ISP_VER_T603X:
        case ISP_VER_T8122:
            switch (os_firmware.version) {
                case V14_7:
                    selected_top = 0x1000000;
                    break;
                default:
                    reason = "unsupported firmware";
                    goto out;
            }
            break;
        case ISP_VER_T8140:
            if (!t8140 || os_firmware.version != V26_6_2) {
                reason = "unsupported T8140 firmware or SoC";
                goto out;
            }
            selected_top = ISP_T8140_HEAP_TOP;
            break;
        default:
            reason = "unknown revision";
            goto out;
    }

    const struct adt_segment_ranges *seg;
    u32 segments_len = 0;

    seg = adt_getprop(adt, isp_node, "segment-ranges", &segments_len);
    reason = "invalid segment-ranges length";
    if (!seg || !segments_len || segments_len % sizeof(*seg))
        goto out;
    unsigned int count = segments_len / sizeof(*seg);

    reason = "segment end overflow";
    if (seg[count - 1].iova > UINT64_MAX - seg[count - 1].size)
        goto out;
    u64 end = seg[count - 1].iova + seg[count - 1].size;
    reason = "segment alignment overflow";
    if (end > UINT64_MAX - (SZ_16K - 1))
        goto out;
    selected_iova = ALIGN_UP(end, SZ_16K);
    reason = "unmeasured T8140 segment end";
    if (t8140 && selected_iova != ISP_T8140_SEG_END)
        goto out;
    reason = "heap top at or below segment end";
    if (selected_iova >= selected_top)
        goto out;
    selected_size = selected_top - selected_iova;
    if (t8140) {
        reason = "ISP DAPF initialization failed";
        if (dapf_init(dart_path, 5) < 0)
            goto out;
    }
    reason = "heap allocation failed";
    if (selected_size > SIZE_MAX - (SZ_16K - 1) || cur_boot_args.mem_size <= SZ_16K ||
        ALIGN_UP(selected_size, SZ_16K) > cur_boot_args.mem_size - SZ_16K)
        goto out;
    selected_phys = top_of_memory_alloc(selected_size);
    if (!selected_phys)
        goto out;

    printf("isp: Code: 0x%lx..0x%lx (0x%x @ 0x%lx)\n", seg[0].iova, seg[0].iova + seg[0].size,
           seg[0].size, seg[0].phys);
    if (count > 1)
        printf("isp: Data: 0x%lx..0x%lx (0x%x @ 0x%lx)\n", seg[1].iova, seg[1].iova + seg[1].size,
               seg[1].size, seg[1].phys);
    printf("isp: Heap: 0x%lx..0x%lx (0x%lx @ 0x%lx)\n", selected_iova, selected_top, selected_size,
           selected_phys);

    heap_iova = selected_iova;
    heap_top = selected_top;
    heap_size = selected_size;
    heap_phys = selected_phys;
    isp_initialized = true;
    if (t8140)
        return 0; // Resident H17 ISP stays powered for the Linux handoff.
    err = 0;

out:
    if (err)
        printf("isp: disabled: revision 0x%x firmware %s aligned end 0x%lx: %s\n", ver_rev,
               os_firmware.string ? os_firmware.string : "unknown", selected_iova, reason);
    if (local_powered) {
        if (t8140) {
            const u32 offsets[] = {ISP_PMGR_T8140_CPU, ISP_PMGR_T8140_CORE0, ISP_PMGR_T8140_CORE1,
                                   ISP_PMGR_T8140_FE};
            while (local_powered)
                pmgr_set_mode(pmgr_base + offsets[--local_powered], PMGR_PS_PWRGATE);
        } else {
            pmgr_set_mode(pmgr_base + pmgr_off, PMGR_PS_PWRGATE);
        }
    }
    if (powered)
        pmgr_adt_power_disable(isp_path);
    return err;
}
