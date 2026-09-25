/* SPDX-License-Identifier: MIT */

#include "kboot.h"
#include "adt.h"
#include "assert.h"
#include "firmware.h"
#include "malloc.h"
#include "math.h"
#include "pmgr.h"
#include "soc.h"
#include "utils.h"

#include "libfdt/libfdt.h"

#define bail(...)                                                                                  \
    do {                                                                                           \
        printf(__VA_ARGS__);                                                                       \
        return -1;                                                                                 \
    } while (0)

#define MAX_PSTATES  16
#define MAX_CLUSTERS 8
#define MAX_DIES     2

struct perf_state {
    u32 freq;
    u32 volt;
};

struct aux_perf_state {
    u64 volt;
    u64 freq;
};

struct aux_perf_states {
    u64 dies;
    u64 count;
    struct aux_perf_state states[];
};

int32_t rust_gpu_initdata_size(uint32_t compat_maj, uint32_t compat_min, size_t *data_a,
                               size_t *data_b, size_t *globals);

struct initdata_inputs {
    size_t perf_state_table_count;
    size_t perf_state_count;
    const struct perf_state *c_perf_states;
    uint32_t *max_pwr;
    float *core_leak;
    float *sram_leak;
    float *cs_leak;
    float *afr_leak;
    size_t n_perf_states_cs;
    const struct aux_perf_state *pstates_cs;
    size_t n_perf_states_afr;
    const struct aux_perf_state *pstates_afr;
    uint32_t compat_maj;
    uint32_t compat_min;
};

int32_t rust_fill_gpu_initdata(struct initdata_inputs *ins, void *data_a, void *data_b,
                               void *globals);

static int get_core_counts(u32 *count, u32 nclusters, u32 ncores)
{
    u64 base;
    pmgr_adt_power_enable("/arm-io/sgx");

    int adt_sgx_path[8];
    if (adt_path_offset_trace(adt, "/arm-io/sgx", adt_sgx_path) < 0)
        bail("ADT: GPU: Failed to get sgx\n");

    if (adt_get_reg(adt, adt_sgx_path, "reg", 0, &base, NULL) < 0)
        bail("ADT: GPU: Failed to get sgx reg 0\n");

    u32 cores[3] = {0, 0, 0};

    switch (chip_id) {
        case T6002:
            cores[1] = read32(base + 0xd01514);
            /* fallthrough */
        case T8103:
        case T8112:
        case T6000:
        case T6001:
            cores[0] = read32(base + 0xd01500);
            break;
        case T6020:
        case T6021:
        case T6022:
            cores[0] = read32(base + 0xe01500);
            cores[1] = read32(base + 0xe01504);
            cores[2] = read32(base + 0xe01508);
            break;
    }

    for (u32 i = 0; i < nclusters; i++) {
        count[i] = __builtin_popcount(cores[0] & MASK(ncores));

        for (u32 j = 0; j < ARRAY_SIZE(cores); j++) {
            cores[j] >>= ncores;
            if (j < (ARRAY_SIZE(cores) - 1))
                cores[j] |= cores[j + 1] << (32 - ncores);
        }
    }

    return 0;
}

static void adjust_leakage(float *val, u32 clusters, u32 *cores, u32 max, float uncore_fraction)
{
    for (u32 i = 0; i < clusters; i++) {
        float uncore = val[i] * uncore_fraction;
        float core = val[i] - uncore;

        val[i] = uncore + (cores[i] / (float)max) * core;
    }
}

static void load_fuses(float *out, u32 count, u64 base, u32 start, u32 width, float scale,
                       float offset, bool flip)
{
    for (u32 i = 0; i < count; i++) {
        base += (start / 32) * 4;
        start &= 31;

        u32 low = read32(base);
        u32 high = read32(base + 4);
        u32 val = (((((u64)high) << 32) | low) >> start) & MASK(width);

        float fval = (float)val * scale + offset;

        if (flip)
            out[count - i - 1] = fval;
        else
            out[i] = fval;

        start += width;
    }
}

static u32 t8103_pwr_scale[] = {0, 63, 80, 108, 150, 198, 210};

static int calc_power_t8103(u32 count, u32 table_count, const struct perf_state *core,
                            const struct perf_state *sram, const struct aux_perf_states *cs,
                            u32 *max_pwr, float *core_leak, float *sram_leak, float *cs_leak,
                            float *afr_leak)
{
    UNUSED(sram);
    UNUSED(cs);
    UNUSED(core_leak);
    UNUSED(sram_leak);
    UNUSED(cs_leak);
    UNUSED(afr_leak);
    u32 *pwr_scale;
    u32 pwr_scale_count;
    u32 core_count;
    u32 max_cores;

    switch (chip_id) {
        case T8103:
            pwr_scale = t8103_pwr_scale;
            pwr_scale_count = ARRAY_SIZE(t8103_pwr_scale);
            max_cores = 8;
            break;
        default:
            bail("ADT: GPU: Unsupported chip\n");
    }

    if (get_core_counts(&core_count, 1, max_cores))
        return -1;

    if (table_count != 1)
        bail("ADT: GPU: expected 1 perf state table but got %d\n", table_count);

    if (count != pwr_scale_count)
        bail("ADT: GPU: expected %d perf states but got %d\n", pwr_scale_count, count);

    for (u32 i = 0; i < pwr_scale_count; i++)
        max_pwr[i] = (u32)core[i].volt * (u32)pwr_scale[i] * 100;

    core_leak[0] = 1000.0;
    sram_leak[0] = 45.0;

    adjust_leakage(core_leak, 1, &core_count, max_cores, 0.12);
    adjust_leakage(sram_leak, 1, &core_count, max_cores, 0.2);

    return 0;
}

