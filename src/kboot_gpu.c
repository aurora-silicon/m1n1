/* SPDX-License-Identifier: MIT */

#include "kboot.h"
#include "adt.h"
#include "assert.h"
#include "exception.h"
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
 * T6040 (M4 Pro).
 *
 * Only part of the GPU's per-unit data is in the ADT. The operating points (one voltage per
 * cluster), the CS and AFR tables and the UAT/firmware regions are; the leakage coefficients
 * come from an efuse page, the core mask and the two performance-state maps (A and B) from
 * SGX registers, and the maximum power and the relative tables are computed from all of it.
 *
 * The fill runs only when the device tree enables the GPU, so a tree that keeps the GPU off
 * leaves the hardware untouched. Anything unexpected disables the GPU node again.
 */
#define T6040_OPPS     16
#define T6040_CLUSTERS 2
#define T6040_CORES    10
#define T6040_STATES   11
#define T6040_EFUSE    0x508374c00UL

/* Rounding barrier: keeps the compiler from fusing a multiply into a following add. */
#define T6040_ROUND(x) __asm__("" : "+w"(x))

static float t6040_f32(u32 w)
{
    float f;
    memcpy(&f, &w, sizeof(f));
    return f;
}

static double t6040_f64(u64 w)
{
    double d;
    memcpy(&d, &w, sizeof(d));
    return d;
}

/* Binary32 words (A, B, C) of the GPU cluster power model, by voltage band (< 701, < 901, more). */
static const u32 t6040_gpu_coef[3][3] = {
    {0x3c44f3b6, 0x3aa07f54, 0x406dcd8c},
    {0x3c367d9a, 0x3abd2472, 0x40671cc1},
    {0x3b9b602e, 0x3b46b49e, 0x4037a220},
};

/* Binary32 words (A, B, T) of the CS power model, by the same voltage bands. */
static const u32 t6040_cs_coef[3][3] = {
    {0x3c21eaa8, 0x3a97a9b9, 0x3d121894},
    {0x3c116cb8, 0x3abe814d, 0x3d0d151a},
    {0x3b4d1eb1, 0x3b66dc36, 0x3ccd6ba8},
};

#define T6040_W_BASE     0x402df854 /* static exponent base */
#define T6040_W_GPU_DYN  0x41769ba6
#define T6040_W_EXPONENT 0x3f99999a
#define T6040_W_CAP_MA   0x472be000 /* 44000 mA */
#define T6040_W_110      0x42dc0000
#define T6040_D_CS_DYN   0x401bc7ae20000000ULL
#define T6040_D_EXPONENT 0x3ff3333340000000ULL

/* ln(x) for 0.25 < x < 4, by the atanh series. */
static double t6040_log(double x)
{
    double t = (x - 1.0) / (x + 1.0), t2 = t * t, term = t, sum = 0.0;

    for (int k = 1; k < 200; k += 2) {
        double add = term / k;
        sum += add;
        if (__builtin_fabs(add) <= __builtin_fabs(sum) * 1e-20)
            break;
        term *= t2;
    }
    return 2.0 * sum;
}

/* exp(y) for |y| < 40: y = k ln2 + r, Taylor series for exp(r), then scale by 2^k. */
static double t6040_exp(double y)
{
    const double ln2 = 0.69314718055994530942;
    int k = (int)(y / ln2 + (y >= 0 ? 0.5 : -0.5));
    double r = y - k * ln2, term = 1.0, sum = 1.0;

    for (int n = 1; n < 40; n++) {
        term *= r / n;
        sum += term;
        if (__builtin_fabs(term) <= 1e-20 * sum)
            break;
    }
    for (; k > 0; k--)
        sum *= 2.0;
    for (; k < 0; k++)
        sum *= 0.5;
    return sum;
}

static int t6040_band(s32 mv)
{
    return mv < 701 ? 0 : (mv < 901 ? 1 : 2);
}

/* Volts as binary32 and the integer mV that the piecewise tests and the exponent use. */
static float t6040_volts(u32 mv, s32 *m)
{
    float v = (float)mv / 1000.0f;
    T6040_ROUND(v);
    float k = v * 1000.0f;
    T6040_ROUND(k);
    *m = (s32)k;
    return v;
}

