/* SPDX-License-Identifier: MIT */

#include "kboot.h"
#include "adt.h"
#include "g18g_identity.h"
#include "assert.h"
#include "firmware.h"
#include "malloc.h"
#include "math.h"
#include "pmgr.h"
#include "soc.h"
#include "utils.h"
#include "xnuboot.h"

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
    if (ADT_GETPROP(adt, sgx, prop, &size) < 0 || !size)
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

static int dt_set_gpu_t8140(void *dt)
{
    int gpu = fdt_path_offset(dt, "gpu");
    if (gpu < 0)
        return 0;
    if (fdt_node_check_compatible(dt, gpu, "apple,agx-t8140") &&
        fdt_node_check_compatible(dt, gpu, "gpu,t8140"))
        return 0;

    int sgx = adt_path_offset(adt, "/arm-io/sgx");
    u32 count, num_states, tables, len;
    const struct perf_state *states, *sram;
    if (sgx < 0 || ADT_GETPROP(adt, sgx, "perf-state-count", &count) != sizeof(count) ||
        ADT_GETPROP(adt, sgx, "gpu-num-perf-states", &num_states) != sizeof(num_states) ||
        ADT_GETPROP(adt, sgx, "perf-state-table-count", &tables) != sizeof(tables) || count != 16 ||
        num_states != 15 || tables != 1)
        goto disable;
    states = adt_getprop(adt, sgx, "perf-states", &len);
    if (!states || len != count * sizeof(*states))
        goto disable;
    sram = adt_getprop(adt, sgx, "perf-states-sram", &len);
    if (!sram || len != count * sizeof(*sram))
        goto disable;
    for (u32 i = 0; i < count; i++)
        if (states[i].volt > UINT32_MAX / 1000)
            goto disable;

    int ph_len;
    const fdt32_t *opp_ph = fdt_getprop(dt, gpu, "operating-points-v2", &ph_len);
    if (!opp_ph || ph_len != sizeof(*opp_ph))
        goto disable;
    int table = fdt_node_offset_by_phandle(dt, fdt32_ld(opp_ph));
    if (table < 0)
        goto disable;
    int opp, opp_count = 0;
    fdt_for_each_subnode(opp, dt, table)
    {
        if (++opp_count > 16 || !fdt_getprop(dt, opp, "opp-hz", &ph_len) || ph_len != sizeof(u64) ||
            !fdt_getprop(dt, opp, "opp-microvolt", &ph_len) || ph_len != sizeof(u32))
            goto disable;
    }
    if (opp_count != 16)
        goto disable;

    static const struct {
        const char *name;
        const char *path;
    } regions[] = {
        {"gfx-handoff", "/reserved-memory/uat-handoff"},
        {"gfx-shared-l2-region", "/reserved-memory/uat-l2-pagetables"},
        {"gfx-shared-region", "/reserved-memory/uat-pagetables"},
        {"gpu-region", "/reserved-memory/uat-ttbs"},
    };
    u64 base[ARRAY_SIZE(regions)], size[ARRAY_SIZE(regions)];
    for (size_t i = 0; i < ARRAY_SIZE(regions); i++) {
        char prop[64];
        snprintf(prop, sizeof(prop), "%s-base", regions[i].name);
        if (ADT_GETPROP(adt, sgx, prop, &base[i]) != sizeof(u64))
            goto disable;
        snprintf(prop, sizeof(prop), "%s-size", regions[i].name);
        if (ADT_GETPROP(adt, sgx, prop, &size[i]) != sizeof(u64) || !base[i] || !size[i] ||
            (base[i] & (SZ_16K - 1)) || (size[i] & (SZ_16K - 1)) ||
            base[i] > UINT64_MAX - size[i] || fdt_path_offset(dt, regions[i].path) < 0)
            goto disable;
        for (size_t j = 0; j < i; j++)
            if (base[i] < base[j] + size[j] && base[j] < base[i] + size[i])
                goto disable;
    }

    opp_count = 0;
    fdt_for_each_subnode(opp, dt, table)
    {
        if (fdt_setprop_inplace_u64(dt, opp, "opp-hz", states[opp_count].freq) ||
            fdt_setprop_inplace_u32(dt, opp, "opp-microvolt", states[opp_count].volt * 1000))
            goto disable;
        opp_count++;
    }
    for (size_t i = 0; i < ARRAY_SIZE(regions); i++)
        if (dt_set_resvmem(dt, regions[i].path, base[i], size[i]))
            goto disable;
    return 0;

disable:
    gpu = fdt_path_offset(dt, "gpu");
    if (gpu >= 0 && fdt_setprop_string(dt, gpu, "status", "disabled"))
        return -1;
    printf("FDT: GPU: invalid T8140 ADT or DT data, disabling GPU\n");
    return 0;
}