static int calc_power_t600x(u32 count, u32 table_count, const struct perf_state *core,
                            const struct perf_state *sram, const struct aux_perf_states *cs,
                            u32 *max_pwr, float *core_leak, float *sram_leak, float *cs_leak,
                            float *afr_leak)
{
    float s_sram, k_sram, s_core, k_core, s_cs, k_cs;
    float dk_core, dk_sram = 0, dk_cs = 0;
    float imax = 1000;

    u32 ndies = 1;
    u32 nclusters = 0;
    u32 ncores = 0;
    u32 core_count[MAX_CLUSTERS];

    bool simple_exps = false;
    bool adjust_leakages = true;
    bool has_cs = false;

    switch (chip_id) {
        case T6002:
            ndies = 2;
            nclusters += 4;
            load_fuses(core_leak + 4, 4, 0x22922bc1b8, 25, 13, 2, 2, true);
            load_fuses(sram_leak + 4, 4, 0x22922bc1cc, 4, 9, 1, 1, true);
            // fallthrough
        case T6001:
            nclusters += 2;
        case T6000:
            nclusters += 2;
            load_fuses(core_leak + 0, min(4, nclusters), 0x2922bc1b8, 25, 13, 2, 2, false);
            load_fuses(sram_leak + 0, min(4, nclusters), 0x2922bc1cc, 4, 9, 1, 1, false);

            s_sram = 4.3547606;
            k_sram = 0.024927923;
            // macOS difference: macOS uses a misbehaved piecewise function here
            // Since it's obviously wrong, let's just use only the first component
            s_core = 1.48461742;
            k_core = 0.39013552;
            dk_core = 1.06975;
            dk_sram = 0.00625;

            ncores = 8;
            adjust_leakages = true;
            imax = 26.0;
            break;
        case T8112:
            nclusters = 1;
            load_fuses(core_leak, 1, 0x23d2c84dc, 30, 13, 2, 2, false);
            load_fuses(sram_leak, 1, 0x23d2c84b0, 15, 9, 1, 1, false);

            s_sram = 3.61619841;
            k_sram = 0.0529281;
            // macOS difference: macOS uses a misbehaved piecewise function here
            // Since it's obviously wrong, let's just use only the first component
            s_core = 1.21356187;
            k_core = 0.43328839;
            dk_core = 0.983196;
            dk_sram = 0.007828;

            simple_exps = true;
            ncores = 10;
            adjust_leakages = false; // pre-adjusted?
            imax = 24.0;
            break;
        case T6022:
            ndies = 2;
            nclusters += 4;
            load_fuses(core_leak + 4, min(4, nclusters), 0x229e2cc1f8, 4, 13, 2, 2, true);
            load_fuses(sram_leak + 4, min(4, nclusters), 0x229e2cc208, 19, 9, 1, 1, true);
            load_fuses(cs_leak + 1, 1, 0x229e2cc204, 8, 12, 1, 1, false);
            load_fuses(afr_leak + 1, 1, 0x229e2cc210, 0, 12, 1, 1, false);

            // For some reason, this one is different on T6022...
            dk_cs = 6.7;
            // fallthrough
        case T6021:
            if (!dk_cs)
                dk_cs = 4.492;

            nclusters += 4;
            s_sram = 5.808;
            k_sram = 0.00707;
            // macOS difference: macOS uses a misbehaved piecewise function here
            // Since it's obviously wrong, let's just use only the first component
            s_core = 1.24554153;
            k_core = 0.56203084;

            s_cs = 1.87;
            k_cs = 0.162;

            goto t602x;

        case T6020:
            nclusters = 2;
            s_sram = 5.02191218;
            k_sram = 0.0145621013;
            // macOS difference: macOS uses a misbehaved piecewise function here
            // Since it's obviously wrong, let's just use only the first component
            s_core = 1.21006932;
            k_core = 0.52776378;

            s_cs = 1.8;
            k_cs = 0.162;
            dk_cs = 1.889;

        t602x:
            dk_core = 1.00075;
            dk_sram = 0.00785;
            load_fuses(core_leak + 0, min(4, nclusters), 0x29e2cc1f8, 4, 13, 2, 2, false);
            load_fuses(sram_leak + 0, min(4, nclusters), 0x29e2cc208, 19, 9, 1, 1, false);
            load_fuses(cs_leak + 0, 1, 0x29e2cc204, 8, 12, 1, 1, false);
            load_fuses(afr_leak + 0, 1, 0x29e2cc210, 0, 12, 1, 1, false);

            simple_exps = true;
            ncores = 10;
            adjust_leakages = false; // pre-adjusted?
            imax = 33.0;
            has_cs = true;
            break;

        default:
            bail("ADT: GPU: Unsupported chip\n");
    }

    if (get_core_counts(core_count, nclusters, ncores))
        return -1;

    printf("FDT: GPU: Core counts: ");
    for (u32 i = 0; i < nclusters; i++) {
        printf("%d ", core_count[i]);
    }
    printf("\n");

    if (adjust_leakages) {
        adjust_leakage(core_leak, nclusters, core_count, ncores, 0.0825);
        adjust_leakage(sram_leak, nclusters, core_count, ncores, 0.2247);
    }

    if (table_count != nclusters)
        bail("ADT: GPU: expected %d perf state tables but got %d\n", nclusters, table_count);

    if (has_cs && (!cs || !cs_leak)) {
        bail("ADT: GPU: expected CS perf table, but not found\n");
    }

    max_pwr[0] = 0;

    for (u32 i = 1; i < count; i++) {
        u32 total_mw = 0;

        for (u32 j = 0; j < nclusters; j++) {
            // macOS difference: macOS truncates Hz to integer MHz before doing this math.
            // That's probably wrong, so let's not do that.

            float mw = 0;
            size_t idx = j * count + i;

            mw += sram[idx].volt / 1000.f * sram_leak[j] * k_sram *
                  expf(sram[idx].volt / 1000.f * s_sram);
            mw += core[idx].volt / 1000.f * core_leak[j] * k_core *
                  expf(core[idx].volt / 1000.f * s_core);

            float sbase = sram[idx].volt / 750.f;
            float sram_v_p;
            if (simple_exps)
                sram_v_p = sbase * sbase; // v ^ 2
            else
                sram_v_p = sbase * sbase * sbase; // v ^ 3
            mw += dk_sram * core_count[j] * (sram[idx].freq / 1000000.f) * sram_v_p;

            float cbase = core[idx].volt / 750.f;
            float core_v_p;
            if (simple_exps || core[idx].volt < 750)
                core_v_p = cbase * cbase; // v ^ 2
            else
                core_v_p = cbase * cbase * cbase; // v ^ 3
            mw += dk_core * core_count[j] * (core[idx].freq / 1000000.f) * core_v_p;

            if (mw > imax * core[idx].volt)
                mw = imax * core[idx].volt;

            total_mw += mw;
        }

        // CS gets added after the imax limit

        if (has_cs) {
            for (u32 j = 0; j < ndies; j++) {
                float mw = 0;

                int csi = j * cs->count + min(i, cs->count - 1);
                u32 cs_mv = cs->states[csi].volt / 1000;
                u32 cs_hz = cs->states[csi].freq;

                mw += cs_mv / 1000.f * cs_leak[j] * k_cs * expf(cs_mv / 1000.f * s_cs);
                float csbase = cs_mv / 750.f;
                float cs_v_p = powf(csbase, 1.8);
                mw += dk_cs * (cs_hz / 1000000.f) * cs_v_p;

                total_mw += mw;
            }
        }

        max_pwr[i] = total_mw * 1000;
    }

    return 0;
}

