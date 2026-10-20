/* SPDX-License-Identifier: MIT */

#include "../config.h"

#include "adt.h"
#include "afk.h"
#include "dcp.h"
#include "firmware.h"
#include "malloc.h"
#include "pmgr.h"
#include "rtkit.h"
#include "smc.h"
#include "string.h"
#include "utils.h"

#include "dcp/dptx_phy.h"

static char dcp_pmgr_dev[16] = "DISP0_CPU0";
static u32 dcp_die;

/* Read over the proxy after the SMC drops the debug link (M6 bring-up).
 * [0] magic 0x4d36d1a6, [1] smc open (1 ok, 2 fail), [2..4] dp2hdmi ret/old/readback,
 * [5..7] hdmi ret/old/readback, [8] DCP hpd result. */
u32 m6_diag[9];

/* An SMC power function from the ADT. M2-era {phandle, fourcc, key, unk}: plain u32 write of
 * 0x800001. T8152 smc-pmu "pKW8" {phandle, fourcc, key, w0, w1}: an 8-byte key write whose two
 * words are the ADT arguments as given (live 2026-10-19: pcIO is 8 bytes and reads all zero). */
static int dcp_smc_function_on(smc_dev_t *smc, int node, const char *prop, u32 *key_out)
{
    u32 len = 0;
    const u32 *f = adt_getprop(adt, node, prop, &len);
    if (!f || len < 12) {
        printf("dcp: failed to get %s\n", prop);
        return -1;
    }
    u32 key = f[2];
    *key_out = key;
    if (len < 20)
        return smc_write_u32(smc, key, 0x800001);

    u32 *d = &m6_diag[strcmp(prop, "function-dp2hdmi_pwr_en") ? 5 : 2];
    u32 payload[2] = {f[3], f[4]}, pre[2] = {0}, post[2] = {0};
    int rpre = smc_read_u64(smc, key, pre);
    int ret = smc_write(smc, key, payload, sizeof(payload));
    int rpost = smc_read_u64(smc, key, post);
    d[0] = ret;
    d[1] = pre[0];
    d[2] = post[0];
    printf("dcp: %s key %08x pre {%08x,%08x}(%d) write8 {%08x,%08x} -> %d post {%08x,%08x}(%d)\n",
           prop, key, pre[0], pre[1], rpre, payload[0], payload[1], ret, post[0], post[1], rpost);
    return ret;
}

int dcp_dp2hdmi_power_on(dcp_dev_t *dcp, const char *path)
{
    int node = adt_path_offset(adt, path);
    if (node < 0) {
        printf("dcp: failed to find dp2hdmi-gpio node '%s'\n", path);
        return -1;
    }

    m6_diag[0] = 0x4d36d1a6;
    printf("dcp: opening SMC (boot)\n");
    smc_dev_t *smc = smc_init();
    m6_diag[1] = smc ? 1 : 2;
    if (!smc) {
        printf("dcp: SMC init failed; dp2hdmi stays off\n");
        return -1;
    }
    int r1 = dcp_smc_function_on(smc, node, "function-dp2hdmi_pwr_en", &dcp->dp2hdmi_pwr_gpio);
    int r2 = dcp_smc_function_on(smc, node, "function-hdmi_pwr_en", &dcp->hdmi_pwr_gpio);
    {
        /* Experiment: key info, then the ADT words with an "on" bit in byte 0, pin byte kept. */
        u32 ki[2] = {0};
        int ri = smc_key_info(smc, 0x7063494f, ki);
        printf("dcp: pcIO keyinfo -> %d: %08x %08x\n", ri, ki[0], ki[1]);
        static const char *fns[] = {"function-dp2hdmi_pwr_en", "function-hdmi_pwr_en"};
        for (int i = 0; i < 2; i++) {
            const u32 *f = adt_getprop(adt, node, fns[i], NULL);
            u32 w[2] = {f[3] | 1, f[4]};
            int rw = smc_write(smc, 0x7063494f, w, sizeof(w));
            printf("dcp: %s on-bit write {%08x,%08x} -> %d\n", fns[i], w[0], w[1], rw);
        }
    }
    smc_shutdown(smc);
    /* Give the HDMI converter time to boot before DCP probes the sink. */
    mdelay(1500);
    return (r1 || r2) ? -1 : 0;
}