/*
 * T8152 (J873 Mac mini M6, G18G). Port of the GPU part of aurora-silicon/m1n1-aurora
 * j873 experiments/j873/installer-prep-26A428/j873-linux-boot.py prepare
 * --gpu-firmware-receipt, so a normal boot gets the same device tree without the host
 * loader. Everything is read from this boot's ADT and from DRAM. It performs no MMIO and
 * never starts an ASC. The kernel (t8152-j873-gpu.dtsi) ships the eight reservations and
 * both GFX/GFX1 mailbox providers disabled; this function enables them only as the loader
 * does, and the providers only together with the firmware receipt.
 */
enum t8152_region {
    R_TTBS,
    R_SHARED,
    R_L2,
    R_HANDOFF,
    R_DATA, /* GFX, then GFX1 */
    R_DATA1,
    R_SHARED_RO, /* GFX, then GFX1 */
    R_SHARED_RO1,
    R_COUNT,
};

static const struct {
    const char *name; /* ADT /arm-io/sgx property prefix */
    const char *path; /* FDT /reserved-memory node */
    bool no_map;      /* the four firmware carveouts must stay out of the linear map */
} t8152_regions[R_COUNT] = {
    [R_TTBS] = {"gpu-region", "/reserved-memory/uat-ttbs", false},
    [R_SHARED] = {"gfx-shared-region", "/reserved-memory/uat-pagetables", false},
    [R_L2] = {"gfx-shared-l2-region", "/reserved-memory/uat-l2-pagetables", false},
    [R_HANDOFF] = {"gfx-handoff", "/reserved-memory/uat-handoff", false},
    [R_DATA] = {"gfx-data", "/reserved-memory/gfx-data", true},
    [R_DATA1] = {"gfx1-data", "/reserved-memory/gfx1-data", true},
    [R_SHARED_RO] = {"gfx-data-shared-ro", "/reserved-memory/gfx-data-shared-ro", true},
    [R_SHARED_RO1] = {"gfx1-data-shared-ro", "/reserved-memory/gfx1-data-shared-ro", true},
};

static const char *const t8152_mailboxes[G18G_ROLES] = {"/soc/mailbox@582600000",
                                                          "/soc/mailbox@582e00000"};

/* ADT layout pinned from the 26A428 J873 ADT. A changed layout means another machine or firmware. */
static const struct {
    const char *node;
    const char *nub;
    u64 regs[3][2];
    u64 mailbox;
} t8152_roles[G18G_ROLES] = {
    [G18G_GFX] = {"/arm-io/gfx-asc",
                  "/arm-io/gfx-asc/iop-gfx-nub",
                  {{0x582600000, 0x74000}, {0x582050000, 8}, {0x316020000, 0x10000}},
                  1},
    [G18G_GFX1] = {"/arm-io/gfx1-asc",
                   "/arm-io/gfx1-asc/iop-gfx1-nub",
                   {{0x582e00000, 0x74000}, {0x582850000, 8}, {0x316020000, 0x10000}},
                   2},
};

struct t8152_segment {
    u64 base, virt, phys;
    u32 size, flags;
} PACKED;

struct t8152_gpu {
    u64 base[R_COUNT], size[R_COUNT];
    u64 rtkit_private_vm_base;
    struct {
        u64 wrapper;
        u64 text_base;
        u64 text_size;
    } role[G18G_ROLES];
};

static bool adt_uint(int node, const char *name, u64 *out)
{
    u32 len;
    const void *p = adt_getprop(adt, node, name, &len);

    if (!p)
        return false;
    if (len == sizeof(u32)) {
        u32 v;
        memcpy(&v, p, sizeof(v));
        *out = v;
    } else if (len == sizeof(u64)) {
        memcpy(out, p, sizeof(*out));
    } else {
        return false;
    }
    return true;
}