static int dt_set_resvmem(void *dt, const char *path, u64 base, u64 size)
{
    int node = fdt_path_offset(dt, path);
    if (node < 0)
        bail("FDT: GPU: failed to find %s node\n", path);

    fdt64_t reg[2];

    fdt64_st(&reg[0], base);
    fdt64_st(&reg[1], size);

    if (fdt_setprop_string(dt, node, "status", "okay"))
        bail("FDT: GPU: failed to un-disable memory region");

    if (fdt_setprop(dt, node, "reg", reg, sizeof(reg)))
        bail("FDT: GPU: failed to set reg prop for %s\n", path);

    return 0;
}

static int dt_set_region(void *dt, int sgx, const char *name, const char *path)
{
    u64 base, size;
    char prop[64];

    snprintf(prop, sizeof(prop), "%s-base", name);
    if (ADT_GETPROP(adt, sgx, prop, &base) < 0 || !base)
        bail("ADT: GPU: failed to find %s property\n", prop);

    snprintf(prop, sizeof(prop), "%s-size", name);
    if (ADT_GETPROP(adt, sgx, prop, &size) < 0 || !base)
        bail("ADT: GPU: failed to find %s property\n", prop);

    return dt_set_resvmem(dt, path, base, size);
}

int fdt_set_float_array(void *dt, int node, const char *name, float *val, int count)
{
    fdt32_t data[MAX_CLUSTERS];

    if (count > MAX_CLUSTERS)
        bail("FDT: GPU: fdt_set_float_array() with too many values\n");

    memcpy(data, val, sizeof(float) * count);
    for (int i = 0; i < count; i++) {
        data[i] = cpu_to_fdt32(data[i]);
    }

    if (fdt_setprop_inplace(dt, node, name, data, sizeof(u32) * count))
        bail("FDT: GPU: Failed to set %s\n", name);

    return 0;
}

static int fdt_set_aux_opp(void *dt, int gpu, const char *prop, const struct aux_perf_states *ps,
                           u32 dies)
{
    int len;
    const fdt32_t *opps_ph = fdt_getprop(dt, gpu, prop, &len);
    if (!opps_ph || len != 4)
        bail("FDT: GPU: %s not found\n", prop);

    int opps = fdt_node_offset_by_phandle(dt, fdt32_ld(opps_ph));
    if (opps < 0)
        bail("FDT: GPU: node for phandle %u not found\n", fdt32_ld(opps_ph));

    u32 count = ps->count;

    u32 i = 0;
    int opp;
    fdt_for_each_subnode(opp, dt, opps)
    {
        fdt32_t volts[MAX_DIES];

        for (u32 j = 0; j < dies; j++) {
            volts[j] = cpu_to_fdt32(ps->states[i + j * ps->count].volt);
        }

        if (i >= count)
            bail("FDT: GPU: Expected %d operating points, but found more\n", count);

        if (fdt_setprop_inplace(dt, opp, "opp-microvolt", &volts, sizeof(u32) * dies))
            bail("FDT: GPU: Failed to set opp-microvolt for aux PS %d\n", i);

        if (fdt_setprop_inplace_u64(dt, opp, "opp-hz", ps->states[i].freq))
            bail("FDT: GPU: Failed to set opp-hz for PS %d\n", i);

        i++;
    }

    return 0;
}

/*
 * T8122 (M3) GPU firmware handoff.
 *
 * iBoot preloads the GFX ASC firmware and leaves the firmware's UAT page
 * tables, TTBAT and handoff page in RAM. Nothing else in m1n1 reserves that
 * memory on T8122, so the OS could reuse pages the firmware still occupies.
 *
 * On every T8122 boot this reserves, no-map, the four UAT regions of
 * /arm-io/sgx and the __TEXT/__DATA segments of /arm-io/gfx-asc. When the ADT
 * description is complete and consistent and the DT has an apple,agx-t8122
 * GPU node, the six regions are also linked to that node by name (ttbs,
 * pagetables, handoff, shared-l2, fw-text, fw-data), together with the
 * segment description the M3 runtime expects: apple,m3-handoff-version,
 * apple,firmware-segment-vas and apple,firmware-segment-flags. The
 * publication runs on a copy of the FDT and is committed whole or not at all.
 *
 * The GPU node stays disabled unless the board is J613 and the node carries
 * the apple,j613-native-gpu opt-in. This never fails the boot.
 */
#define T8122_GPU_REGIONS 6
#define T8122_GPU_UAT     4
#define T8122_GPU_FW_VA   0xfffffc0000000000UL
#define T8122_GPU_MAX_SEG 0x200000UL
#define T8122_GPU_PA_MAX  (1UL << 42)
#define T8122_GPU_MAX_MR  32
#define T8122_GPU_OPT_IN  "apple,j613-native-gpu"

static const char *const t8122_gpu_names[T8122_GPU_REGIONS] = {
    "ttbs", "pagetables", "handoff", "shared-l2", "fw-text", "fw-data",
};
/* Static reserved-memory nodes the M3 runtime also accepts (m3_board.rs). */
static const char *const t8122_gpu_nodes[T8122_GPU_UAT] = {
    "uat-ttbs",
    "uat-pagetables",
    "uat-handoff",
    "uat-pagetables-l2",
};
static const char *const t8122_gpu_adt_regions[T8122_GPU_UAT] = {
    "gpu-region",
    "gfx-shared-region",
    "gfx-handoff",
    "gfx-shared-l2-region",
};

struct t8122_gpu_fw {
    u64 base[T8122_GPU_REGIONS], size[T8122_GPU_REGIONS];
    u64 vas[2];
    u32 flags[2];
};

static int t8122_adt_u64(int node, const char *name, u64 *val)
{
    u32 len;
    const void *p = adt_getprop(adt, node, name, &len);
    if (!p || len != sizeof(*val))
        return -1;
    memcpy(val, p, sizeof(*val));
    return 0;
}

static bool t8122_adt_compatible(int node, const char *compat)
{
    u32 len;
    const char *p = adt_getprop(adt, node, "compatible", &len);
    for (u32 off = 0; p && off < len;) {
        u32 n = strnlen(p + off, len - off);
        if (n == strlen(compat) && !memcmp(p + off, compat, n))
            return true;
        off += n + 1;
    }
    return false;
}

static int t8122_gpu_dram(u64 *start, u64 *end)
{
    int chosen = adt_path_offset(adt, "/chosen");
    u64 size;
    if (chosen < 0 || t8122_adt_u64(chosen, "dram-base", start) ||
        t8122_adt_u64(chosen, "dram-size", &size) || !size || *start + size <= *start)
        bail("ADT: GPU: T8122 DRAM bounds missing\n");
    *end = *start + size;
    return 0;
}