static u32 t6040_cluster_mw(u32 mv, u32 f_mhz, float leak, float active)
{
    s32 m;
    float v = t6040_volts(mv, &m);
    const u32 *k = t6040_gpu_coef[t6040_band(m)];

    float la = leak * t6040_f32(k[0]);
    T6040_ROUND(la);
    float bm = t6040_f32(k[1]) * (float)m;
    T6040_ROUND(bm);
    float x = bm + t6040_f32(k[2]);
    T6040_ROUND(x);
    float i_static = la * powf(t6040_f32(T6040_W_BASE), x);
    T6040_ROUND(i_static);

    float d = powf(v, t6040_f32(T6040_W_EXPONENT)) * t6040_f32(T6040_W_GPU_DYN);
    T6040_ROUND(d);
    d = d * (float)f_mhz;
    T6040_ROUND(d);
    d = d * active;
    T6040_ROUND(d);

    float i = i_static + d;
    T6040_ROUND(i);
    if (i > t6040_f32(T6040_W_CAP_MA))
        i = t6040_f32(T6040_W_CAP_MA);
    float p = v * i;
    T6040_ROUND(p);
    return (u32)p;
}

static u32 t6040_cs_mw(u32 mv, u32 f_mhz, float leak)
{
    s32 m;
    float v = t6040_volts(mv, &m);
    const u32 *k = t6040_cs_coef[t6040_band(m)];

    float la = leak * t6040_f32(k[0]);
    T6040_ROUND(la);
    float bm = t6040_f32(k[1]) * (float)m;
    T6040_ROUND(bm);
    float x = __builtin_fmaf(t6040_f32(k[2]), t6040_f32(T6040_W_110), bm);
    float i_static = la * powf(t6040_f32(T6040_W_BASE), x);
    T6040_ROUND(i_static);

    double d = t6040_exp(t6040_f64(T6040_D_EXPONENT) * t6040_log((double)v));
    d = d * t6040_f64(T6040_D_CS_DYN);
    T6040_ROUND(d);
    d = d * (double)f_mhz;
    T6040_ROUND(d);
    float i_dynamic = (float)d;

    float i = i_static + i_dynamic;
    T6040_ROUND(i);
    if (i > t6040_f32(T6040_W_CAP_MA))
        i = t6040_f32(T6040_W_CAP_MA);
    float p = v * i;
    T6040_ROUND(p);
    return (u32)p;
}

static int t6040_read32(u64 addr, u32 *val)
{
    int before = exc_count;

    exc_guard = GUARD_MARK | GUARD_SILENT;
    *val = read32(addr);
    sysop("dsb sy");
    sysop("isb");
    exc_guard = GUARD_OFF;
    return exc_count == before ? 0 : -1;
}

/* Performance-state map: two 4-bit operating-point indices per 32-bit word. */
static int t6040_perf_map(u64 base, u32 *index)
{
    u32 n = 0;

    for (u32 i = 0; i < 16; i += 2) {
        u32 w;
        if (t6040_read32(base + 2 * i, &w))
            return -1;
        index[i] = w & 0xf;
        index[i + 1] = (w >> 16) & 0xf;
    }
    for (u32 i = 1; i < 16; i++)
        if (index[i])
            n = i + 1;
    if (n != T6040_STATES || index[0] != 0)
        return -1;
    for (u32 i = 2; i < n; i++)
        if (index[i] <= index[i - 1])
            return -1;
    return n;
}

static int t6040_set_u32s(void *dt, int node, const char *name, const u32 *val, u32 count)
{
    fdt32_t data[16];

    if (count > ARRAY_SIZE(data))
        return -1;
    for (u32 i = 0; i < count; i++)
        data[i] = cpu_to_fdt32(val[i]);
    return fdt_setprop(dt, node, name, data, count * sizeof(u32));
}

static int t6040_set_floats(void *dt, int node, const char *name, const float *val, u32 count)
{
    u32 words[16];

    if (count > ARRAY_SIZE(words))
        return -1;
    memcpy(words, val, count * sizeof(float));
    return t6040_set_u32s(dt, node, name, words, count);
}