/* A string property must equal `want`; a trailing NUL is allowed. */
static bool adt_string_is(int node, const char *name, const char *want)
{
    u32 len;
    const char *p = adt_getprop(adt, node, name, &len);
    size_t n = strlen(want);

    return p && (len == n || (len == n + 1 && !p[n])) && !memcmp(p, want, n);
}

#define t8152_fail(...)                                                                            \
    do {                                                                                           \
        printf("ADT: GPU: T8152: " __VA_ARGS__);                                                   \
        return -1;                                                                                 \
    } while (0)

/* Validate the whole GPU layout read-only before anything is changed (inspect_gpu_facts). */
static int t8152_inspect(struct t8152_gpu *g)
{
    u64 dram_base, dram_size, value;
    int chosen = adt_path_offset(adt, "/chosen");
    int sgx = adt_path_offset(adt, "/arm-io/sgx");
    int defaults = adt_path_offset(adt, "/defaults");

    if (chosen < 0 || sgx < 0 || defaults < 0)
        t8152_fail("sgx, defaults or chosen node missing\n");
    if (!adt_uint(chosen, "dram-base", &dram_base) || !adt_uint(chosen, "dram-size", &dram_size) ||
        !dram_base || !dram_size || dram_base > UINT64_MAX - dram_size)
        t8152_fail("invalid DRAM bounds\n");
    if (!adt_string_is(sgx, "compatible", "gpu,t8152"))
        t8152_fail("not the T8152 sgx node\n");
    if (!adt_uint(defaults, "uat-vaddr-size", &value) || value != 43)
        t8152_fail("UAT virtual-address width changed\n");
    if (!adt_uint(sgx, "agx-address-space-mgmt-mode", &value) || value != 1)
        t8152_fail("UAT address-space management mode changed\n");
    static const char *const shared_rw[] = {
        "gfx-data-shared-rw-base", "gfx-data-shared-rw-size", "gfx1-data-shared-rw-base",
        "gfx1-data-shared-rw-size"};
    for (size_t i = 0; i < ARRAY_SIZE(shared_rw); i++)
        if (adt_uint(sgx, shared_rw[i], &value) && value)
            t8152_fail("a shared-rw region appeared; it is not exported\n");
    if (!adt_uint(sgx, "rtkit-private-vm-region-base", &g->rtkit_private_vm_base))
        t8152_fail("rtkit-private-vm-region-base missing\n");

    u64 dram_end = dram_base + dram_size;
    for (int i = 0; i < R_COUNT; i++) {
        char prop[64];

        snprintf(prop, sizeof(prop), "%s-base", t8152_regions[i].name);
        if (!adt_uint(sgx, prop, &g->base[i]))
            t8152_fail("missing %s\n", prop);
        snprintf(prop, sizeof(prop), "%s-size", t8152_regions[i].name);
        if (!adt_uint(sgx, prop, &g->size[i]))
            t8152_fail("missing %s\n", prop);

        u64 base = g->base[i], size = g->size[i];
        if (!base || !size || ((base | size) & (SZ_16K - 1)) || base < dram_base ||
            base > UINT64_MAX - size || base + size > dram_end)
            t8152_fail("invalid boot-local region %s\n", t8152_regions[i].name);
        if (i == R_SHARED && (size < 0x44000 || (size & 0x7fff)))
            t8152_fail("page-table reservation too small or not halvable\n");
        for (int j = 0; j < i; j++)
            if (base < g->base[j] + g->size[j] && g->base[j] < base + size)
                t8152_fail("overlapping reservations %s and %s\n", t8152_regions[i].name,
                           t8152_regions[j].name);
    }

    for (int r = 0; r < G18G_ROLES; r++) {
        int path[8];
        int node = adt_path_offset_trace(adt, t8152_roles[r].node, path);
        int nub = adt_path_offset(adt, t8152_roles[r].nub);
        const struct g18g_role_profile *profile = g18g_profile(r);
        u64 addr, size;

        if (node < 0 || nub < 0)
            t8152_fail("%s node missing\n", t8152_roles[r].node);
        if (!adt_string_is(node, "compatible", "iop,ascwrap-v8"))
            t8152_fail("%s is not an ascwrap-v8 GPU ASC\n", t8152_roles[r].node);
        for (int i = 0; i < 3; i++)
            if (adt_get_reg(adt, path, "reg", i, &addr, &size) < 0 ||
                addr != t8152_roles[r].regs[i][0] || size != t8152_roles[r].regs[i][1])
                t8152_fail("%s register layout changed\n", t8152_roles[r].node);
        if (adt_get_reg(adt, path, "reg", 3, &addr, &size) >= 0)
            t8152_fail("%s has an extra register window\n", t8152_roles[r].node);
        if (!adt_uint(node, "msgbox-mailbox-num", &value) || value != t8152_roles[r].mailbox ||
            !adt_string_is(node, "msgbox-using-postoffice", "true"))
            t8152_fail("%s post-office mailbox changed\n", t8152_roles[r].node);
        if (!adt_string_is(nub, "uuid", profile->uuid))
            t8152_fail("%s is not the pinned B-revision firmware\n", t8152_roles[r].node);
        if (!adt_string_is(node, "segment-names", "__TEXT;__DATA;__DATA_SHARED_RO"))
            t8152_fail("%s firmware segment layout changed\n", t8152_roles[r].node);

        u32 len;
        const struct t8152_segment *seg = adt_getprop(adt, node, "segment-ranges", &len);
        if (!seg || len != 3 * sizeof(*seg))
            t8152_fail("%s segment-ranges length changed\n", t8152_roles[r].node);
        for (int i = 0; i < 3; i++) {
            u64 sbase = seg[i].base, ssize = seg[i].size;
            if (sbase != seg[i].phys || ((sbase | ssize) & (SZ_16K - 1)) || !ssize ||
                sbase < dram_base || sbase > UINT64_MAX - ssize || sbase + ssize > dram_end)
                t8152_fail("%s invalid firmware segment %d\n", t8152_roles[r].node, i);
            /* The loaded firmware must sit wholly in iBoot's reserved low memory. */
            if (sbase + ssize > cur_boot_args.phys_base)
                t8152_fail("%s segment %d is not below Linux RAM\n", t8152_roles[r].node, i);
        }
        if (seg[0].size != profile->text_size)
            t8152_fail("%s TEXT size changed\n", t8152_roles[r].node);
        enum t8152_region data = r == G18G_GFX ? R_DATA : R_DATA1;
        enum t8152_region shared_ro = r == G18G_GFX ? R_SHARED_RO : R_SHARED_RO1;
        if (seg[1].base != g->base[data] || seg[1].size != g->size[data] ||
            seg[2].base != g->base[shared_ro] || seg[2].size != g->size[shared_ro])
            t8152_fail("%s DATA segments disagree with the sgx regions\n", t8152_roles[r].node);
        for (int i = 0; i < R_COUNT; i++)
            if (seg[0].base < g->base[i] + g->size[i] && g->base[i] < seg[0].base + seg[0].size)
                t8152_fail("%s TEXT overlaps %s\n", t8152_roles[r].node, t8152_regions[i].name);

        g->role[r].wrapper = t8152_roles[r].regs[0][0];
        g->role[r].text_base = seg[0].base;
        g->role[r].text_size = seg[0].size;
    }
    return 0;
}