static bool t8122_gpu_range_ok(u64 base, u64 size, u64 dram_start, u64 dram_end)
{
    return base && size && !((base | size) & (SZ_16K - 1)) && base + size > base &&
           base >= dram_start && base + size <= dram_end && base + size <= T8122_GPU_PA_MAX;
}

/* Validate the complete ADT description before anything touches the FDT. */
static int t8122_gpu_read_adt(struct t8122_gpu_fw *fw)
{
    int sgx = adt_path_offset(adt, "/arm-io/sgx");
    if (sgx < 0 || !t8122_adt_compatible(sgx, "gpu,t8122"))
        bail("ADT: GPU: T8122 /arm-io/sgx missing or not gpu,t8122\n");

    for (int i = 0; i < T8122_GPU_UAT; i++) {
        char prop[64];
        snprintf(prop, sizeof(prop), "%s-base", t8122_gpu_adt_regions[i]);
        if (t8122_adt_u64(sgx, prop, &fw->base[i]))
            bail("ADT: GPU: T8122 sgx %s missing\n", prop);
        snprintf(prop, sizeof(prop), "%s-size", t8122_gpu_adt_regions[i]);
        if (t8122_adt_u64(sgx, prop, &fw->size[i]))
            bail("ADT: GPU: T8122 sgx %s missing\n", prop);
    }

    int asc = adt_path_offset(adt, "/arm-io/gfx-asc");
    int nub = adt_path_offset(adt, "/arm-io/gfx-asc/iop-gfx-nub");
    if (asc < 0 || nub < 0)
        bail("ADT: GPU: T8122 gfx-asc or iop-gfx-nub missing\n");

    u32 len, nub_len, preloaded;
    const struct adt_segment_ranges *seg = adt_getprop(adt, asc, "segment-ranges", &len);
    const void *nub_seg = adt_getprop(adt, nub, "segment-ranges", &nub_len);
    if (!seg || len != 2 * sizeof(*seg) || !nub_seg || nub_len != len || memcmp(seg, nub_seg, len))
        bail("ADT: GPU: T8122 gfx-asc segment-ranges missing or unlike iop-gfx-nub\n");
    const char *names = adt_getprop(adt, asc, "segment-names", &len);
    if (!names || len < 14 || memcmp(names, "__TEXT;__DATA", 14))
        bail("ADT: GPU: T8122 gfx-asc segment-names is not __TEXT;__DATA\n");
    const void *pre = adt_getprop(adt, nub, "pre-loaded", &len);
    if (!pre || len != sizeof(preloaded))
        bail("ADT: GPU: T8122 GPU firmware is not pre-loaded\n");
    memcpy(&preloaded, pre, sizeof(preloaded));
    if (preloaded != 1)
        bail("ADT: GPU: T8122 GPU firmware is not pre-loaded\n");

    for (int i = 0; i < 2; i++) {
        struct adt_segment_ranges s;
        memcpy(&s, &seg[i], sizeof(s));
        if (s.phys != s.remap || !s.size || s.size & (SZ_16K - 1) || s.size > T8122_GPU_MAX_SEG ||
            s.unk != (i == 0 ? 1 : 0))
            bail("ADT: GPU: T8122 gfx-asc segment %d is not a plain preloaded segment\n", i);
        fw->base[T8122_GPU_UAT + i] = s.phys;
        fw->size[T8122_GPU_UAT + i] = s.size;
        fw->vas[i] = s.iova;
        fw->flags[i] = s.unk;
    }
    if (fw->vas[0] != T8122_GPU_FW_VA || fw->vas[1] != fw->vas[0] + fw->size[T8122_GPU_UAT])
        bail("ADT: GPU: T8122 firmware segments at unexpected VAs 0x%lx/0x%lx\n", fw->vas[0],
             fw->vas[1]);

    u64 dram_start, dram_end;
    if (t8122_gpu_dram(&dram_start, &dram_end))
        return -1;
    for (int i = 0; i < T8122_GPU_REGIONS; i++) {
        if (!t8122_gpu_range_ok(fw->base[i], fw->size[i], dram_start, dram_end))
            bail("ADT: GPU: T8122 %s region 0x%lx+0x%lx is not a 16K-aligned DRAM range\n",
                 t8122_gpu_names[i], fw->base[i], fw->size[i]);
        for (int j = 0; j < i; j++)
            if (fw->base[i] < fw->base[j] + fw->size[j] && fw->base[j] < fw->base[i] + fw->size[i])
                bail("ADT: GPU: T8122 %s region overlaps %s\n", t8122_gpu_names[i],
                     t8122_gpu_names[j]);
    }
    return 0;
}

/* Run fn on a copy of the FDT and commit the copy only if fn succeeds. */
static int t8122_fdt_transaction(void *dt, int (*fn)(void *dt, void *arg), void *arg)
{
    int size = fdt_totalsize(dt), ret = -1;
    void *copy = malloc(size);
    if (copy && !fdt_open_into(dt, copy, size)) {
        ret = fn(copy, arg);
        if (!ret)
            memcpy(dt, copy, size);
    }
    free(copy);
    return ret;
}

/* The GPU node: the gpu alias (as for the other SoCs), else the only
 * apple,agx-t8122 node. An alias to anything else is refused.
 */
static int t8122_gpu_node(void *dt)
{
    int gpu = fdt_path_offset(dt, "gpu");
    if (gpu >= 0)
        return fdt_node_check_compatible(dt, gpu, "apple,agx-t8122") ? -FDT_ERR_BADVALUE : gpu;
    gpu = fdt_node_offset_by_compatible(dt, -1, "apple,agx-t8122");
    if (gpu >= 0 && fdt_node_offset_by_compatible(dt, gpu, "apple,agx-t8122") >= 0)
        return -FDT_ERR_BADVALUE;
    return gpu;
}

static int t8122_resv_node(void *dt)
{
    int resv = fdt_path_offset(dt, "/reserved-memory");
    if (resv < 0 || fdt_address_cells(dt, resv) != 2 || fdt_size_cells(dt, resv) != 2)
        return -1;
    return resv;
}

/* The /reserved-memory node that holds region i: the static uat-* node for the
 * UAT regions, asc-firmware@<phys> (as for the other ASCs) for the segments.
 */
static void t8122_gpu_node_name(char *name, size_t len, int i, u64 base)
{
    if (i < T8122_GPU_UAT)
        snprintf(name, len, "%s", t8122_gpu_nodes[i]);
    else
        snprintf(name, len, "asc-firmware@%lx", base);
}

/*
 * Whether base+size overlaps an enabled reserved-memory node or a /memreserve/
 * entry. The node `self`, which is about to be (re)written, does not count:
 * a static uat-* node whatever its old reg, an asc-firmware@ node only if it
 * already describes exactly this range. Sets *covered when an overlapping
 * entry contains the whole range. Returns 1 on overlap, 0 if free, -1 on error.
 */