static int dt_set_gpu_t6040(void *dt)
{
    int gpu = fdt_path_offset(dt, "gpu");
    if (gpu < 0)
        return 0;
    if (fdt_node_check_compatible(dt, gpu, "apple,agx-t6040"))
        return 0;

    const char *status = fdt_getprop(dt, gpu, "status", NULL);
    if (status && strcmp(status, "okay") && strcmp(status, "ok")) {
        printf("FDT: GPU: T6040 GPU is disabled in the device tree, not filling it\n");
        return 0;
    }

    int sgx_path[8];
    int sgx = adt_path_offset_trace(adt, "/arm-io/sgx", sgx_path);
    u32 count, tables, len;
    u64 sgx_base;
    if (sgx < 0 || adt_get_reg(adt, sgx_path, "reg", 0, &sgx_base, NULL) < 0 ||
        ADT_GETPROP(adt, sgx, "perf-state-count", &count) != sizeof(count) ||
        ADT_GETPROP(adt, sgx, "perf-state-table-count", &tables) != sizeof(tables) ||
        count != T6040_OPPS || tables != T6040_CLUSTERS)
        goto disable;

    const struct perf_state *states = adt_getprop(adt, sgx, "perf-states", &len);
    if (!states || len != count * tables * sizeof(*states))
        goto disable;
    for (u32 j = 0; j < tables; j++)
        for (u32 i = 1; i < count; i++)
            if (!states[j * count + i].freq || states[j * count + i].freq != states[i].freq ||
                states[j * count + i].volt < 400 || states[j * count + i].volt > 1300)
                goto disable;

    const struct aux_perf_states *aux[2];
    const char *aux_name[2] = {"cs-perf-states", "afr-perf-states"};
    for (int j = 0; j < 2; j++) {
        aux[j] = adt_getprop(adt, sgx, aux_name[j], &len);
        if (!aux[j] || len < sizeof(*aux[j]) || aux[j]->dies != 1 || aux[j]->count < 2 ||
            aux[j]->count > MAX_PSTATES ||
            len < sizeof(*aux[j]) + aux[j]->count * sizeof(aux[j]->states[0]))
            goto disable;
        for (u32 i = 0; i < aux[j]->count; i++)
            if (!aux[j]->states[i].freq || aux[j]->states[i].volt < 400000 ||
                aux[j]->states[i].volt > 1300000)
                goto disable;
    }

    u32 base_pstate = 1;
    ADT_GETPROP(adt, sgx, "gpu-perf-base-pstate", &base_pstate);
    if (base_pstate >= T6040_STATES)
        goto disable;

    /* Leakage from the efuse page: two cluster cores, the shared SRAM, CS and AFR. */
    u32 ef[5];
    const u32 ef_off[5] = {0xc10, 0xc1c, 0xc20, 0xc24, 0xc28};
    for (int i = 0; i < 5; i++)
        if (t6040_read32(T6040_EFUSE + ef_off[i], &ef[i]))
            goto disable;
    u64 sram_word = ((u64)ef[3] << 32) | ef[2];
    const u32 raw[5] = {(ef[0] >> 1) & 0x3fff, (ef[0] >> 15) & 0x3fff, (sram_word >> 24) & 0x3ff,
                        (ef[1] >> 9) & 0x1fff, (ef[4] >> 4) & 0xfff};
    const u32 raw_max[5] = {0x3fff, 0x3fff, 0x3ff, 0x1fff, 0xfff};
    for (int i = 0; i < 5; i++)
        if (raw[i] == 0 || raw[i] == raw_max[i])
            goto disable;

    float core_leak[2] = {(float)(raw[0] + 1) * 1.5f, (float)(raw[1] + 1) * 1.5f};
    float sram = (float)(raw[2] + 1) * 3.0f;
    float sram_leak[2] = {sram, sram};
    float cs_leak = (float)(raw[3] + 1);
    float afr_leak = (float)(raw[4] + 1) * 1.5f;

    /* Core mask and the A/B maps are SGX registers: the GPU must be powered to read them. */
    if (pmgr_adt_power_enable("/arm-io/sgx") < 0)
        goto disable;

    u32 masks[3];
    for (int i = 0; i < 3; i++)
        if (t6040_read32(sgx_base + 0xe01500 + 4 * i, &masks[i]))
            goto disable;
    if (masks[1] || masks[2] || (masks[0] >> (T6040_CLUSTERS * T6040_CORES)))
        goto disable;
    u32 active[T6040_CLUSTERS];
    for (int c = 0; c < T6040_CLUSTERS; c++) {
        active[c] = __builtin_popcount((masks[0] >> (T6040_CORES * c)) & MASK(T6040_CORES));
        if (!active[c])
            goto disable;
    }

    u32 index_a[16], index_b[16];
    int n = t6040_perf_map(sgx_base + 0xe01400, index_a);
    if (n < 0 || t6040_perf_map(sgx_base + 0xe01480, index_b) != n)
        goto disable;

    /* Maximum power of each logical A state, and the relative tables. */
    u32 power[T6040_STATES] = {0}, freq[T6040_STATES] = {0};
    for (int i = 0; i < n; i++)
        freq[i] = states[index_a[i]].freq / 1000000;
    for (int i = 1; i < n; i++) {
        const struct perf_state *s = &states[index_a[i]];
        u32 c = min((u32)i, (u32)aux[0]->count - 1);
        u32 cs =
            t6040_cs_mw(aux[0]->states[c].volt / 1000, aux[0]->states[c].freq / 1000000, cs_leak);
        for (int j = 0; j < T6040_CLUSTERS; j++) {
            float r = (float)active[j] / 10.0f;
            power[i] += t6040_cluster_mw(s[j * count].volt, s->freq / 1000000, core_leak[j], r) +
                        cs / T6040_CLUSTERS;
        }
    }

    u32 rel_a[T6040_STATES], rel_b[T6040_STATES];
    u64 span_p = power[n - 1] - power[0], span_f = freq[n - 1] - freq[base_pstate];
    if (!span_p || !span_f)
        goto disable;
    for (int i = 0; i < n; i++) {
        rel_a[i] = (u32)(100ULL * (power[i] - power[0]) / span_p);
        rel_b[i] =
            i < (int)base_pstate ? 0 : (u32)(100ULL * (freq[i] - freq[base_pstate]) / span_f);
    }

    /* Operating points, in the order of the ADT table, one voltage per cluster. */
    int ph_len;
    const fdt32_t *opp_ph = fdt_getprop(dt, gpu, "operating-points-v2", &ph_len);
    if (!opp_ph || ph_len != sizeof(*opp_ph))
        goto disable;
    int table = fdt_node_offset_by_phandle(dt, fdt32_ld(opp_ph));
    if (table < 0)
        goto disable;
    int opp;
    u32 i = 0;
    fdt_for_each_subnode(opp, dt, table)
    {
        fdt32_t volts[T6040_CLUSTERS];
        if (i >= count)
            goto disable;
        for (u32 j = 0; j < T6040_CLUSTERS; j++)
            volts[j] = cpu_to_fdt32(states[i + j * count].volt * 1000);
        if (fdt_setprop_inplace(dt, opp, "opp-microvolt", volts, sizeof(volts)) ||
            fdt_setprop_inplace_u64(dt, opp, "opp-hz", states[i].freq))
            goto disable;
        i++;
    }
    if (i != count)
        goto disable;
    if (fdt_set_aux_opp(dt, gpu, "apple,cs-opp", aux[0], 1) ||
        fdt_set_aux_opp(dt, gpu, "apple,afr-opp", aux[1], 1))
        goto disable;

    if (dt_set_region(dt, sgx, "gfx-handoff", "/reserved-memory/uat-handoff") ||
        dt_set_region(dt, sgx, "gfx-shared-l2-region", "/reserved-memory/uat-l2-pagetables") ||
        dt_set_region(dt, sgx, "gfx-shared-region", "/reserved-memory/uat-pagetables") ||
        dt_set_region(dt, sgx, "gpu-region", "/reserved-memory/uat-ttbs") ||
        dt_set_region(dt, sgx, "gfx-data", "/reserved-memory/gpu-fw-data"))
        goto disable;

    gpu = fdt_path_offset(dt, "gpu");
    if (gpu < 0)
        return -1;
    if (firmware_set_fdt(dt, gpu, "apple,firmware-version", &os_firmware) ||
        t6040_set_floats(dt, gpu, "apple,core-leak-coef", core_leak, T6040_CLUSTERS) ||
        t6040_set_floats(dt, gpu, "apple,sram-leak-coef", sram_leak, T6040_CLUSTERS) ||
        t6040_set_floats(dt, gpu, "apple,cs-leak-coef", &cs_leak, 1) ||
        t6040_set_floats(dt, gpu, "apple,afr-leak-coef", &afr_leak, 1) ||
        t6040_set_u32s(dt, gpu, "apple,perf-map-a", index_a, n) ||
        t6040_set_u32s(dt, gpu, "apple,perf-map-b", index_b, n) ||
        t6040_set_u32s(dt, gpu, "apple,perf-relative-a", rel_a, n) ||
        t6040_set_u32s(dt, gpu, "apple,perf-relative-b", rel_b, n) ||
        fdt_setprop_u32(dt, gpu, "apple,perf-base-pstate", base_pstate) ||
        fdt_setprop_u32(dt, gpu, "apple,max-power-mw", power[n - 1]))
        goto disable;

    printf("FDT: GPU: T6040 cores %u+%u, max power %u mW, OS firmware %s\n", active[0], active[1],
           power[n - 1], os_firmware.string);
    return 0;

disable:
    gpu = fdt_path_offset(dt, "gpu");
    if (gpu >= 0 && fdt_setprop_string(dt, gpu, "status", "disabled"))
        return -1;
    printf("FDT: GPU: invalid T6040 ADT, fuse or SGX data, disabling GPU\n");
    return 0;
}

int dt_set_gpu_m4(void *dt);

int dt_set_gpu(void *dt)
{
    if (chip_id == T8132)
        return dt_set_gpu_m4(dt);

    if (chip_id == T8140)
        return dt_set_gpu_t8140(dt);

    if (chip_id == T6040)
        return dt_set_gpu_t6040(dt);

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