/* The DTB must declare exactly the reservations and providers the loader expects. */
static int t8152_check_dt(void *dt)
{
    int resv = fdt_path_offset(dt, "/reserved-memory");

    if (resv < 0 || fdt_address_cells(dt, resv) != 2 || fdt_size_cells(dt, resv) != 2)
        bail("FDT: GPU: /reserved-memory must use two address and two size cells\n");
    for (int i = 0; i < R_COUNT; i++) {
        int node = fdt_path_offset(dt, t8152_regions[i].path);

        if (node < 0)
            bail("FDT: GPU: missing %s\n", t8152_regions[i].path);
        if (!!fdt_getprop(dt, node, "no-map", NULL) != t8152_regions[i].no_map)
            bail("FDT: GPU: %s must%s be no-map\n", t8152_regions[i].path,
                 t8152_regions[i].no_map ? "" : " not");
    }
    for (int r = 0; r < G18G_ROLES; r++) {
        int node = fdt_path_offset(dt, t8152_mailboxes[r]);
        int len;
        const char *status;

        if (node < 0 || fdt_node_check_compatible(dt, node, "apple,t8152-gfx-ascwrap-v8"))
            bail("FDT: GPU: %s is missing or not a T8152 GFX ASC provider\n", t8152_mailboxes[r]);
        status = fdt_getprop(dt, node, "status", &len);
        if (!status || strcmp(status, "disabled"))
            bail("FDT: GPU: %s must ship disabled\n", t8152_mailboxes[r]);
    }
    return 0;
}