static int dcp_hdmi_dptx_init(dcp_dev_t *dcp, const display_config_t *cfg)
{
    if (dcp_dp2hdmi_power_on(dcp, cfg->dp2hdmi_gpio) < 0 && !dcp->dp2hdmi_pwr_gpio)
        return -1;

    dcp->die = cfg->die;

    dcp->phy = dptx_phy_init(cfg->dptx_phy, cfg->dcp_index);
    if (!dcp->phy) {
        printf("dcp: failed to init (lp)dptx-phy '%s'\n", cfg->dptx_phy);
        return -1;
    }

    dcp->dpav_ep = dcp_dpav_init(dcp);
    if (!dcp->dpav_ep) {
        printf("dcp: failed to initialize dpav endpoint\n");
        return -1;
    }

    dcp->dptx_ep = dcp_dptx_init(dcp, cfg->num_dptxports);
    if (!dcp->dptx_ep) {
        printf("dcp: failed to initialize dptx-port endpoint\n");
        dcp_dpav_shutdown(dcp->dpav_ep);
        return -1;
    }

#ifdef RTKIT_SYSLOG
    // start system endpoint when extended logging is requested
    dcp->system_ep = dcp_system_init(dcp);
    if (!dcp->system_ep) {
        printf("dcp: failed to initialize system endpoint\n");
        dcp_dptx_shutdown(dcp->dptx_ep);
        dcp_dpav_shutdown(dcp->dpav_ep);
        return -1;
    }

    dcp_system_set_property_u64(dcp->system_ep, "gAFKConfigLogMask", 0xffff);
#endif

    return 0;
}

int dcp_connect_dptx(dcp_dev_t *dcp)
{
    if (dcp->dptx_ep && dcp->phy) {
        return dcp_dptx_connect(dcp->dptx_ep, dcp->phy, dcp->die, 0);
    }

    return 0;
}

int dcp_work(dcp_dev_t *dcp)
{
    return afk_epic_work(dcp->afk, -1);
}

static int dcp_create_firmware_mappings(const display_config_t *cfg, dcp_dev_t *dcp)
{
    int created = 0;
    int dcp_node = adt_path_offset(adt, cfg->dcp);
    int node = adt_first_child_offset(adt, dcp_node);
    if (node < 0) {
        printf("dcp: iop-dcp*-nub not found!\n");
        return -1;
    }

    u64 asc_dram_mask;
    if (ADT_GETPROP(adt, node, "asc-dram-mask", &asc_dram_mask) < 0)
        asc_dram_mask = 0;

    const struct adt_segment_ranges *seg;
    u32 segments_len;

    seg = adt_getprop(adt, node, "segment-ranges", &segments_len);
    unsigned int count = segments_len / sizeof(*seg);

    for (unsigned int i = 0; i < count; i++) {
        if (seg[i].unk & BIT(1))
            continue;
        u64 iova = seg[i].remap & ~asc_dram_mask;
        size_t len = ALIGN_UP(seg[i].size, SZ_16K);
        bool mapped = false, missing = false;
        for (size_t off = 0; off < len; off += SZ_16K) {
            uintptr_t phys = (uintptr_t)dart_translate_silent(dcp->dart_dcp, iova + off);
            if (!phys) {
                missing = true;
            } else if (phys != seg[i].phys + off) {
                printf("dcp: Conflicting firmware mapping for segment #%u at %lx\n", i,
                       iova + off);
                return -1;
            } else {
                mapped = true;
            }
        }
        if (!missing)
            continue;
        if (mapped) {
            printf("dcp: Incomplete firmware mapping for segment #%u\n", i);
            return -1;
        }
        u32 flags = i == 0 ? 0b0100 : 0; // TEXT gets this bit set?
        printf("dcp: Mapping segment #%u %lx -> %lx [%lx]\n", i, iova, seg[i].phys, len);
        if (dart_map_flags(dcp->dart_dcp, iova, (void *)seg[i].phys, len, flags)) {
            printf("dcp: Failed to map segment\n");
            return -1;
        }
        created++;
    }

    return created;
}