static int t8122_resv_overlap(void *dt, const char *self, bool self_any, u64 base, u64 size,
                              bool *covered)
{
    int resv = t8122_resv_node(dt), node, ret = 0;
    *covered = false;
    if (resv < 0)
        return -1;
    fdt_for_each_subnode(node, dt, resv)
    {
        const char *name = fdt_get_name(dt, node, NULL);
        const char *status = fdt_getprop(dt, node, "status", NULL);
        int len;
        const fdt64_t *reg = fdt_getprop(dt, node, "reg", &len);
        bool disabled = status && strcmp(status, "okay") && strcmp(status, "ok");
        if (disabled || !reg)
            continue;
        if (name && !strcmp(name, self) &&
            (self_any || (len == 16 && fdt64_ld(reg) == base && fdt64_ld(reg + 1) == size)))
            continue;
        for (int i = 0; i + 16 <= len; i += 16, reg += 2) {
            u64 start = fdt64_ld(reg), end = start + fdt64_ld(reg + 1);
            if (start < base + size && base < end) {
                printf("FDT: GPU: T8122 range 0x%lx+0x%lx overlaps /reserved-memory/%s\n", base,
                       size, name);
                *covered |= start <= base && base + size <= end;
                ret = 1;
            }
        }
    }
    for (int i = 0; i < fdt_num_mem_rsv(dt); i++) {
        u64 start, len;
        if (fdt_get_mem_rsv(dt, i, &start, &len) == 0 && start < base + size &&
            base < start + len) {
            printf("FDT: GPU: T8122 range 0x%lx+0x%lx overlaps /memreserve/ 0x%lx+0x%lx\n", base,
                   size, start, len);
            *covered |= start <= base && base + size <= start + len;
            ret = 1;
        }
    }
    return ret;
}

/* Create or fill the no-map node for one region. Returns its phandle or 0. */
static u32 t8122_gpu_reserve(void *dt, const char *name, bool asc_mem, u64 base, u64 size)
{
    int resv = t8122_resv_node(dt);
    if (resv < 0)
        return 0;
    int node = fdt_subnode_offset(dt, resv, name);
    if (node < 0)
        node = fdt_add_subnode(dt, resv, name);
    if (node < 0)
        return 0;

    fdt64_t reg[2];
    fdt64_st(&reg[0], base);
    fdt64_st(&reg[1], size);
    if (fdt_setprop(dt, node, "reg", reg, sizeof(reg)) || fdt_setprop_empty(dt, node, "no-map") ||
        (asc_mem && fdt_setprop_string(dt, node, "compatible", "apple,asc-mem")) ||
        (fdt_getprop(dt, node, "status", NULL) && fdt_setprop_string(dt, node, "status", "okay")))
        return 0;

    u32 phandle = fdt_get_phandle(dt, node);
    if (!phandle && (fdt_generate_phandle(dt, &phandle) ||
                     fdt_setprop_u32(dt, node, "phandle", phandle)))
        return 0;
    return phandle;
}

/*
 * Set memory-region/memory-region-names on the GPU node: entries the DT
 * already has under other names stay first, in order; the six handoff regions
 * replace any of the same name.
 */
static int t8122_gpu_link(void *dt, int gpu, const u32 *phandles)
{
    fdt32_t regions[T8122_GPU_MAX_MR];
    char names[512];
    int count = 0, used = 0, len = 0, names_len = 0;

    const fdt32_t *old = fdt_getprop(dt, gpu, "memory-region", &len);
    const char *old_names = fdt_getprop(dt, gpu, "memory-region-names", &names_len);
    int old_count = old ? len / 4 : 0;
    if ((old && len % 4) || (old_count && !old_names) || (!old && old_names))
        bail("FDT: GPU: T8122 GPU node memory-region lists are inconsistent\n");

    int off = 0;
    for (int i = 0; i < old_count; i++) {
        if (off >= names_len)
            bail("FDT: GPU: T8122 GPU node has fewer memory-region-names than regions\n");
        const char *name = old_names + off;
        int n = strnlen(name, names_len - off) + 1;
        if (off + n > names_len)
            bail("FDT: GPU: T8122 GPU node memory-region-names is not terminated\n");
        off += n;
        bool ours = false;
        for (int j = 0; j < T8122_GPU_REGIONS; j++)
            ours |= !strcmp(name, t8122_gpu_names[j]);
        if (ours)
            continue;
        if (count >= T8122_GPU_MAX_MR - T8122_GPU_REGIONS || used + n > (int)sizeof(names) - 64)
            bail("FDT: GPU: T8122 GPU node has too many memory regions\n");
        regions[count++] = old[i];
        memcpy(names + used, name, n);
        used += n;
    }
    if (old_names && off != names_len)
        bail("FDT: GPU: T8122 GPU node has more memory-region-names than regions\n");
    for (int j = 0; j < T8122_GPU_REGIONS; j++) {
        regions[count++] = cpu_to_fdt32(phandles[j]);
        int n = strlen(t8122_gpu_names[j]) + 1;
        memcpy(names + used, t8122_gpu_names[j], n);
        used += n;
    }

    if (fdt_setprop(dt, gpu, "memory-region", regions, count * sizeof(fdt32_t)))
        bail("FDT: GPU: T8122 cannot set memory-region\n");
    gpu = t8122_gpu_node(dt);
    if (gpu < 0 || fdt_setprop(dt, gpu, "memory-region-names", names, used))
        bail("FDT: GPU: T8122 cannot set memory-region-names\n");
    return 0;
}