static int dt_set_gpu_t8152(void *dt)
{
    struct t8152_gpu gpu;
    int node = fdt_path_offset(dt, "gpu");

    if (node < 0) {
        printf("FDT: GPU: gpu alias not found in device tree\n");
        return 0;
    }
    if (fdt_node_check_compatible(dt, node, "apple,agx-t8152"))
        return 0;

    if (t8152_check_dt(dt) || t8152_inspect(&gpu))
        goto disable;
    printf("G18G_GPU_ADT_LAYOUT_VERIFIED regions=%d\n", R_COUNT);

    bool qualified = true;
    for (int r = 0; r < G18G_ROLES; r++) {
        struct g18g_loader_expect want;
        char tlv[5] = "";
        const void *text = (const void *)gpu.role[r].text_base;

        g18g_expected_loader(r, gpu.base[R_SHARED], gpu.size[R_SHARED], gpu.base[R_HANDOFF],
                             gpu.base[R_L2], gpu.rtkit_private_vm_base, gpu.role[r].wrapper, chip_id,
                             &want);
        int rc = g18g_qualify_text(r, text, gpu.role[r].text_size, &want, tlv);
        if (rc) {
            printf("ADT: GPU: T8152: firmware role %d rejected: %s %s\n", r,
                   g18g_result_string(rc), tlv);
            qualified = false;
        }
    }

    for (int i = 0; i < R_COUNT; i++) {
        if (dt_set_resvmem(dt, t8152_regions[i].path, gpu.base[i], gpu.size[i]))
            return -1;
        printf("G18G_GPU_REGION %s: 0x%lx + 0x%lx\n", t8152_regions[i].name, gpu.base[i],
               gpu.size[i]);
    }
    printf("G18G_GPU_REGIONS_WRITTEN regions=%d\n", R_COUNT);

    if (!qualified) {
        printf("G18G_GPU_FIRMWARE_REJECTED mailboxes=disabled receipt=absent\n");
        return 0;
    }
    printf("G18G_GPU_FIRMWARE_TEXT_QUALIFIED roles=2 runtime_admitted=no\n");

    for (int r = 0; r < G18G_ROLES; r++) {
        int mailbox = fdt_path_offset(dt, t8152_mailboxes[r]);

        if (mailbox < 0 || fdt_setprop_string(dt, mailbox, "status", "okay"))
            bail("FDT: GPU: failed to enable %s\n", t8152_mailboxes[r]);
    }
    printf("G18G_GPU_MAILBOXES_ENABLED providers=2\n");

    int chosen = fdt_path_offset(dt, "/chosen");
    if (chosen < 0 || fdt_setprop_string(dt, chosen, G18G_CHOSEN_PROPERTY, G18G_PROFILE))
        bail("FDT: GPU: failed to set /chosen %s\n", G18G_CHOSEN_PROPERTY);
    printf("G18G_GPU_FIRMWARE_RECEIPT_SET %s %s\n", G18G_CHOSEN_PROPERTY, G18G_PROFILE);
    return 0;

disable:
    node = fdt_path_offset(dt, "gpu");
    if (node >= 0 && fdt_setprop_string(dt, node, "status", "disabled"))
        return -1;
    printf("FDT: GPU: invalid T8152 ADT or DT data, disabling GPU\n");
    return 0;
}

int dt_set_gpu(void *dt)
{
    if (chip_id == T8140)
        return dt_set_gpu_t8140(dt);
    if (chip_id == T8152)
        return dt_set_gpu_t8152(dt);

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