dcp_dev_t *dcp_init(const display_config_t *cfg)
{
    u32 sid;

    if (cfg && cfg->dptx_phy[0]) {
        if (os_firmware.version != V13_5) {
            printf("dcp: dtpx-port is only supported with V13_5 OS firmware.\n");
            return NULL;
        }

        strncpy(dcp_pmgr_dev, cfg->pmgr_dev, sizeof(dcp_pmgr_dev));
        dcp_die = cfg->die;
        pmgr_adt_power_enable(cfg->dcp);
        pmgr_adt_power_enable(cfg->dptx_phy);
        mdelay(25);
    }

    int dart_node = adt_path_offset(adt, cfg->dcp_dart);
    int node = adt_first_child_offset(adt, dart_node);
    if (node < 0) {
        printf("dcp: mapper-dcp* not found!\n");
        return NULL;
    }
    if (ADT_GETPROP(adt, node, "reg", &sid) < 0) {
        printf("dcp: failed to read dart stream ID!\n");
        return NULL;
    }

    dcp_dev_t *dcp = calloc(1, sizeof(dcp_dev_t));
    if (!dcp)
        return NULL;

    dcp->dart_dcp = dart_init_adt(cfg->dcp_dart, 0, sid, true);
    if (!dcp->dart_dcp) {
        printf("dcp: failed to initialize DCP DART\n");
        goto out_free;
    }
    u64 vm_base = dart_vm_base(dcp->dart_dcp);
    dart_setup_pt_region(dcp->dart_dcp, cfg->dcp_dart, sid, vm_base);

    dcp->dart_disp = dart_init_adt(cfg->disp_dart, 0, 0, true);
    if (!dcp->dart_disp) {
        printf("dcp: failed to initialize DISP DART\n");
        goto out_dart_dcp;
    }
    // set disp0's page tables at dart-dcp's vm-base
    dart_setup_pt_region(dcp->dart_disp, cfg->disp_dart, 0, vm_base);

    dcp->iovad_dcp = iovad_init(vm_base + 0x10000000, vm_base + 0x20000000);

    int ret = dcp_create_firmware_mappings(cfg, dcp);
    if (ret < 0) {
        printf("dcp: failed to create firmware mappings\n");
        goto out_iovad;
    }
    if (ret > 0) {
        pmgr_reset(dcp_die, dcp_pmgr_dev);
    }

    dcp->asc = asc_init(cfg->dcp);
    if (!dcp->asc) {
        printf("dcp: failed to initialize ASC\n");
        goto out_iovad;
    }

    dcp->rtkit = rtkit_init("dcp", dcp->asc, dcp->dart_dcp, dcp->iovad_dcp, NULL, false);
    if (!dcp->rtkit) {
        printf("dcp: failed to initialize RTKit\n");
        goto out_iovad;
    }

    /* Keep unqualified ASCWrap-v8 firmware from trapping startup in a wait. */
    bool booted = asc_is_v8(dcp->asc) ? rtkit_boot_timed(dcp->rtkit, 2000000)
                                       : rtkit_boot(dcp->rtkit);
    if (!booted) {
        printf("dcp: failed to boot RTKit\n");
        goto out_iovad;
    }

    dcp->afk = afk_epic_init(dcp->rtkit);
    if (!dcp->afk) {
        printf("dcp: failed to initialize AFK\n");
        goto out_rtkit;
    }

    if (cfg && cfg->dptx_phy[0]) {
        int ret = dcp_hdmi_dptx_init(dcp, cfg);
        if (ret < 0)
            goto out_afk;
    }

    return dcp;

out_afk:
    afk_epic_shutdown(dcp->afk);
out_rtkit:
    rtkit_quiesce(dcp->rtkit);
    rtkit_free(dcp->rtkit);
out_iovad:
    iovad_shutdown(dcp->iovad_dcp, dcp->dart_dcp);
    dart_shutdown(dcp->dart_disp);
out_dart_dcp:
    dart_shutdown(dcp->dart_dcp);
out_free:
    free(dcp);
    return NULL;
}

int dcp_shutdown(dcp_dev_t *dcp, bool sleep)
{
    /* The ASCWrap-v8 sleep path needs a PMGR2 reset, which is not implemented
     * yet. Quiescing has a working RTKit wake/renegotiation path on T8152. */
    if (sleep && asc_is_v8(dcp->asc)) {
        printf("dcp: using quiesce instead of sleep without PMGR2 reset\n");
        sleep = false;
    }
    /* dcp/dcp0 on desktop M2 and M2 Pro/Max devices do not wake from sleep */
    dcp_system_shutdown(dcp->system_ep);
    dcp_dptx_shutdown(dcp->dptx_ep);
    dcp_dpav_shutdown(dcp->dpav_ep);
    free(dcp->phy);
    afk_epic_shutdown(dcp->afk);
    if (sleep) {
        rtkit_sleep(dcp->rtkit);
        pmgr_reset(dcp_die, dcp_pmgr_dev);
    } else {
        rtkit_quiesce(dcp->rtkit);
    }
    rtkit_free(dcp->rtkit);
    dart_shutdown(dcp->dart_disp);
    iovad_shutdown(dcp->iovad_dcp, dcp->dart_dcp);
    dart_shutdown(dcp->dart_dcp);
    free(dcp);

    return 0;
}