static int t8122_gpu_publish(void *dt, void *arg)
{
    const struct t8122_gpu_fw *fw = arg;
    u32 phandles[T8122_GPU_REGIONS];
    char name[64];

    int gpu = t8122_gpu_node(dt);
    if (gpu < 0)
        bail("FDT: GPU: T8122 DT has no apple,agx-t8122 GPU node (gpu alias)\n");
    if (t8122_resv_node(dt) < 0)
        bail("FDT: GPU: T8122 DT has no usable /reserved-memory\n");

    /* Nothing else may already claim any part of the handoff. */
    for (int i = 0; i < T8122_GPU_REGIONS; i++) {
        bool covered;
        t8122_gpu_node_name(name, sizeof(name), i, fw->base[i]);
        if (t8122_resv_overlap(dt, name, i < T8122_GPU_UAT, fw->base[i], fw->size[i], &covered))
            bail("FDT: GPU: T8122 %s region conflicts with an existing reservation\n",
                 t8122_gpu_names[i]);
    }
    for (int i = 0; i < T8122_GPU_REGIONS; i++) {
        t8122_gpu_node_name(name, sizeof(name), i, fw->base[i]);
        phandles[i] = t8122_gpu_reserve(dt, name, i >= T8122_GPU_UAT, fw->base[i], fw->size[i]);
        if (!phandles[i])
            bail("FDT: GPU: T8122 cannot reserve %s region\n", t8122_gpu_names[i]);
    }

    gpu = t8122_gpu_node(dt);
    if (gpu < 0 || t8122_gpu_link(dt, gpu, phandles))
        return -1;

    fdt64_t vas[2];
    fdt32_t flags[2];
    for (int i = 0; i < 2; i++) {
        fdt64_st(&vas[i], fw->vas[i]);
        flags[i] = cpu_to_fdt32(fw->flags[i]);
    }
    gpu = t8122_gpu_node(dt);
    if (gpu < 0 || fdt_setprop_u32(dt, gpu, "apple,m3-handoff-version", 1) ||
        fdt_setprop(dt, gpu, "apple,firmware-segment-vas", vas, sizeof(vas)) ||
        fdt_setprop(dt, gpu, "apple,firmware-segment-flags", flags, sizeof(flags)))
        bail("FDT: GPU: T8122 cannot set the firmware segment description\n");

    bool enable = !fdt_node_check_compatible(dt, 0, "apple,j613") &&
                  fdt_getprop(dt, gpu, T8122_GPU_OPT_IN, NULL);
    if (fdt_setprop_string(dt, gpu, "status", enable ? "okay" : "disabled"))
        bail("FDT: GPU: T8122 cannot set GPU status\n");

    for (int i = 0; i < T8122_GPU_REGIONS; i++)
        printf("FDT: GPU: T8122 %s: 0x%lx+0x%lx (no-map)\n", t8122_gpu_names[i], fw->base[i],
               fw->size[i]);
    printf("FDT: GPU: T8122 firmware VAs 0x%lx/0x%lx; handoff published, GPU %s\n", fw->vas[0],
           fw->vas[1], enable ? "enabled (" T8122_GPU_OPT_IN ")" : "left disabled");
    return 0;
}

struct t8122_gpu_range {
    const char *what;
    int index;
    u64 base, size;
};

static int t8122_gpu_reserve_one(void *dt, void *arg)
{
    const struct t8122_gpu_range *r = arg;
    char name[64];
    t8122_gpu_node_name(name, sizeof(name), r->index, r->base);
    return t8122_gpu_reserve(dt, name, r->index >= T8122_GPU_UAT, r->base, r->size) ? 0 : -1;
}

static void t8122_gpu_reserve_range(void *dt, const struct t8122_gpu_range *r, u64 dram_start,
                                    u64 dram_end)
{
    bool covered;
    char name[64];
    t8122_gpu_node_name(name, sizeof(name), r->index, r->base);
    if (!t8122_gpu_range_ok(r->base, r->size, dram_start, dram_end)) {
        printf("FDT: GPU: T8122 %s 0x%lx+0x%lx is unusable, not reserved\n", r->what, r->base,
               r->size);
        return;
    }
    int overlap =
        t8122_resv_overlap(dt, name, r->index < T8122_GPU_UAT, r->base, r->size, &covered);
    if (overlap) {
        printf("FDT: GPU: T8122 %s 0x%lx+0x%lx %s\n", r->what, r->base, r->size,
               overlap < 0 ? "not reserved (no /reserved-memory)"
               : covered   ? "is already reserved"
                           : "partially overlaps another reservation; not reserved");
        return;
    }
    if (t8122_fdt_transaction(dt, t8122_gpu_reserve_one, (void *)r))
        printf("FDT: GPU: T8122 failed to reserve %s 0x%lx+0x%lx\n", r->what, r->base, r->size);
    else
        printf("FDT: GPU: T8122 reserved %s 0x%lx+0x%lx (unlinked)\n", r->what, r->base, r->size);
}

/* Fallback: reserve whatever the ADT describes, one range at a time. */
static void t8122_gpu_reserve_all(void *dt)
{
    u64 dram_start = 0, dram_end = T8122_GPU_PA_MAX;
    if (t8122_gpu_dram(&dram_start, &dram_end))
        printf("ADT: GPU: T8122 reserving without DRAM bounds\n");

    int sgx = adt_path_offset(adt, "/arm-io/sgx");
    for (int i = 0; sgx >= 0 && i < T8122_GPU_UAT; i++) {
        char prop[64];
        struct t8122_gpu_range r = {t8122_gpu_names[i], i, 0, 0};
        snprintf(prop, sizeof(prop), "%s-base", t8122_gpu_adt_regions[i]);
        int missing = t8122_adt_u64(sgx, prop, &r.base);
        snprintf(prop, sizeof(prop), "%s-size", t8122_gpu_adt_regions[i]);
        missing |= t8122_adt_u64(sgx, prop, &r.size);
        if (missing)
            printf("ADT: GPU: T8122 sgx has no %s region\n", t8122_gpu_adt_regions[i]);
        else
            t8122_gpu_reserve_range(dt, &r, dram_start, dram_end);
    }

    /* Both copies of the segment list, in case they disagree. */
    const char *paths[] = {"/arm-io/gfx-asc", "/arm-io/gfx-asc/iop-gfx-nub"};
    const void *first = NULL;
    u32 first_len = 0;
    for (int p = 0; p < 2; p++) {
        int node = adt_path_offset(adt, paths[p]);
        u32 len = 0;
        const struct adt_segment_ranges *seg =
            node < 0 ? NULL : adt_getprop(adt, node, "segment-ranges", &len);
        if (!seg || !len || len % sizeof(*seg) || len > 8 * sizeof(*seg)) {
            printf("ADT: GPU: T8122 %s has no usable segment-ranges\n", paths[p]);
            continue;
        }
        if (first && len == first_len && !memcmp(seg, first, len))
            continue;
        first = seg;
        first_len = len;
        for (u32 i = 0; i < len / sizeof(*seg); i++) {
            struct adt_segment_ranges s;
            memcpy(&s, &seg[i], sizeof(s));
            struct t8122_gpu_range r = {"firmware segment", T8122_GPU_UAT + (i ? 1 : 0), s.phys,
                                        ALIGN_UP((u64)s.size, SZ_16K)};
            t8122_gpu_reserve_range(dt, &r, dram_start, dram_end);
        }
    }
}

static int t8122_gpu_disable(void *dt, void *arg)
{
    UNUSED(arg);
    int gpu = t8122_gpu_node(dt);
    return gpu < 0 ? 0 : fdt_setprop_string(dt, gpu, "status", "disabled");
}

static int dt_set_gpu_t8122(void *dt)
{
    struct t8122_gpu_fw fw;

    printf("FDT: GPU: T8122 firmware handoff\n");
    if (!t8122_gpu_read_adt(&fw) && !t8122_fdt_transaction(dt, t8122_gpu_publish, &fw))
        return 0;

    printf("FDT: GPU: T8122 handoff not published; reserving the GPU firmware memory only\n");
    if (t8122_fdt_transaction(dt, t8122_gpu_disable, NULL))
        printf("FDT: GPU: T8122 cannot mark the GPU node disabled\n");
    t8122_gpu_reserve_all(dt);
    return 0;
}

int dt_set_gpu(void *dt)
{
    if (chip_id == T8122)
        return dt_set_gpu_t8122(dt);

    bool has_cs_afr = false;
    int (*calc_power)(u32 count, u32 table_count, const struct perf_state *core,
                      const struct perf_state *sram, const struct aux_perf_states *cs, u32 *max_pwr,
                      float *core_leak, float *sram_leak, float *cs_leak, float *afr_leak);

    u32 dies = 1;

    printf("FDT: GPU: Initializing GPU info\n");

    switch (chip_id) {
        case T8103:
            calc_power = calc_power_t8103;
            break;
        case T6022:
            dies = 2;
            // fallthrough
        case T6021:
        case T6020:
            has_cs_afr = true;
            calc_power = calc_power_t600x;
            break;
        case T6002:
            dies = 2;
            // fallthrough
        case T6001:
        case T6000:
        case T8112:
            calc_power = calc_power_t600x;
            break;
        default:
            printf("ADT: GPU: unsupported chip!\n");
            return 0;
    }

    int gpu = fdt_path_offset(dt, "gpu");
    if (gpu < 0) {
        printf("FDT: GPU: gpu alias not found in device tree\n");
        return 0;
    }

    /* check for old-style downstream compatibles */
    bool downstream_dtb = false;
    downstream_dtb |= fdt_node_check_compatible(dt, gpu, "apple,agx-t8103") == 0;
    downstream_dtb |= fdt_node_check_compatible(dt, gpu, "apple,agx-t8112") == 0;
    downstream_dtb |= fdt_node_check_compatible(dt, gpu, "apple,agx-t6000") == 0;
    downstream_dtb |= fdt_node_check_compatible(dt, gpu, "apple,agx-t6001") == 0;
    downstream_dtb |= fdt_node_check_compatible(dt, gpu, "apple,agx-t6002") == 0;
    downstream_dtb |= fdt_node_check_compatible(dt, gpu, "apple,agx-t6020") == 0;
    downstream_dtb |= fdt_node_check_compatible(dt, gpu, "apple,agx-t6021") == 0;
    downstream_dtb |= fdt_node_check_compatible(dt, gpu, "apple,agx-t6022") == 0;

    int sgx = adt_path_offset(adt, "/arm-io/sgx");
    if (sgx < 0)
        bail("ADT: GPU: /arm-io/sgx node not found\n");

    u32 perf_state_count;
    if (ADT_GETPROP(adt, sgx, "perf-state-count", &perf_state_count) < 0 || !perf_state_count)
        bail("ADT: GPU: missing perf-state-count\n");

    u32 perf_state_table_count;
    if (ADT_GETPROP(adt, sgx, "perf-state-table-count", &perf_state_table_count) < 0 ||
        !perf_state_table_count)
        bail("ADT: GPU: missing perf-state-table-count\n");

    if (perf_state_count > MAX_PSTATES)
        bail("ADT: GPU: perf-state-count too large\n");

    if (perf_state_table_count > MAX_CLUSTERS)
        bail("ADT: GPU: perf-state-table-count too large\n");

    u32 perf_states_len;
    const struct perf_state *perf_states, *perf_states_sram;
    const struct aux_perf_states *perf_states_afr, *perf_states_cs;

    perf_states = adt_getprop(adt, sgx, "perf-states", &perf_states_len);
    if (!perf_states ||
        perf_states_len != sizeof(*perf_states) * perf_state_count * perf_state_table_count)
        bail("ADT: GPU: invalid perf-states length\n");

    perf_states_sram = adt_getprop(adt, sgx, "perf-states-sram", &perf_states_len);
    if (perf_states_sram &&
        perf_states_len != sizeof(*perf_states) * perf_state_count * perf_state_table_count)
        bail("ADT: GPU: invalid perf-states-sram length\n");

    perf_states_afr = adt_getprop(adt, sgx, "afr-perf-states", NULL);
    perf_states_cs = adt_getprop(adt, sgx, "cs-perf-states", NULL);

    if (has_cs_afr && !perf_states_cs)
        bail("ADT: GPU: cs-perf-states not found\n");

    if (has_cs_afr && !perf_states_afr)
        bail("ADT: GPU: afr-perf-states not found\n");

    u32 max_pwr[MAX_PSTATES];
    float core_leak[MAX_CLUSTERS];
    float sram_leak[MAX_CLUSTERS];
    float cs_leak[MAX_DIES];
    float afr_leak[MAX_DIES];

    if (calc_power(perf_state_count, perf_state_table_count, perf_states, perf_states_sram,
                   perf_states_cs, max_pwr, core_leak, sram_leak, cs_leak, afr_leak))
        return -1;

    printf("FDT: GPU: Max power table: ");
    for (u32 i = 0; i < perf_state_count; i++) {
        printf("%d ", max_pwr[i]);
    }
    printf("\nFDT: GPU: Core leakage table: ");
    for (u32 i = 0; i < perf_state_table_count; i++) {
        printf("%d.%03d ", (int)core_leak[i], ((int)(core_leak[i] * 1000) % 1000));
    }
    printf("\nFDT: GPU: SRAM leakage table: ");
    for (u32 i = 0; i < perf_state_table_count; i++) {
        printf("%d.%03d ", (int)sram_leak[i], ((int)(sram_leak[i] * 1000) % 1000));
    }
    printf("\n");

    if (has_cs_afr) {
        printf("FDT: GPU: CS leakage table: ");
        for (u32 i = 0; i < dies; i++) {
            printf("%d.%03d ", (int)cs_leak[i], ((int)(cs_leak[i] * 1000) % 1000));
        }
        printf("\n");

        printf("FDT: GPU: AFR leakage table: ");
        for (u32 i = 0; i < dies; i++) {
            printf("%d.%03d ", (int)afr_leak[i], ((int)(afr_leak[i] * 1000) % 1000));
        }
        printf("\n");
    }

    if (downstream_dtb) {
        if (fdt_set_float_array(dt, gpu, "apple,core-leak-coef", core_leak, perf_state_table_count))
            return -1;

        if (fdt_set_float_array(dt, gpu, "apple,sram-leak-coef", sram_leak, perf_state_table_count))
            return -1;

        int len;
        const fdt32_t *opps_ph = fdt_getprop(dt, gpu, "operating-points-v2", &len);
        if (!opps_ph || len != 4)
            bail("FDT: GPU: operating-points-v2 not found\n");

        int opps = fdt_node_offset_by_phandle(dt, fdt32_ld(opps_ph));
        if (opps < 0)
            bail("FDT: GPU: node for phandle %u not found\n", fdt32_ld(opps_ph));

        u32 i = 0;
        int opp;
        fdt_for_each_subnode(opp, dt, opps)
        {
            fdt32_t volts[MAX_CLUSTERS];

            for (u32 j = 0; j < perf_state_table_count; j++) {
                volts[j] = cpu_to_fdt32(perf_states[i + j * perf_state_count].volt * 1000);
            }

            if (i >= perf_state_count)
                bail("FDT: GPU: Expected %d operating points, but found more\n", perf_state_count);

            if (fdt_setprop_inplace(dt, opp, "opp-microvolt", &volts,
                                    sizeof(u32) * perf_state_table_count))
                bail("FDT: GPU: Failed to set opp-microvolt for PS %d\n", i);

            if (fdt_setprop_inplace_u64(dt, opp, "opp-hz", perf_states[i].freq))
                bail("FDT: GPU: Failed to set opp-hz for PS %d\n", i);

            if (fdt_setprop_inplace_u32(dt, opp, "opp-microwatt", max_pwr[i]))
                bail("FDT: GPU: Failed to set opp-microwatt for PS %d\n", i);

            i++;
        }

        if (i != perf_state_count)
            bail("FDT: GPU: Expected %d operating points, but found %d\n", perf_state_count, i);

        if (has_cs_afr) {
            int ret = fdt_set_aux_opp(dt, gpu, "apple,cs-opp", perf_states_cs, dies);
            if (ret)
                return ret;

            if (fdt_set_float_array(dt, gpu, "apple,cs-leak-coef", cs_leak, dies))
                return -1;
        }

        if (has_cs_afr) {
            int ret = fdt_set_aux_opp(dt, gpu, "apple,afr-opp", perf_states_afr, dies);
            if (ret)
                return ret;
            if (fdt_set_float_array(dt, gpu, "apple,afr-leak-coef", afr_leak, dies))
                return -1;
        }
    }

    if (dt_set_region(dt, sgx, "gfx-handoff", "/reserved-memory/uat-handoff"))
        return -1;
    if (dt_set_region(dt, sgx, "gfx-shared-region", "/reserved-memory/uat-pagetables"))
        return -1;
    if (dt_set_region(dt, sgx, "gpu-region", "/reserved-memory/uat-ttbs"))
        return -1;

    // refresh gpu dt node offset after modifying the dt in dt_set_region()
    gpu = fdt_path_offset(dt, "gpu");
    if (gpu < 0) {
        printf("FDT: GPU: gpu alias not found in device tree\n");
        return 0;
    }

    const struct fw_version_info *compat;

    switch (os_firmware.version) {
        case V12_3_1:
            compat = &fw_versions[V12_3];
            break;
        case V13_5B4:
        case V13_6_1:
            compat = &fw_versions[V13_5];
            break;
        default:
            compat = &os_firmware;
            break;
    }

    if (downstream_dtb) {
        if (firmware_set_fdt(dt, gpu, "apple,firmware-version", &os_firmware))
            return -1;
        if (firmware_set_fdt(dt, gpu, "apple,firmware-compat", compat))
            return -1;
    }
    // Ignoring errors for old dts compat
    firmware_set_fdt(dt, gpu, "apple,firmware-abi", compat);

    size_t data_a_size, data_b_size, globals_size;
    if (rust_gpu_initdata_size(compat->num[0], compat->num[1], &data_a_size, &data_b_size,
                               &globals_size) == -1)
        return -1;

    void *data_a = (void *)top_of_memory_alloc(ALIGN_UP(data_a_size, SZ_16K));
    void *data_b = (void *)top_of_memory_alloc(ALIGN_UP(data_b_size, SZ_16K));
    void *globals = (void *)top_of_memory_alloc(ALIGN_UP(globals_size, SZ_16K));
    memset(data_a, 0, data_a_size);
    memset(data_b, 0, data_b_size);
    memset(globals, 0, globals_size);

    size_t n_perf_states_cs = 0, n_perf_states_afr = 0;
    const struct aux_perf_state *pstate_cs_raw = NULL, *pstate_afr_raw = NULL;

    if (has_cs_afr) {
        n_perf_states_cs = perf_states_cs->count;
        n_perf_states_afr = perf_states_afr->count;
        pstate_cs_raw = perf_states_cs->states;
        pstate_afr_raw = perf_states_afr->states;
    }
    struct initdata_inputs ins = {
        .perf_state_table_count = perf_state_table_count,
        .perf_state_count = perf_state_count,
        .c_perf_states = perf_states,
        .max_pwr = max_pwr,
        .core_leak = core_leak,
        .sram_leak = sram_leak,
        .cs_leak = cs_leak,
        .afr_leak = afr_leak,
        .n_perf_states_cs = n_perf_states_cs,
        .pstates_cs = pstate_cs_raw,
        .n_perf_states_afr = n_perf_states_afr,
        .pstates_afr = pstate_afr_raw,
        .compat_maj = compat->num[0],
        .compat_min = compat->num[1],
    };
    if (rust_fill_gpu_initdata(&ins, data_a, data_b, globals) == -1)
        return -1;

    if (dt_set_resvmem(dt, "/reserved-memory/hw-cal-a", (u64)data_a, ALIGN_UP(data_a_size, SZ_16K)))
        return 0; // Old dts.
    if (dt_set_resvmem(dt, "/reserved-memory/hw-cal-b", (u64)data_b, ALIGN_UP(data_b_size, SZ_16K)))
        return -1;
    if (dt_set_resvmem(dt, "/reserved-memory/globals", (u64)globals,
                       ALIGN_UP(globals_size, SZ_16K)))
        return -1;

    /* Save init data sizes for debugging */
    // refresh gpu dt node offset after modifying the dt in dt_set_region()
    gpu = fdt_path_offset(dt, "gpu");
    if (gpu < 0) {
        printf("FDT: GPU: gpu alias not found in device tree\n");
        return 0;
    }
    fdt_setprop_u32(dt, gpu, "debug,hw-cal-a-size", data_a_size);
    fdt_setprop_u32(dt, gpu, "debug,hw-cal-b-size", data_b_size);
    fdt_setprop_u32(dt, gpu, "debug,globals-size", globals_size);

    return 0;
}
