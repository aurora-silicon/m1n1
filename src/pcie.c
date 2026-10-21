/* SPDX-License-Identifier: MIT */

#include "adt.h"
#include "exception.h"
#include "pcie.h"
#include "pmgr.h"
#include "smc.h"
#include "string.h"
#include "tunables.h"
#include "utils.h"

/*
 * Everything the console prints during pcie_init() (pcie:, pmgr:, SMC: ...) is kept here and published by kboot as
 * /chosen "m1n1,pcie-log" (a NUL-separated string list), because the m1n1 console is not on screen when Linux runs.
 */
#define PCIE_LOG_SIZE 12288
static char pcie_log_buf[PCIE_LOG_SIZE];
static u32 pcie_log_len;

const char *pcie_log_get(u32 *len)
{
    *len = pcie_log_len;
    return pcie_log_buf;
}

/*
 * The ADT uses 17 register sets:
 *
 * 0:  90000000 00000006 10000000 00000000  ECAM
 * 1:  80000000 00000006 00040000 00000000  RC
 * 2:  80080000 00000006 00090000 00000000  PHY
 * 3:  800c0000 00000006 00020000 00000000  PHY IP
 * 4:  8c000000 00000006 00004000 00000000  AXI
 * 5:  3d2bc000 00000000 00001000 00000000  fuses
 * 6:  81000000 00000006 00008000 00000000  port 0 config
 * 7:  81010000 00000006 00001000 00000000  port 0 LTSSM debug
 * 8:  80084000 00000006 00004000 00000000  port 0 PHY
 * 9:  800c8000 00000006 00016610 00000000  port 0 PHY IP
   <macOS 12.0 RC and later add a per-port Intr2AXI reg here>
 * 10: 82000000 00000006 00008000 00000000  port 1 config
 * 11: 82010000 00000006 00001000 00000000  port 1 LTSSM debug
 * 12: 80088000 00000006 00004000 00000000  port 1 PHY
 * 13: 800d0000 00000006 00006000 00000000  port 1 PHY IP
   <...>
 * 14: 83000000 00000006 00008000 00000000  port 2 config
 * 15: 83010000 00000006 00001000 00000000  port 2 LTSSM debug
 * 16: 8008c000 00000006 00004000 00000000  port 2 PHY
 * 17: 800d8000 00000006 00006000 00000000  port 2 PHY IP
   <...>
 */

/* PHY registers */

#define APCIE_PHY_CTRL             0x000
#define APCIE_PHY_CTRL_CLK0REQ     BIT(0)
#define APCIE_PHY_CTRL_CLK1REQ     BIT(1)
#define APCIE_PHY_CTRL_CLK0ACK     BIT(2)
#define APCIE_PHY_CTRL_CLK1ACK     BIT(3)
#define APCIE_PHY_CTRL_RESET_T8103 BIT(7)
#define APCIE_PHY_CTRL_RESET_T8132 BIT(4)

#define APCIE_PHYIF_CTRL     0x024
#define APCIE_PHYIF_CTRL_RUN BIT(0)

/* PHY common registers */
#define APCIE_PHYCMN_CLK         0x000
#define APCIE_PHYCMN_CLK_MODE    GENMASK(1, 0) /* Guesswork */
#define APCIE_PHYCMN_CLK_MODE_ON 1
#define APCIE_PHYCMN_CLK_100MHZ  BIT(31)

/* Port registers */

#define APCIE_PORT_LINKSTS      0x208
#define APCIE_PORT_LINKSTS_UP   BIT(0)
#define APCIE_PORT_LINKSTS_BUSY BIT(2)
#define APCIE_PORT_LINKSTS_L2   BIT(6)

#define APCIE_PORT_APPCLK    0x800
#define APCIE_PORT_APPCLK_EN BIT(0)

#define APCIE_PORT_STATUS     0x804
#define APCIE_PORT_STATUS_RUN BIT(0)

#define APCIE_PORT_RESET     0x814
#define APCIE_PORT_RESET_DIS BIT(0)

#define APCIE_T602X_PORT_RESET  0x82c
#define APCIE_T602X_PORT_MSIMAP 0x3800

/* PCIe capability registers */
#define PCIE_CAP_BASE    0x70
#define PCIE_LNKCAP      0x0c
#define PCIE_LNKCAP_SLS  GENMASK(3, 0)
#define PCIE_LNKCAP_MLW  GENMASK(9, 4)
#define PCIE_LNKCAP2     0x2c
#define PCIE_LNKCAP2_SLS GENMASK(6, 1)
#define PCIE_LNKCTL2     0x30
#define PCIE_LNKCTL2_TLS GENMASK(3, 0)

/* DesignWare PCIe Core registers */

#define DWC_DBI_RO_WR    0x8bc
#define DWC_DBI_RO_WR_EN BIT(0)

#define DWC_DBI_PORT_LINK_CONTROL        0x710
#define DWC_DBI_PORT_LINK_DLL_LINK_EN    BIT(5)
#define DWC_DBI_PORT_LINK_FAST_LINK_MODE BIT(7)
#define DWC_DBI_PORT_LINK_MODE           GENMASK(21, 16)
#define DWC_DBI_PORT_LINK_MODE_1_LANE    0x1
#define DWC_DBI_PORT_LINK_MODE_2_LANES   0x3
#define DWC_DBI_PORT_LINK_MODE_4_LANES   0x7
#define DWC_DBI_PORT_LINK_MODE_8_LANES   0xf
#define DWC_DBI_PORT_LINK_MODE_16_LANES  0x1f

#define DWC_DBI_LINK_WIDTH_SPEED_CONTROL 0x80c
#define DWC_DBI_LINK_WIDTH               GENMASK(12, 8)
#define DWC_DBI_SPEED_CHANGE             BIT(17)

#define PHY_STRIDE   0x4000
#define PHYIP_STRIDE 0x40000

struct fuse_bits {
    u16 src_reg;
    u16 tgt_reg;
    u8 src_bit;
    u8 tgt_bit;
    u8 width;
};

const struct fuse_bits pcie_fuse_bits_t8103[] = {
    {0x0084, 0x6238, 4, 0, 6},   {0x0084, 0x6220, 10, 14, 3}, {0x0084, 0x62a4, 13, 17, 2},
    {0x0418, 0x522c, 27, 9, 2},  {0x0418, 0x522c, 13, 12, 3}, {0x0418, 0x5220, 18, 14, 3},
    {0x0418, 0x52a4, 21, 17, 2}, {0x0418, 0x522c, 23, 16, 5}, {0x0418, 0x5278, 23, 20, 3},
    {0x0418, 0x5018, 31, 2, 1},  {0x041c, 0x1204, 0, 2, 5},   {},
};

const struct fuse_bits pcie_fuse_bits_t6000[] = {
    {0x004c, 0x1004, 3, 2, 5},   {0x0048, 0x522c, 26, 16, 5}, {0x0048, 0x522c, 29, 9, 2},
    {0x0048, 0x522c, 26, 12, 3}, {0x0048, 0x522c, 26, 16, 5}, {0x0048, 0x52a4, 24, 17, 2},
    {0x004c, 0x5018, 2, 3, 1},   {0x0048, 0x50a4, 14, 17, 2}, {0x0048, 0x62a4, 14, 17, 2},
    {0x0048, 0x6220, 8, 14, 3},  {0x0048, 0x6238, 2, 0, 6},   {},
};

/* clang-format off */
const struct fuse_bits pcie_fuse_bits_t8112[] = {
    {0x0490, 0x6238, 0, 0, 6},   {0x0490, 0x6220, 6, 14, 3},  {0x0490, 0x62a4, 12, 17, 2},
    {0x0490, 0x5018, 14, 2, 1},  {0x0490, 0x5220, 15, 14, 3}, {0x0490, 0x52a4, 18, 17, 2},
    {0x0490, 0x5278, 20, 20, 3}, {0x0490, 0x522c, 23, 12, 3}, {0x0490, 0x522c, 26, 9, 2},
    {0x0490, 0x522c, 28, 16, 4}, {0x0494, 0x522c, 0, 20, 1},  {0x0494, 0x1204, 5, 2, 5},
    {},
};
/* clang-format on */

enum apcie_type {
    APCIE_T81XX = 0,
    APCIE_T602X = 1,
    APCIE_T8122 = 2,
    APCIE_T6031 = 3,
    APCIE_T8132 = 4,
    APCIE_T8140 = 5,
};

struct reg_info {
    enum apcie_type type;
    enum apcie_type compat;
    int shared_reg_count;
    int config_idx;
    int rc_idx;
    int phy_common_idx;
    int phy_idx;
    u32 phy_ctrl_reset;
    int phy_ip_idx;
    int axi_idx;
    int fuse_idx;
    bool alt_phy_start;
};

static const struct reg_info regs_t8xxx_t600x = {
    .type = APCIE_T81XX,
    .compat = APCIE_T81XX,
    .shared_reg_count = 6,
    .config_idx = 0,
    .rc_idx = 1,
    .phy_common_idx = -1,
    .phy_idx = 2,
    .phy_ctrl_reset = APCIE_PHY_CTRL_RESET_T8103,
    .phy_ip_idx = 3,
    .axi_idx = 4,
    .fuse_idx = 5,
};

static const struct reg_info regs_t602x = {
    .type = APCIE_T602X,
    .compat = APCIE_T602X,
    .shared_reg_count = 8,
    .config_idx = 0,
    .rc_idx = 1,
    // 2 = phy unknown?
    .phy_common_idx = 3,
    .phy_idx = 4,
    .phy_ctrl_reset = APCIE_PHY_CTRL_RESET_T8103,
    .phy_ip_idx = 5,
    .axi_idx = 6,
    .fuse_idx = 7,
};

static const struct reg_info regs_t8122 = {
    .type = APCIE_T8122,
    .compat = APCIE_T8122,
    .shared_reg_count = 7,
    .config_idx = 0,
    .rc_idx = 1,
    .phy_common_idx = 2,
    .phy_idx = 2,
    .phy_ctrl_reset = APCIE_PHY_CTRL_RESET_T8103,
    .phy_ip_idx = 3,
    .axi_idx = 4,
    .fuse_idx = 5,
};

static const struct reg_info regs_t8132 = {
    .type = APCIE_T8132,
    .compat = APCIE_T8122,
    .shared_reg_count = 7,
    .config_idx = 0,
    .rc_idx = 1,
    .phy_common_idx = 2,
    .phy_idx = 2,
    .phy_ctrl_reset = APCIE_PHY_CTRL_RESET_T8132,
    .phy_ip_idx = 3,
    .axi_idx = 4,
    .fuse_idx = 5,
};

static const struct reg_info regs_t6031 = {
    .type = APCIE_T6031,
    .compat = APCIE_T8122,
    .shared_reg_count = 8,
    .config_idx = 0,
    .rc_idx = 1,
    .phy_common_idx = 2,
    .phy_idx = 2,
    .phy_ctrl_reset = APCIE_PHY_CTRL_RESET_T8103,
    .phy_ip_idx = 3,
    .axi_idx = 4,
};

static const struct reg_info regs_t8140 = {
    .type = APCIE_T8140,
    .compat = APCIE_T8140,
    .shared_reg_count = 7,
    .config_idx = 0,
    .rc_idx = 1,
    .phy_common_idx = 2,
    .phy_idx = 2,
    .phy_ctrl_reset = APCIE_PHY_CTRL_RESET_T8132,
    .phy_ip_idx = 3,
    .axi_idx = 4,
};

static bool pcie_initialized = false;

enum PCIE_CONTROLLERS {
    APCIE,
    APCIE_GE0,
    APCIE_GE1,
    NUM_CONTROLLERS,
};

#define MAX_PHYS 4

struct state {
    int num_phys;
    u64 rc_base;
    u64 axi_base;
    u64 phy_common_base;
    u64 phy_base[MAX_PHYS];
    u64 phy_ip_base[MAX_PHYS];
    u64 fuse_base;
    u32 port_count;
    u64 port_base[8];
    u64 port_ltssm_base[8];
    u64 port_phy_base[8];
    u64 port_intr2axi_base[8];
    const struct reg_info *pcie_regs;
    bool initialized;
    bool dirty;
    u32 active_ports;
};

static struct state controllers[NUM_CONTROLLERS];

/* Check every T8140 ADT window and tunable before PMGR or MMIO changes. */
static int pcie_t8140_preflight(const char *path, int *adt_path, u32 ports, u32 reg_len)
{
    static const char *const shared_props[] = {
        "apcie-axi2af-tunables",        "apcie-phy-tunables",         "apcie-phy-ip-pll-tunables",
        "apcie-phy-ip-auspma-tunables", "apcie-cio3pllcore-tunables", "apcie-pcieclkgen-tunables",
    };
    static const int shared_indices[] = {4, 2, 3, 3, 5, 6};
    u64 base, span;
    u32 present = 0;

    /* The observed ADT has seven shared windows followed by exactly
     * six windows for each port. Reject truncated or extended descriptions. */
    if (!ports || ports > 8 || reg_len != 16 * (7 + 6 * ports))
        return -1;

    for (int i = 0; i < 7; i++) {
        if (adt_get_reg(adt, adt_path, "reg", i, &base, &span) || !base || !span ||
            base > UINT64_MAX - span) {
            printf("pcie: invalid T8140 shared reg[%d]\n", i);
            return -1;
        }
        if (i == 0) {
            if (span < (u64)ports * (1 << 15))
                return -1;
        }
        if (i == 1 && span < 0x5c)
            return -1;
        if (i == 2 && span < 0xc000)
            return -1;
    }

    for (int i = 0; i < (int)ARRAY_SIZE(shared_props); i++) {
        if (adt_get_reg(adt, adt_path, "reg", shared_indices[i], &base, &span))
            return -1;
        int ret = tunables_validate_local(path, shared_props[i], span);
        if (ret) {
            printf("pcie: missing or invalid T8140 %s\n", shared_props[i]);
            return -1;
        }
    }

    int node = adt_path_offset(adt, path);
    if (adt_get_reg(adt, adt_path, "reg", 1, &base, &span))
        return -1;
    if (adt_getprop(adt, node, "apcie-common-tunables", NULL) &&
        tunables_validate_local(path, "apcie-common-tunables", span))
        return -1;

    for (u32 port = 0; port < ports; port++) {
        char bridge[64];
        snprintf(bridge, sizeof(bridge), "%s/pci-bridge%d", path, port);
        int bridge_node = adt_path_offset(adt, bridge);
        u64 port_config_span = 0;
        for (int reg = 0; reg < 6; reg++) {
            int idx = 7 + port * 6 + reg;
            if (adt_get_reg(adt, adt_path, "reg", idx, &base, &span) || !base || !span ||
                base > UINT64_MAX - span || (reg == 0 && span < 0x4000) ||
                (reg == 2 && span < 0x400)) {
                printf("pcie: invalid T8140 port %d reg[%d]\n", port, idx);
                return -1;
            }
            if (reg == 0)
                port_config_span = span;
        }
        if (bridge_node < 0)
            continue;
        if (!adt_is_compatible(adt, bridge_node, "apcie-bridge"))
            return -1;
        u32 max_speed;
        if (ADT_GETPROP(adt, bridge_node, "maximum-link-speed", &max_speed) < 0 || !max_speed ||
            max_speed > 4)
            return -1;
        if (tunables_validate_local(bridge, "apcie-config-tunables", port_config_span) ||
            tunables_validate_local(bridge, "pcie-rc-tunables", 1 << 15) ||
            tunables_validate_local(bridge, "pcie-rc-gen3-shadow-tunables", 1 << 15) ||
            tunables_validate_local(bridge, "pcie-rc-gen4-shadow-tunables", 1 << 15))
            return -1;
        present++;
        printf("pcie: T8140 ADT bridge %d validated\n", port);
    }
    if (!present)
        return -1;
    printf("pcie: T8140 ADT preflight passed (%d ports, %d bridges)\n", ports, present);
    return 0;
}

/*
 * T8152 (Apple M6) APCIe: PHY half, root complex enable and per-port bring-up.
 *
 * The register sequences come from AppleT8152PCIe and its Gen5 port class (macOS 27.0.1, kernel
 * 26A425), decoded offline: _apcieCommonInit, _apcieGPInit, _enableRootComplex, then per port
 * initializeRefclkBuffer, _resetPortHardware, enablePortHardware and setLtssmEnable. Nothing here
 * has run on hardware. Per port it also drives the endpoint PERST# GPIO and, where the ADT names
 * one, the SMC radio power key, in the order the base kext uses. Not implemented: whatever sets
 * the PHY/PLL lane rate (no decoded function writes one).
 *
 * Stages, each gated by the one before:
 *   1. ADT preflight: no MMIO. Compatible, board, port count, reg geometry, window relations,
 *      lane-cfg, bridge nodes, tunable records.
 *   2. Power the controller, then read every register the sequences touch under an exception
 *      guard. A fault, a PHY that is not idle, or a port whose link is already up stops there.
 *   3. Writes, only when built with PCIE_T8152_WRITES=1. Every poll has a timeout.
 *
 * kboot_boot() calls pcie_init() on every boot, so none of this runs unless the image is built
 * with PCIE_T8152_RECON=1 (read-only stages 1 and 2) or PCIE_T8152_WRITES=1. A default build
 * keeps the old behaviour: the generic path reports an unsupported compatible and touches nothing.
 */
#ifndef PCIE_T8152_WRITES
#define PCIE_T8152_WRITES 0
#endif
#ifndef PCIE_T8152_RECON
#define PCIE_T8152_RECON 0
#endif
/*
 * PCIE_T8152_HANDOFF=1 (with PCIE_T8152_WRITES=1): do the controller and port initialisation
 * but leave the radio power, the endpoint PERST# GPIO and the LTSSM start to the Linux driver
 * (pcie-apple.c, sunrise power sequence), which performs them itself. The SMC is not opened.
 */
#ifndef PCIE_T8152_HANDOFF
#define PCIE_T8152_HANDOFF 0
#endif
/*
 * Isolation flags for hardware bring-up (all default off):
 *   PCIE_T8152_PORT_MASK=N      bring up only the ports in bitmask N (default 0x5 = ports 0 and 2)
 *   PCIE_T8152_NO_RADIO=1       never open the SMC: skip the radio power step
 *   PCIE_T8152_STOP_AFTER_RADIO=1  return from a port right after its radio power step (no PERST#, no LTSSM)
 */
#ifndef PCIE_T8152_PORT_MASK
#define PCIE_T8152_PORT_MASK 0x5
#endif
#ifndef PCIE_T8152_NO_RADIO
#define PCIE_T8152_NO_RADIO 0
#endif
#ifndef PCIE_T8152_STOP_AFTER_RADIO
#define PCIE_T8152_STOP_AFTER_RADIO 0
#endif

#define T8152_PORTS       3
#define T8152_REG_ENTRIES 28 /* 10 shared windows + 6 per port */
#define T8152_POLL_US     250000

#define T8152_REG_COMMON   1
#define T8152_REG_PHYBASE  2
#define T8152_REG_PHY      3
#define T8152_REG_PHYIP    4
#define T8152_REG_AXI2AF   5
#define T8152_REG_CLKGEN   6
#define T8152_REG_AUS5AON  7
#define T8152_REG_AUS5TOP  8
#define T8152_REG_GAPFOCLA 9
#define T8152_REG_COUNT    10

#define T8152_PHY_CTRL0       0x000
#define T8152_PHY_CTRL0_RUN   BIT(21)
#define T8152_PHY_CTRL4       0x004
#define T8152_PHY_CTRL4_REQ   BIT(4)
#define T8152_PHY_CTRL4_HOLD  BIT(5)
#define T8152_PHY_STATUS      0x008
#define T8152_PHY_STATUS_RST  BIT(4)
#define T8152_AON_PG_STATUS   0x00c
#define T8152_AON_PG_CTRL     0x010
#define T8152_AON_PG_CTRL_ON  BIT(0)
#define T8152_CLKGEN_CTRL     0x000
#define T8152_CLKGEN_AUX      0x800
#define T8152_PHYCMN_OFFSET   0x4000 /* from the PHY base window; inferred */
#define T8152_PHYCMN_ENABLE   BIT(0)

/* Port windows: ADT reg index 10 + 6 * port + k. */
#define T8152_PORT_REG0    10
#define T8152_PORT_REGS    6
#define T8152_PORT_CFG     0 /* config window: readConfigReg/writeConfigReg */
#define T8152_PORT_LANE    2 /* PHY lane window, inside PHYBASE */
#define T8152_PORT_INTR    3 /* Intr2AXI */
#define T8152_LANE_STRIDE  0x4000
#define T8152_LANE_BASEOFF 0x10000

#define T8152_CFG_LTSSM_EN  0x080
#define T8152_CFG_PIPE_STS  0x0a8
#define T8152_CFG_LINKSTS   0x208
#define T8152_CFG_APPCLK    0x800
#define T8152_CFG_STATUS    0x804
#define T8152_CFG_PERST     0x82c
#define T8152_CFG_PERST_REL BIT(0)
#define T8152_CFG_PERST_RET BIT(16)
#define T8152_LANE_CTRL     0x000
#define T8152_COMMON_LANECFG 0x004
#define T8152_COMMON_GTB     0x048
#define T8152_COMMON_RC      0x04c

struct t8152_ports {
    u32 present;
    u32 lane_cfg;
    u64 ecam;
    u64 cfg[T8152_PORTS];
    u64 lane[T8152_PORTS];
    u64 intr[T8152_PORTS];
    char bridge[T8152_PORTS][64];
    u64 gpio_base;
    u32 perst; /* ports with a function-perst GPIO */
    u32 radio; /* ports with an SMC function-reset (radio power) */
    u32 perst_pin[T8152_PORTS];
    u32 t_refclk_to_perst[T8152_PORTS]; /* us */
    u32 perst_to_config[T8152_PORTS];   /* ms */
    u32 radio_key[T8152_PORTS];
    u32 radio_w0[T8152_PORTS];
    u32 radio_w1[T8152_PORTS];
};

static const struct {
    const char *name;
    u32 idx;
    u64 min_size;
} t8152_wins[] = {
    {"common", T8152_REG_COMMON, 0x4000},     {"phybase", T8152_REG_PHYBASE, 0x800000},
    {"phy", T8152_REG_PHY, 0x4000},           {"phyip", T8152_REG_PHYIP, 0x1c4000},
    {"axi2af", T8152_REG_AXI2AF, 0x1000000},  {"clkgen", T8152_REG_CLKGEN, 0x4000},
    {"aus5aon", T8152_REG_AUS5AON, 0x4000},   {"aus5top", T8152_REG_AUS5TOP, 0x4000},
    {"gapfocla", T8152_REG_GAPFOCLA, 0x4000},
};

struct t8152_regs {
    u64 base[T8152_REG_COUNT];
    u64 size[T8152_REG_COUNT];
};

static bool t8152_dirty;

static int t8152_preflight(const char *path, int *adt_path, int adt_offset, struct t8152_regs *r)
{
    u32 ports, reg_len;

    if (chip_id != T8152 || board_id != 0x24 || !adt_is_compatible(adt, 0, "J873gAP") ||
        !adt_is_compatible(adt, adt_offset, "apcie,t8152")) {
        printf("pcie: T8152 preflight: not a J873g apcie,t8152 node\n");
        return -1;
    }
    if (ADT_GETPROP(adt, adt_offset, "#ports", &ports) < 0 || ports != T8152_PORTS) {
        printf("pcie: T8152 preflight: expected %d ports\n", T8152_PORTS);
        return -1;
    }
    if (!adt_getprop(adt, adt_offset, "reg", &reg_len) || reg_len != 16 * T8152_REG_ENTRIES) {
        printf("pcie: T8152 preflight: unexpected reg length\n");
        return -1;
    }

    for (size_t i = 0; i < ARRAY_SIZE(t8152_wins); i++) {
        u32 idx = t8152_wins[i].idx;
        if (adt_get_reg(adt, adt_path, "reg", idx, &r->base[idx], &r->size[idx]) ||
            r->size[idx] < t8152_wins[i].min_size || (r->base[idx] & 3) ||
            r->base[idx] + r->size[idx] > 0x400000000ULL) {
            printf("pcie: T8152 preflight: bad reg[%d] (%s)\n", idx, t8152_wins[i].name);
            return -1;
        }
    }
    /* reg[3] is a view into reg[2]; reg[7] sits 0x400 above reg[8]. */
    if (r->base[T8152_REG_PHY] != r->base[T8152_REG_PHYBASE] + 0x20000 ||
        r->base[T8152_REG_AUS5AON] != r->base[T8152_REG_AUS5TOP] + 0x400) {
        printf("pcie: T8152 preflight: window layout differs from the decoded kext\n");
        return -1;
    }
    /* The PHY tunable list is required; AXI2AF tunables are optional. */
    if (tunables_validate_local(path, "apcie-phy-tunables", r->size[T8152_REG_PHYBASE]) != 0) {
        printf("pcie: T8152 preflight: apcie-phy-tunables missing or malformed\n");
        return -1;
    }
    if (tunables_validate_local(path, "apcie-axi2af-tunables", r->size[T8152_REG_AXI2AF]) < 0) {
        printf("pcie: T8152 preflight: apcie-axi2af-tunables malformed\n");
        return -1;
    }
    return 0;
}

static bool t8152_probe_read(u64 addr, u32 *value)
{
    int before = exc_count;

    exc_guard = GUARD_MARK | GUARD_SILENT;
    u32 v = read32(addr);
    sysop("dsb sy");
    sysop("isb");
    exc_guard = GUARD_OFF;
    if (exc_count != before)
        return false;
    *value = v;
    return true;
}

/* function-perst {gpio phandle, 'GPIO', pin, flags}; function-reset {smc phandle, 'pKW8', key, w0, w1}. */
static int t8152_port_functions(struct t8152_ports *p, u32 port, int bridge_offset)
{
    u32 len = 0, v;
    const u32 *f = adt_getprop(adt, bridge_offset, "function-perst", &len);

    p->t_refclk_to_perst[port] = 100;
    p->perst_to_config[port] = 100;
    if (ADT_GETPROP(adt, bridge_offset, "t-refclk-to-perst", &v) >= 0)
        p->t_refclk_to_perst[port] = v;
    if (ADT_GETPROP(adt, bridge_offset, "perst-to-config", &v) >= 0)
        p->perst_to_config[port] = v;

    if (f && len >= 16) {
        int gpio = adt_path_offset(adt, "/arm-io/gpio0");
        int gpio_path[8];
        u32 phandle, pins;

        if (gpio < 0 || ADT_GETPROP(adt, gpio, "AAPL,phandle", &phandle) < 0 || phandle != f[0] ||
            ADT_GETPROP(adt, gpio, "#gpio-pins", &pins) < 0 || f[2] >= pins) {
            printf("pcie: T8152 preflight: port %d function-perst does not name /arm-io/gpio0\n", port);
            return -1;
        }
        if (!p->gpio_base && (adt_path_offset_trace(adt, "/arm-io/gpio0", gpio_path) < 0 ||
                              adt_get_reg(adt, gpio_path, "reg", 0, &p->gpio_base, NULL))) {
            printf("pcie: T8152 preflight: no gpio0 register window\n");
            return -1;
        }
        p->perst_pin[port] = f[2];
        p->perst |= BIT(port);
    }

    f = adt_getprop(adt, bridge_offset, "function-reset", &len);
    if (f && len >= 20) {
        if (memcmp(&f[1], "pKW8", 4) && memcmp(&f[1], "8WKp", 4)) {
            printf("pcie: T8152 preflight: port %d function-reset is not an SMC pKW8 function\n", port);
            return -1;
        }
        p->radio_key[port] = f[2];
        p->radio_w0[port] = f[3];
        p->radio_w1[port] = f[4];
        p->radio |= BIT(port);
    }
    printf("pcie: T8152 preflight: port %d perst %s (pin %d, refclk-to-perst %d us, perst-to-config %d ms), radio power %s\n",
           port, (p->perst & BIT(port)) ? "gpio" : "none", p->perst_pin[port], p->t_refclk_to_perst[port],
           p->perst_to_config[port], (p->radio & BIT(port)) ? "smc key" : "none");
    return 0;
}

static int t8152_ports_preflight(const char *path, int *adt_path, const struct t8152_regs *r,
                                 struct t8152_ports *p)
{
    int node = adt_path_offset(adt, path);
    u64 ecam_size;

    memset(p, 0, sizeof(*p));
    if (adt_get_reg(adt, adt_path, "reg", 0, &p->ecam, &ecam_size) ||
        ecam_size < ((u64)T8152_PORTS << 15)) {
        printf("pcie: T8152 preflight: bad ECAM window\n");
        return -1;
    }
    /* The lane pipe-reset mask below was derived for lane-cfg 0 only. */
    if (ADT_GETPROP(adt, node, "lane-cfg", &p->lane_cfg) < 0 || p->lane_cfg != 0) {
        printf("pcie: T8152 preflight: lane-cfg is not 0\n");
        return -1;
    }

    for (u32 port = 0; port < T8152_PORTS; port++) {
        u64 base[4], size[4];
        int bridge_offset;

        snprintf(p->bridge[port], sizeof(p->bridge[port]), "%s/pci-bridge%d", path, port);
        bridge_offset = adt_path_offset(adt, p->bridge[port]);
        if (bridge_offset < 0)
            continue;
        if (!adt_is_compatible(adt, bridge_offset, "apcie-bridge")) {
            printf("pcie: T8152 preflight: port %d bridge is not apcie-bridge\n", port);
            return -1;
        }
        for (u32 k = 0; k < 4; k++) {
            if (adt_get_reg(adt, adt_path, "reg", T8152_PORT_REG0 + T8152_PORT_REGS * port + k,
                            &base[k], &size[k]) ||
                size[k] < (k == T8152_PORT_CFG ? 0x8000 : 0x4000)) {
                printf("pcie: T8152 preflight: port %d reg window %d invalid\n", port, k);
                return -1;
            }
        }
        if (base[T8152_PORT_LANE] !=
            r->base[T8152_REG_PHYBASE] + T8152_LANE_BASEOFF + T8152_LANE_STRIDE * port) {
            printf("pcie: T8152 preflight: port %d lane window is not inside PHYBASE\n", port);
            return -1;
        }
        if (tunables_validate_local(p->bridge[port], "apcie-config-tunables", size[T8152_PORT_CFG]) < 0 ||
            tunables_validate_local(p->bridge[port], "pcie-rc-tunables", 1 << 15) < 0 ||
            tunables_validate_local(p->bridge[port], "pcie-rc-gen3-shadow-tunables", 1 << 15) < 0 ||
            tunables_validate_local(p->bridge[port], "pcie-rc-gen4-shadow-tunables", 1 << 15) < 0) {
            printf("pcie: T8152 preflight: port %d tunable records malformed\n", port);
            return -1;
        }
        p->cfg[port] = base[T8152_PORT_CFG];
        p->lane[port] = base[T8152_PORT_LANE];
        p->intr[port] = base[T8152_PORT_INTR];
        if (t8152_port_functions(p, port, bridge_offset))
            return -1;
        p->present |= BIT(port);
    }
    if (!p->present) {
        printf("pcie: T8152 preflight: no port bridge nodes\n");
        return -1;
    }
    return 0;
}

/* Read-only: every register the port sequence touches first. A port whose link is up is dropped. */
static int t8152_ports_recon(struct t8152_ports *p, const struct t8152_regs *r)
{
    u32 v;
    static const struct {
        const char *name;
        u32 off;
    } cfg_regs[] = {
        {"cfg+0x080", T8152_CFG_LTSSM_EN}, {"cfg+0x0a8", T8152_CFG_PIPE_STS},
        {"cfg+0x208", T8152_CFG_LINKSTS},  {"cfg+0x800", T8152_CFG_APPCLK},
        {"cfg+0x804", T8152_CFG_STATUS},   {"cfg+0x82c", T8152_CFG_PERST},
    };
    static const u32 common_regs[] = {T8152_COMMON_LANECFG, T8152_COMMON_GTB, T8152_COMMON_RC};

    for (size_t i = 0; i < ARRAY_SIZE(common_regs); i++) {
        if (!t8152_probe_read(r->base[T8152_REG_COMMON] + common_regs[i], &v)) {
            printf("pcie: T8152 recon: common+0x%03x faulted; stopping\n", common_regs[i]);
            return -1;
        }
        printf("pcie: T8152 recon: common+0x%03x = 0x%08x\n", common_regs[i], v);
    }
    for (u32 port = 0; port < T8152_PORTS; port++) {
        if (!(p->present & BIT(port)))
            continue;
        for (size_t i = 0; i < ARRAY_SIZE(cfg_regs); i++) {
            if (!t8152_probe_read(p->cfg[port] + cfg_regs[i].off, &v)) {
                printf("pcie: T8152 recon: port %d %s faulted; stopping\n", port, cfg_regs[i].name);
                return -1;
            }
            printf("pcie: T8152 recon: port %d %-10s = 0x%08x\n", port, cfg_regs[i].name, v);
            if (cfg_regs[i].off == T8152_CFG_LINKSTS && (v & APCIE_PORT_LINKSTS_UP)) {
                printf("pcie: T8152 recon: port %d link already up; leaving it alone\n", port);
                p->present &= ~BIT(port);
                break;
            }
        }
        if (!(p->present & BIT(port)))
            continue;
        if (!t8152_probe_read(p->lane[port] + T8152_LANE_CTRL, &v)) {
            printf("pcie: T8152 recon: port %d lane+0x000 faulted; stopping\n", port);
            return -1;
        }
        printf("pcie: T8152 recon: port %d lane+0x000  = 0x%08x\n", port, v);
        if ((p->perst & BIT(port)) && !t8152_probe_read(p->gpio_base + 4 * p->perst_pin[port], &v)) {
            printf("pcie: T8152 recon: port %d perst pin %d faulted; stopping\n", port, p->perst_pin[port]);
            return -1;
        }
        if (p->perst & BIT(port))
            printf("pcie: T8152 recon: port %d perst pin %d reg = 0x%08x\n", port, p->perst_pin[port], v);
    }
    return p->present ? 0 : -1;
}

#if PCIE_T8152_WRITES
static int t8152_phy_init(const char *path, int adt_offset, const struct t8152_regs *r)
{
    u64 phy = r->base[T8152_REG_PHY];
    u64 aon = r->base[T8152_REG_AUS5AON];

    /* _apcieCommonInit. The kext skips this when an unidentified platform stub returns 2. */
    if (!(read32(aon + T8152_AON_PG_CTRL) & T8152_AON_PG_CTRL_ON))
        set32(aon + T8152_AON_PG_CTRL, T8152_AON_PG_CTRL_ON);
    if (adt_getprop(adt, adt_offset, "apcie-axi2af-tunables", NULL) &&
        tunables_apply_local(path, "apcie-axi2af-tunables", T8152_REG_AXI2AF)) {
        printf("pcie: T8152 axi2af tunables failed\n");
        return -1;
    }

    /* _apcieGPInit */
    if (tunables_apply_local(path, "apcie-phy-tunables", T8152_REG_PHYBASE)) {
        printf("pcie: T8152 phy tunables failed\n");
        return -1;
    }

    /* _initializeAUS5Phys */
    set32(phy + T8152_PHY_CTRL4, T8152_PHY_CTRL4_REQ);
    if (poll32(phy + T8152_PHY_STATUS, T8152_PHY_STATUS_RST, T8152_PHY_STATUS_RST, T8152_POLL_US)) {
        printf("pcie: T8152 PHY reset status did not set\n");
        return -1;
    }
    clear32(phy + T8152_PHY_CTRL4, T8152_PHY_CTRL4_HOLD);
    set32(phy + T8152_PHY_CTRL0, T8152_PHY_CTRL0_RUN);

    /* PHY common enable; the block is inferred from the kext's helper (PHY base + 0x4000). */
    set32(r->base[T8152_REG_PHYBASE] + T8152_PHYCMN_OFFSET, T8152_PHYCMN_ENABLE);
    return 0;
}

/* _enableRootComplex(true). COMMON[4] is `this[0x1a4]`, which _setLaneCfg stores (the ADT lane-cfg). */
static void t8152_rc_enable(const struct t8152_regs *r, const struct t8152_ports *p)
{
    u64 common = r->base[T8152_REG_COMMON];

    write32(common + T8152_COMMON_LANECFG, p->lane_cfg);
    write32(common + T8152_COMMON_RC, 0x140);
    set32(common + T8152_COMMON_GTB, BIT(0)); /* _enableGTBToPTM(true) */
}

/* _resetPortHardware: values as written by the kext, in order. */
static const struct {
    u32 off, val;
} t8152_port_reset[] = {
    {0x088, 0x110},      {0x100, 0xffffffff}, {0x148, 0xffffffff}, {0x210, 0xffffffff},
    {0x080, 0x0},        {0x084, 0x0},        {0x104, 0xfffffff0}, {0x124, 0x100},
    {0x16c, 0x0},        {0x13c, 0x0},        {0x800, 0x100100},   {0x808, 0x1001ff},
    {0x82c, 0x10000},
};
static const struct {
    u32 off, val;
} t8152_port_reset_tail[] = {
    {0x130, 0x208}, {0x140, 0x10}, {0x144, 0x253770}, {0x21c, 0x0}, {0x834, 0x0}, {0x83c, 0x0},
};

#if !PCIE_T8152_HANDOFF
#define T8152_GPIO_DATA     BIT(0)
#define T8152_GPIO_MODE     GENMASK(3, 1)
#define T8152_GPIO_MODE_OUT 1

/* Apple GPIO: one 32-bit register per pin (DATA bit 0, MODE bits 3:1, 1 = output). */
static void t8152_gpio_drive(const struct t8152_ports *p, u32 port, int level)
{
    mask32(p->gpio_base + 4 * p->perst_pin[port], T8152_GPIO_MODE | T8152_GPIO_DATA,
           FIELD_PREP(T8152_GPIO_MODE, T8152_GPIO_MODE_OUT) | (level ? T8152_GPIO_DATA : 0));
}

#if !PCIE_T8152_NO_RADIO
/* AppleSMCPMU pKW8 on: 8 bytes {w0 with its low 16 bits set to 1, w1}, as the DCP code does for HDMI. */
static int t8152_radio_power_on(const struct t8152_ports *p, u32 port)
{
    u32 payload[2] = {(p->radio_w0[port] & ~0xffffu) | 1, p->radio_w1[port]};
    smc_dev_t *smc = smc_init();
    int ret;

    if (!smc) {
        printf("pcie: T8152 port %d: SMC init failed\n", port);
        return -1;
    }
    ret = smc_write(smc, p->radio_key[port], payload, sizeof(payload));
    smc_shutdown(smc);
    printf("pcie: T8152 port %d: radio power key %08x write {%08x, %08x} -> %d\n", port,
           p->radio_key[port], payload[0], payload[1], ret);
    return ret;
}
#endif
#endif

static int t8152_port_init(const struct t8152_ports *p, u32 port, int bridge_offset)
{
    const char *bridge = p->bridge[port];
    u64 cfg = p->cfg[port];
    u64 lane = p->lane[port];
    u64 dbi = p->ecam + ((u64)port << 15);
    u32 max_speed;

    /*
     * Power and endpoint reset, as the base kext orders them: the radio is powered, the endpoint
     * PERST# is held low, and it is released only after the refclk lane init, t-refclk-to-perst,
     * and at least 100 ms since power (PERST# release is also enforced to be 100 ms after the
     * reset timestamp in setEndpointReset).
     */
#if !PCIE_T8152_HANDOFF
#if PCIE_T8152_NO_RADIO
    if (p->radio & BIT(port))
        printf("pcie: T8152 port %d: radio power skipped (PCIE_T8152_NO_RADIO)\n", port);
#else
    if ((p->radio & BIT(port)) && t8152_radio_power_on(p, port))
        printf("pcie: T8152 port %d: radio power failed; continuing with PERST# and link training\n", port);
#endif
#if PCIE_T8152_STOP_AFTER_RADIO
    printf("pcie: T8152 port %d: stopping after the radio power step (PCIE_T8152_STOP_AFTER_RADIO)\n", port);
    return 0;
#endif
    if (p->perst & BIT(port))
        t8152_gpio_drive(p, port, 0);
#endif

    /* initializeRefclkBuffer (PHY lane window). */
    set32(lane + T8152_LANE_CTRL, BIT(0));
    if (poll32(lane + T8152_LANE_CTRL, BIT(2), BIT(2), 50000))
        goto timeout;
    set32(lane + T8152_LANE_CTRL, BIT(1));
    if (poll32(lane + T8152_LANE_CTRL, BIT(3), BIT(3), 50000))
        goto timeout;
    clear32(lane + T8152_LANE_CTRL, BIT(4));
    set32(lane + T8152_LANE_CTRL, BIT(9));
    set32(lane + T8152_LANE_CTRL, BIT(10));

    udelay(p->t_refclk_to_perst[port]);
#if !PCIE_T8152_HANDOFF
    if (p->perst & BIT(port)) {
        mdelay(100);
        t8152_gpio_drive(p, port, 1);
        printf("pcie: T8152 port %d: endpoint PERST# released (pin %d)\n", port, p->perst_pin[port]);
        mdelay(p->perst_to_config[port]);
    }
#endif

    /* _resetPortHardware */
    for (size_t i = 0; i < ARRAY_SIZE(t8152_port_reset); i++)
        write32(cfg + t8152_port_reset[i].off, t8152_port_reset[i].val);
    for (u32 i = 0; i < 16; i++)
        write32(cfg + 0x3000 + 4 * i, 0);
    for (u32 i = 0; i < 256; i++)
        write32(cfg + 0x3800 + 4 * i, 0);
    for (size_t i = 0; i < ARRAY_SIZE(t8152_port_reset_tail); i++)
        write32(cfg + t8152_port_reset_tail[i].off, t8152_port_reset_tail[i].val);

    /* The kext applies tunables here through a slot whose property it was not possible to read. */
    if (tunables_apply_local_addr(bridge, "apcie-config-tunables", cfg)) {
        printf("pcie: T8152 port %d config tunables failed\n", port);
        return -1;
    }

    /* enablePortHardware: setPortEnable(true), PERST# release, lane pipe reset (mask 1). */
    set32(cfg + T8152_CFG_APPCLK, BIT(0));
    if (poll32(cfg + T8152_CFG_STATUS, BIT(0), BIT(0), 50000))
        goto timeout;
    set32(cfg + T8152_CFG_PERST, T8152_CFG_PERST_REL);
    if (poll32(cfg + T8152_CFG_PIPE_STS, BIT(16), 0, 20000))
        goto timeout;
    clear32(cfg + T8152_CFG_PERST, T8152_CFG_PERST_RET);
    if (poll32(cfg + T8152_CFG_STATUS, BIT(0), BIT(0), T8152_POLL_US))
        goto timeout;

    /* _initializeRootComplex: DBI registers through the ECAM window, as for the other Gen5 parts. */
    set32(dbi + DWC_DBI_RO_WR, DWC_DBI_RO_WR_EN);
    if (tunables_apply_local_addr(bridge, "pcie-rc-tunables", dbi) ||
        tunables_apply_local_addr(bridge, "pcie-rc-gen3-shadow-tunables", dbi) ||
        tunables_apply_local_addr(bridge, "pcie-rc-gen4-shadow-tunables", dbi)) {
        printf("pcie: T8152 port %d rc tunables failed\n", port);
        return -1;
    }
    if (ADT_GETPROP(adt, bridge_offset, "maximum-link-speed", &max_speed) >= 0 && max_speed >= 1 &&
        max_speed <= 4) {
        mask32(dbi + PCIE_CAP_BASE + PCIE_LNKCAP, PCIE_LNKCAP_SLS,
               FIELD_PREP(PCIE_LNKCAP_SLS, max_speed));
        mask32(dbi + PCIE_CAP_BASE + PCIE_LNKCAP2, PCIE_LNKCAP2_SLS,
               FIELD_PREP(PCIE_LNKCAP2_SLS, (1 << max_speed) - 1));
        mask16(dbi + PCIE_CAP_BASE + PCIE_LNKCTL2, PCIE_LNKCTL2_TLS,
               FIELD_PREP(PCIE_LNKCTL2_TLS, max_speed));
        set32(dbi + DWC_DBI_LINK_WIDTH_SPEED_CONTROL, DWC_DBI_SPEED_CHANGE);
    }
    /* One lane: the kext derives the width from the lane config; x1 is assumed here. */
    mask32(dbi + DWC_DBI_PORT_LINK_CONTROL, DWC_DBI_PORT_LINK_MODE,
           FIELD_PREP(DWC_DBI_PORT_LINK_MODE, DWC_DBI_PORT_LINK_MODE_1_LANE));
    mask32(dbi + DWC_DBI_LINK_WIDTH_SPEED_CONTROL, DWC_DBI_LINK_WIDTH,
           FIELD_PREP(DWC_DBI_LINK_WIDTH, 1));
    mask32(dbi + PCIE_CAP_BASE + PCIE_LNKCAP, PCIE_LNKCAP_MLW, FIELD_PREP(PCIE_LNKCAP_MLW, 1));
    clear32(dbi + DWC_DBI_RO_WR, DWC_DBI_RO_WR_EN);

    /* _writeIntr2AxiReg(0x80, 1), then enableLinkTraining -> setLtssmEnable(true). */
    write32(p->intr[port] + 0x80, 1);
#if PCIE_T8152_HANDOFF
    printf("pcie: T8152 port %d: initialised; radio power, endpoint PERST# and LTSSM left to Linux\n", port);
    return 0;
#else
    set32(cfg + T8152_CFG_LTSSM_EN, BIT(0));
    if (poll32(cfg + T8152_CFG_LINKSTS, APCIE_PORT_LINKSTS_UP, APCIE_PORT_LINKSTS_UP, T8152_POLL_US))
        printf("pcie: T8152 port %d: link not up after %d ms\n", port, T8152_POLL_US / 1000);
    else
        printf("pcie: T8152 port %d: link up, status 0x%x\n", port, read32(cfg + T8152_CFG_LINKSTS));
    return 0;
#endif

timeout:
    printf("pcie: T8152 port %d: poll timed out (cfg+0x800 0x%x +0x804 0x%x +0x82c 0x%x +0x0a8 0x%x)\n",
           port, read32(cfg + T8152_CFG_APPCLK), read32(cfg + T8152_CFG_STATUS),
           read32(cfg + T8152_CFG_PERST), read32(cfg + T8152_CFG_PIPE_STS));
    return -1;
}
#endif

/*
 * The M6 apcie ADT node has no clock-gates, so pmgr_adt_power_enable() cannot find any device. The APCIe PMGR domains
 * (pmgr0 offsets, from the r4 DTS t8152-j873-pmgr.dtsi) are normally on at boot; Linux reports only the two OCLA
 * (on-chip logic analyser) domains as off. Report every domain; with PCIE_T8152_WRITES request ACTIVE for any that is
 * not, parents first. Nothing here is fatal: the register probe that follows decides.
 */
#define T8152_PS_ACTUAL GENMASK(7, 4)
#define T8152_PS_TARGET GENMASK(3, 0)
#define T8152_PS_ACTIVE 0xf

static const struct {
    const char *name;
    u32 off;
} t8152_apcie_ps[] = {
    {"apcie_gp", 0x8c0},         {"apcie_st0", 0xea0},      {"apcie_sys_gp", 0xec0},
    {"apcie_sys_st0", 0x1280},   {"apcie_sysocla", 0x12a0}, {"apcie_phy_sw", 0x15a0},
    {"apcie_st0_ocla", 0x15c0},  {"apcie_aon_aus5", 0x1620}, {"apcie_aon_gp", 0x1640},
    {"apcie_st0_aus5", 0x1660},  {"apcie_gp_aus5", 0x1680},
};

static void t8152_apcie_pmgr(void)
{
    int path[8];
    u64 base, size;

    if (adt_path_offset_trace(adt, "/arm-io/pmgr", path) < 0 || adt_get_reg(adt, path, "reg", 0, &base, &size) ||
        !base) {
        printf("pcie: T8152 pmgr: /arm-io/pmgr reg[0] not found\n");
        return;
    }
    for (size_t i = 0; i < ARRAY_SIZE(t8152_apcie_ps); i++) {
        u64 addr = base + t8152_apcie_ps[i].off;
        u32 v = read32(addr);

        printf("pcie: T8152 pmgr %-15s = 0x%08x actual %u target %u\n", t8152_apcie_ps[i].name, v,
               (u32)FIELD_GET(T8152_PS_ACTUAL, v), (u32)FIELD_GET(T8152_PS_TARGET, v));
#if PCIE_T8152_WRITES
        if (FIELD_GET(T8152_PS_ACTUAL, v) != T8152_PS_ACTIVE) {
            mask32(addr, T8152_PS_TARGET, FIELD_PREP(T8152_PS_TARGET, T8152_PS_ACTIVE));
            int ret = poll32(addr, T8152_PS_ACTUAL, FIELD_PREP(T8152_PS_ACTUAL, T8152_PS_ACTIVE), 10000);
            printf("pcie: T8152 pmgr %-15s -> %s (0x%08x)\n", t8152_apcie_ps[i].name,
                   ret < 0 ? "not active" : "active", read32(addr));
        }
#endif
    }
}

/* Returns 1 when skipped or read-only, -1 when the controller was touched and no link exists. */
static int pcie_t8152_init(const char *path)
{
    struct t8152_regs r = {0};
    int adt_path[8];
    int adt_offset;

    if (t8152_dirty) {
        printf("pcie: T8152 controller already touched; refusing retry\n");
        return -1;
    }
    adt_offset = adt_path_offset_trace(adt, path, adt_path);
    if (adt_offset < 0) {
        printf("pcie: T8152 controller %s absent\n", path);
        return 1;
    }
    if (t8152_preflight(path, adt_path, adt_offset, &r))
        return 1;
    printf("pcie: T8152 ADT preflight passed\n");

    struct t8152_ports ports;
    if (t8152_ports_preflight(path, adt_path, &r, &ports))
        return 1;
    printf("pcie: T8152 port preflight passed (ports 0x%x, lane-cfg %d)\n", ports.present,
           ports.lane_cfg);

    t8152_apcie_pmgr();

    struct {
        const char *name;
        u64 addr;
        u32 value;
    } probe[] = {
        {"phy+0x000", r.base[T8152_REG_PHY] + T8152_PHY_CTRL0, 0},
        {"phy+0x004", r.base[T8152_REG_PHY] + T8152_PHY_CTRL4, 0},
        {"phy+0x008", r.base[T8152_REG_PHY] + T8152_PHY_STATUS, 0},
        {"aus5aon+0x00c", r.base[T8152_REG_AUS5AON] + T8152_AON_PG_STATUS, 0},
        {"aus5aon+0x010", r.base[T8152_REG_AUS5AON] + T8152_AON_PG_CTRL, 0},
        {"aus5top+0x00c", r.base[T8152_REG_AUS5TOP] + T8152_AON_PG_STATUS, 0},
        {"clkgen+0x000", r.base[T8152_REG_CLKGEN] + T8152_CLKGEN_CTRL, 0},
        {"clkgen+0x800", r.base[T8152_REG_CLKGEN] + T8152_CLKGEN_AUX, 0},
        {"phycommon+0x000", r.base[T8152_REG_PHYBASE] + T8152_PHYCMN_OFFSET, 0},
    };
    for (size_t i = 0; i < ARRAY_SIZE(probe); i++) {
        if (!t8152_probe_read(probe[i].addr, &probe[i].value)) {
            printf("pcie: T8152 recon: read of %s (0x%lx) faulted; stopping\n", probe[i].name,
                   probe[i].addr);
            return -1;
        }
        printf("pcie: T8152 recon: %-16s = 0x%08x\n", probe[i].name, probe[i].value);
    }

    bool run = probe[0].value & T8152_PHY_CTRL0_RUN;
    bool rst = probe[2].value & T8152_PHY_STATUS_RST;
    if (run || rst) {
        printf("pcie: T8152 recon: PHY not idle (run=%d reset-status=%d); no writes\n", run, rst);
        return -1;
    }

    if (t8152_ports_recon(&ports, &r)) {
        printf("pcie: T8152 port recon: nothing to bring up; no writes\n");
        return -1;
    }

#if PCIE_T8152_WRITES
    printf("pcie: T8152 recon passed; running PHY, root complex and port sequences\n");
    t8152_dirty = true;
    if (t8152_phy_init(path, adt_offset, &r))
        return -1;
    printf("pcie: T8152 PHY sequence done: phy+0x000 0x%08x phy+0x004 0x%08x phy+0x008 0x%08x\n",
           read32(r.base[T8152_REG_PHY] + T8152_PHY_CTRL0),
           read32(r.base[T8152_REG_PHY] + T8152_PHY_CTRL4),
           read32(r.base[T8152_REG_PHY] + T8152_PHY_STATUS));
    t8152_rc_enable(&r, &ports);
    for (u32 port = 0; port < T8152_PORTS; port++) {
        if (!(ports.present & BIT(port) & PCIE_T8152_PORT_MASK))
            continue;
        printf("pcie: T8152 initializing port %d\n", port);
        if (t8152_port_init(&ports, port, adt_path_offset(adt, ports.bridge[port])))
            printf("pcie: T8152 port %d: bring-up failed; continuing with the next port\n", port);
    }
    printf("pcie: T8152 sequences done; the controller is left as is and not reported as initialized\n");
    return -1;
#else
    printf("pcie: T8152 recon passed; writes disabled (build with PCIE_T8152_WRITES=1)\n");
    return 1;
#endif
}

static int pcie_init_controller(int controller, const char *path)
{
    struct state *state = &controllers[controller];
    int adt_path[8];
    int adt_offset;
    u32 lane_mode = DWC_DBI_PORT_LINK_MODE_1_LANE;
    u32 link_width = 1;
    const struct fuse_bits *fuse_bits;

    if (state->dirty) {
        printf("pcie: refusing retry of partially initialized controller %d\n", controller);
        return -1;
    }
    state->initialized = false;
    state->active_ports = 0;
    state->num_phys = 1;

    adt_offset = adt_path_offset_trace(adt, path, adt_path);
    if (adt_offset < 0) {
        printf("pcie: Error getting node %s\n", path);
        return -1;
    }

    if (adt_is_compatible(adt, adt_offset, "apcie,t8103")) {
        fuse_bits = pcie_fuse_bits_t8103;
        state->pcie_regs = &regs_t8xxx_t600x;
        printf("pcie: Initializing t8103 PCIe controller\n");
    } else if (adt_is_compatible(adt, adt_offset, "apcie,t6000")) {
        fuse_bits = pcie_fuse_bits_t6000;
        state->pcie_regs = &regs_t8xxx_t600x;
        printf("pcie: Initializing t6000 PCIe controller\n");
    } else if (adt_is_compatible(adt, adt_offset, "apcie,t8112")) {
        fuse_bits = pcie_fuse_bits_t8112;
        state->pcie_regs = &regs_t8xxx_t600x;
        printf("pcie: Initializing t8112 PCIe controller\n");
    } else if (adt_is_compatible(adt, adt_offset, "apcie,t8122")) {
        fuse_bits = NULL;
        state->pcie_regs = &regs_t8122;
        printf("pcie: Initializing t8122 PCIe controller\n");
    } else if (adt_is_compatible(adt, adt_offset, "apcie,t6030")) {
        fuse_bits = NULL;
        state->pcie_regs = &regs_t8122;
        printf("pcie: Initializing t6030 PCIe controller\n");
    } else if (adt_is_compatible(adt, adt_offset, "apcie,t6020")) {
        fuse_bits = NULL;
        state->pcie_regs = &regs_t602x;
        printf("pcie: Initializing t6020 PCIe controller\n");
    } else if (adt_is_compatible(adt, adt_offset, "apcie,t6031")) {
        fuse_bits = NULL;
        state->pcie_regs = &regs_t6031;
        printf("pcie: Initializing t6031 PCIe controller\n");
    } else if (adt_is_compatible(adt, adt_offset, "apcie,t8132")) {
        fuse_bits = NULL;
        state->pcie_regs = &regs_t8132;
        printf("pcie: Initializing t8132 PCIe controller\n");
    } else if (adt_is_compatible(adt, adt_offset, "apcie,t8140")) {
        fuse_bits = NULL;
        state->pcie_regs = &regs_t8140;
        printf("pcie: Initializing t8140 PCIe controller\n");
    } else if (adt_is_compatible(adt, adt_offset, "apcie,t6040")) {
        fuse_bits = NULL;
        state->pcie_regs = &regs_t8132;
        printf("pcie: Initializing t6040 PCIe controller\n");
    } else if (adt_is_compatible(adt, adt_offset, "apcie-ge,t6020")) {
        u32 lane_cfg;
        fuse_bits = NULL;
        state->pcie_regs = &regs_t602x;

        printf("pcie: Initializing t6020 PCIe GE controller\n");
        if (ADT_GETPROP(adt, adt_offset, "lane-cfg", &lane_cfg) < 0) {
            printf("pcie: Error getting lane_cfg for %s\n", path);
            return -1;
        }
        switch (lane_cfg) {
            case 0:
                state->num_phys = 4;
                lane_mode = DWC_DBI_PORT_LINK_MODE_16_LANES;
                link_width = 16;
                break;
            case 1:
                state->num_phys = 2;
                lane_mode = DWC_DBI_PORT_LINK_MODE_8_LANES;
                link_width = 8;
                break;
            default:
                printf("pcie: Unknown lane config %d for %s\n", lane_cfg, path);
                return -1;
        }
    } else {
        printf("pcie: Unsupported compatible\n");
        return -1;
    }

    if (ADT_GETPROP(adt, adt_offset, "#ports", &state->port_count) < 0) {
        printf("pcie: Error getting port count for %s\n", path);
        return -1;
    }

    u64 config_base;
    if (adt_get_reg(adt, adt_path, "reg", state->pcie_regs->config_idx, &config_base, NULL)) {
        printf("pcie: Error getting reg with index %d for %s\n", state->pcie_regs->config_idx,
               path);
        return -1;
    }

    if (adt_get_reg(adt, adt_path, "reg", state->pcie_regs->rc_idx, &state->rc_base, NULL)) {
        printf("pcie: Error getting reg with index %d for %s\n", state->pcie_regs->rc_idx, path);
        return -1;
    }

    if (adt_get_reg(adt, adt_path, "reg", state->pcie_regs->axi_idx, &state->axi_base, NULL)) {
        printf("pcie: Error getting reg with index %d for %s\n", state->pcie_regs->axi_idx, path);
        return -1;
    }

    if (state->pcie_regs->phy_common_idx != -1) {
        if (adt_get_reg(adt, adt_path, "reg", state->pcie_regs->phy_common_idx,
                        &state->phy_common_base, NULL)) {
            printf("pcie: Error getting reg with index %d for %s\n",
                   state->pcie_regs->phy_common_idx, path);
            return -1;
        }
    } else {
        state->phy_common_base = 0;
    }

    if (adt_get_reg(adt, adt_path, "reg", state->pcie_regs->phy_idx, &state->phy_base[0], NULL)) {
        printf("pcie: Error getting reg with index %d for %s\n", state->pcie_regs->phy_idx, path);
        return -1;
    }

    if (adt_get_reg(adt, adt_path, "reg", state->pcie_regs->phy_ip_idx, &state->phy_ip_base[0],
                    NULL)) {
        printf("pcie: Error getting reg with index %d for %s\n", state->pcie_regs->phy_ip_idx,
               path);
        return -1;
    }

    if (state->pcie_regs->compat == APCIE_T8122 || state->pcie_regs->type == APCIE_T8140) {
        // The T8122 init seems very similar to the T602X, with different offsets,
        // and with reg[2], [3] and [4] coalesced to reg[2] in the ADT.
        state->phy_base[0] = state->phy_base[0] + 0x8000;
        state->phy_common_base += 0x4000;
    }

    for (int phy = 1; phy < state->num_phys; phy++) {
        state->phy_base[phy] = state->phy_base[0] + PHY_STRIDE * phy;
        state->phy_ip_base[phy] = state->phy_ip_base[0] + PHYIP_STRIDE * phy;
    }

    if (state->pcie_regs->type != APCIE_T8140 &&
        adt_get_reg(adt, adt_path, "reg", state->pcie_regs->fuse_idx, &state->fuse_base, NULL)) {
        printf("pcie: Error getting reg with index %d for %s\n", state->pcie_regs->fuse_idx, path);
        return -1;
    }

    u32 reg_len;
    if (!adt_getprop(adt, adt_offset, "reg", &reg_len)) {
        printf("pcie: Error getting reg length for %s\n", path);
        return -1;
    }

    int port_reg_cnt = -1;
    if (state->pcie_regs->type == APCIE_T8140) {
        port_reg_cnt = 6;
    } else if (state->port_count && state->port_count <= 8 && reg_len % 16 == 0) {
        u32 entries = reg_len / 16;
        u32 shared = state->pcie_regs->shared_reg_count;
        if (entries >= shared + 3 * state->port_count &&
            (entries - shared) % state->port_count == 0)
            port_reg_cnt = (entries - shared) / state->port_count;
    }
    if (port_reg_cnt < 0) {
        printf("pcie: invalid ADT port geometry (%d bytes, %d ports)\n", reg_len,
               state->port_count);
        return -1;
    }
    printf("pcie: ADT uses %d reg entries per port\n", port_reg_cnt);

    if (state->pcie_regs->type == APCIE_T8140 &&
        pcie_t8140_preflight(path, adt_path, state->port_count, reg_len)) {
        printf("pcie: T8140 ADT preflight failed; leaving PCIe disabled\n");
        return -1;
    }

    state->dirty = true;
    if (state->pcie_regs->type == APCIE_T8140)
        printf("pcie: T8140 enabling controller power\n");
    if (pmgr_adt_power_enable(path)) {
        printf("pcie: Error enabling power for %s\n", path);
        return -1;
    }
    printf("pcie: controller %d power enabled\n", controller);

    if (state->pcie_regs->type == APCIE_T8140)
        printf("pcie: T8140 AXI reg[4] tunables\n");
    if (!adt_getprop(adt, adt_offset, "apcie-axi2af-tunables", NULL)) {
        printf("pcie: No axi2af tunables\n");
    } else if (tunables_apply_local(path, "apcie-axi2af-tunables", state->pcie_regs->axi_idx)) {
        printf("pcie: Error applying %s for %s\n", "apcie-axi2af-tunables", path);
        return -1;
    }

    if (state->pcie_regs->type == APCIE_T8140) {
        printf("pcie: T8140 CIO3 PLL core reg[5] tunables\n");
        if (tunables_apply_local(path, "apcie-cio3pllcore-tunables", 5)) {
            printf("pcie: Error applying T8140 CIO3 PLL core tunables\n");
            return -1;
        }
        printf("pcie: T8140 PCIe clock generator reg[6] tunables\n");
        if (tunables_apply_local(path, "apcie-pcieclkgen-tunables", 6)) {
            printf("pcie: Error applying T8140 PCIe clock generator tunables\n");
            return -1;
        }
        printf("pcie: T8140 clock tunables applied\n");
    }

    /* ??? */
    if (state->pcie_regs->type == APCIE_T8140)
        printf("pcie: T8140 RC reg[1] setup\n");
    if (controller == APCIE)
        write32(state->rc_base + 0x4, 0);

    if (!adt_getprop(adt, adt_offset, "apcie-common-tunables", NULL)) {
        printf("pcie: No common tunables\n");
    } else if (tunables_apply_local(path, "apcie-common-tunables", state->pcie_regs->rc_idx)) {
        printf("pcie: Error applying %s for %s\n", "apcie-common-tunables", path);
        return -1;
    }

    /*
     * Initialize PHY.
     */

    if (state->pcie_regs->type == APCIE_T8140)
        printf("pcie: T8140 PHY reg[2] tunables (including port PHYs)\n");
    if (!adt_getprop(adt, adt_offset, "apcie-phy-tunables", NULL)) {
        printf("pcie: No PHY tunables\n");
    } else if (tunables_apply_local(path, "apcie-phy-tunables", state->pcie_regs->phy_idx)) {
        printf("pcie: Error applying %s for %s\n", "apcie-phy-tunables", path);
        return -1;
    }

    if (state->pcie_regs->type == APCIE_T602X || state->pcie_regs->type == APCIE_T6031) {
        if (poll32(state->phy_common_base + APCIE_PHYCMN_CLK, APCIE_PHYCMN_CLK_100MHZ,
                   APCIE_PHYCMN_CLK_100MHZ, 250000)) {
            printf("pcie: Reference clock not available\n");
            return -1;
        }
    }

    for (int phy = 0; phy < state->num_phys; phy++) {
        if (state->pcie_regs->type == APCIE_T8140)
            printf("pcie: T8140 PHY %d requesting CLK0\n", phy);
        set32(state->phy_base[phy] + APCIE_PHY_CTRL, APCIE_PHY_CTRL_CLK0REQ);
        if (poll32(state->phy_base[phy] + APCIE_PHY_CTRL, APCIE_PHY_CTRL_CLK0ACK,
                   APCIE_PHY_CTRL_CLK0ACK, 50000)) {
            printf("pcie: Timeout enabling PHY CLK0\n");
            return -1;
        }

        if (state->pcie_regs->type == APCIE_T8140)
            printf("pcie: T8140 PHY %d CLK0 acknowledged; requesting CLK1\n", phy);
        set32(state->phy_base[phy] + APCIE_PHY_CTRL, APCIE_PHY_CTRL_CLK1REQ);
        if (poll32(state->phy_base[phy] + APCIE_PHY_CTRL, APCIE_PHY_CTRL_CLK1ACK,
                   APCIE_PHY_CTRL_CLK1ACK, 50000)) {
            printf("pcie: Timeout enabling PHY CLK1\n");
            return -1;
        }

        if (state->pcie_regs->type == APCIE_T8140)
            printf("pcie: T8140 PHY %d CLK1 acknowledged; releasing reset\n", phy);
        if (state->pcie_regs->phy_ctrl_reset)
            clear32(state->phy_base[phy] + APCIE_PHY_CTRL, state->pcie_regs->phy_ctrl_reset);
        udelay(1);

        /* Confirm reset release before continuing with PHY control. */
        if (state->pcie_regs->type == APCIE_T8140 &&
            poll32(state->phy_base[phy] + APCIE_PHY_CTRL, state->pcie_regs->phy_ctrl_reset, 0,
                   50000)) {
            printf("pcie: Timeout releasing T8140 PHY reset\n");
            return -1;
        }

        /* ??? */
        if (state->pcie_regs->type == APCIE_T81XX) {
            set32(state->rc_base + APCIE_PHYIF_CTRL, APCIE_PHYIF_CTRL_RUN);
            udelay(1);
        } else if (state->pcie_regs->type == APCIE_T602X ||
                   state->pcie_regs->compat == APCIE_T8122 ||
                   state->pcie_regs->type == APCIE_T8140) {
            set32(state->phy_base[phy] + 4, 0x01);
        }

        /* Apply "fuses". */
        for (int i = 0; fuse_bits && fuse_bits[i].width; i++) {
            u32 fuse;
            fuse = (read32(state->fuse_base + fuse_bits[i].src_reg) >> fuse_bits[i].src_bit);
            fuse &= (1 << fuse_bits[i].width) - 1;
            mask32(state->phy_ip_base[phy] + fuse_bits[i].tgt_reg,
                   ((1 << fuse_bits[i].width) - 1) << fuse_bits[i].tgt_bit,
                   fuse << fuse_bits[i].tgt_bit);
        }

        if (state->pcie_regs->type == APCIE_T8140) {
            printf("pcie: T8140 PHY %d up; skipping PHY IP PLL and AUSPMA tunables\n", phy);
        } else {
            char pll_prop[64];
            char auspma_prop[64];

            if (state->num_phys == 1) {
                strcpy(pll_prop, "apcie-phy-ip-pll-tunables");
                strcpy(auspma_prop, "apcie-phy-ip-auspma-tunables");
            } else {
                snprintf(pll_prop, sizeof(pll_prop), "apcie-phy-%d-ip-pll-tunables", phy);
                snprintf(auspma_prop, sizeof(auspma_prop), "apcie-phy-%d-ip-auspma-tunables", phy);
            }

            if (tunables_apply_local_addr(path, pll_prop, state->phy_ip_base[phy])) {
                printf("pcie: Error applying %s for %s\n", pll_prop, path);
                return -1;
            }
            if (tunables_apply_local_addr(path, auspma_prop, state->phy_ip_base[phy])) {
                printf("pcie: Error applying %s for %s\n", auspma_prop, path);
                return -1;
            }
        }

        if (state->pcie_regs->type == APCIE_T602X || state->pcie_regs->compat == APCIE_T8122) {
            set32(state->phy_base[phy] + 4, 0x10);
        }
    }

    if (state->pcie_regs->type == APCIE_T602X || state->pcie_regs->compat == APCIE_T8122 ||
        state->pcie_regs->type == APCIE_T8140) {
        if (state->pcie_regs->type == APCIE_T8140)
            printf("pcie: T8140 PHY common reg[2]+0x4000 clock mode\n");
        mask32(state->phy_common_base + APCIE_PHYCMN_CLK, APCIE_PHYCMN_CLK_MODE,
               FIELD_PREP(APCIE_PHYCMN_CLK_MODE, 1));
    }

    if (state->pcie_regs->type == APCIE_T602X || state->pcie_regs->compat == APCIE_T8122) {
        // Why always PHY 1 in this case?
        u32 off = state->num_phys > 1 ? PHY_STRIDE : 0;
        if (poll32(state->phy_base[0] + off + 0x8, 1, 1, 250000)) {
            printf("pcie: PHY clock enable timed out\n");
            return -1;
        }
        for (int phy = 0; phy < state->num_phys; phy++) {
            if (state->pcie_regs->type == APCIE_T602X) {
                set32(state->phy_base[phy] + APCIE_PHY_CTRL, 0x300);
            } else if (state->pcie_regs->compat == APCIE_T8122) {
                set32(state->phy_base[phy] + APCIE_PHY_CTRL, 0x200);
            }
        }
    }

    if (state->pcie_regs->type == APCIE_T602X || state->pcie_regs->compat == APCIE_T8122 ||
        state->pcie_regs->type == APCIE_T8140) {
        if (state->pcie_regs->type == APCIE_T8140)
            printf("pcie: T8140 RC readiness\n");
        write32(state->rc_base + 0x54, 0x140);
        write32(state->rc_base + 0x50, 0x1);
        if (poll32(state->rc_base + 0x58, 1, 1, 250000)) {
            printf("pcie: Failed to initialize RC thing\n");
            return -1;
        }
        if (state->pcie_regs->type == APCIE_T602X) {
            if (controller == APCIE)
                clear32(state->rc_base + 0x3c, 0x1);
            pmgr_adt_power_disable_index(path, 1);
        }
    }

    for (u32 port = 0; port < state->port_count; port++) {
        char bridge[64];
        int bridge_offset;

        /*
         * Initialize RC port.
         */

        switch (controller) {
            case APCIE:
                snprintf(bridge, sizeof(bridge), "%s/pci-bridge%d", path, port);
                break;
            case APCIE_GE0:
                strcpy(bridge, "/arm-io/apcie-ge0/pci-ge0-bridge");
                break;
            case APCIE_GE1:
                strcpy(bridge, "/arm-io/apcie-ge1/pci-ge1-bridge");
                break;
        }

        if ((bridge_offset = adt_path_offset(adt, bridge)) < 0)
            continue;

        printf("pcie: Initializing port %d\n", port);

        if (adt_get_reg(adt, adt_path, "reg",
                        port * port_reg_cnt + state->pcie_regs->shared_reg_count,
                        &state->port_base[port], NULL)) {
            printf("pcie: Error getting reg with index %d for %s\n",
                   port * port_reg_cnt + state->pcie_regs->shared_reg_count, path);
            return -1;
        }

        if (adt_get_reg(adt, adt_path, "reg",
                        port * port_reg_cnt + state->pcie_regs->shared_reg_count + 1,
                        &state->port_ltssm_base[port], NULL)) {
            printf("pcie: Error getting reg with index %d for %s\n",
                   port * port_reg_cnt + state->pcie_regs->shared_reg_count + 1, path);
            return -1;
        }

        if (adt_get_reg(adt, adt_path, "reg",
                        port * port_reg_cnt + state->pcie_regs->shared_reg_count + 2,
                        &state->port_phy_base[port], NULL)) {
            printf("pcie: Error getting reg with index %d for %s\n",
                   port * port_reg_cnt + state->pcie_regs->shared_reg_count + 2, path);
            return -1;
        }

        if (port_reg_cnt >= 5) {
            if (adt_get_reg(adt, adt_path, "reg",
                            port * port_reg_cnt + state->pcie_regs->shared_reg_count + 4,
                            &state->port_intr2axi_base[port], NULL)) {
                printf("pcie: Error getting reg with index %d for %s\n",
                       port * port_reg_cnt + state->pcie_regs->shared_reg_count + 4, path);
                return -1;
            }
        } else {
            state->port_intr2axi_base[port] = 0;
        }

        if (state->pcie_regs->type == APCIE_T8140)
            printf("pcie: T8140 port %d config reg[%d] setup\n", port,
                   port * port_reg_cnt + state->pcie_regs->shared_reg_count);
        if (state->pcie_regs->type == APCIE_T602X) {
            set32(state->rc_base + 0x3c, 0x1);

            // ??????
            if (controller == APCIE)
                write32(state->port_base[port] + 0x10, 0x2);
        }

        if (state->pcie_regs->type == APCIE_T6031) {
            clear32(state->axi_base + 0x600, BIT(16));
        }

        if (state->pcie_regs->type == APCIE_T602X || state->pcie_regs->compat == APCIE_T8122 ||
            state->pcie_regs->type == APCIE_T8140) {
            write32(state->port_base[port] + 0x88, 0x110);
            write32(state->port_base[port] + 0x100, 0xffffffff);
            write32(state->port_base[port] + 0x148, 0xffffffff);
            write32(state->port_base[port] + 0x210, 0xffffffff);
            write32(state->port_base[port] + 0x80, 0x0);
            write32(state->port_base[port] + 0x84, 0x0);
            if (state->pcie_regs->type == APCIE_T602X) {
                write32(state->port_base[port] + 0x104, 0x7fffffff);
            } else if (state->pcie_regs->compat == APCIE_T8122 ||
                       state->pcie_regs->type == APCIE_T8140) {
                write32(state->port_base[port] + 0x104, 0xfffffff0);
            }
            write32(state->port_base[port] + 0x124, 0x100);
            write32(state->port_base[port] + 0x16c, 0x0);
            write32(state->port_base[port] + 0x13c, 0x10);
            write32(state->port_base[port] + 0x800, 0x100100);
            write32(state->port_base[port] + 0x808, 0x1000ff);
            write32(state->port_base[port] + 0x82c, 0x0);
            if (state->pcie_regs->compat == APCIE_T8122 || state->pcie_regs->type == APCIE_T8140) {
                for (int i = 0; i < 16; i++) {
                    write32(state->port_base[port] + 0x3000 + 4 * i, 0);
                }
            }
            // It seems fine to write the full range (512) here.
            for (int i = 0; i < 512; i++) {
                write32(state->port_base[port] + APCIE_T602X_PORT_MSIMAP + 4 * i, 0);
            }
            if (state->pcie_regs->type == APCIE_T602X) {
                write32(state->port_base[port] + 0x397c, 0x0);
            }
            if (controller == APCIE)
                write32(state->port_base[port] + 0x130, 0x3000000);
            else
                write32(state->port_base[port] + 0x130, 0x3000008);
            write32(state->port_base[port] + 0x140, 0x10);
            write32(state->port_base[port] + 0x144, 0x253770);
            write32(state->port_base[port] + 0x21c, 0x0);
            write32(state->port_base[port] + 0x834, 0x0);
            if (controller != APCIE || state->pcie_regs->compat == APCIE_T8122 ||
                state->pcie_regs->type == APCIE_T8140)
                write32(state->port_base[port] + 0x83c, 0x0);
        }

        if (state->pcie_regs->type == APCIE_T8140)
            printf("pcie: T8140 port %d config tunables\n", port);
        if (tunables_apply_local_addr(bridge, "apcie-config-tunables", state->port_base[port])) {
            printf("pcie: Error applying %s for %s\n", "apcie-config-tunables", bridge);
            return -1;
        }

        if (state->pcie_regs->type == APCIE_T8140)
            printf("pcie: T8140 port %d enabling APPCLK\n", port);
        set32(state->port_base[port] + APCIE_PORT_APPCLK, APCIE_PORT_APPCLK_EN);

        if (state->pcie_regs->type == APCIE_T602X || state->pcie_regs->compat == APCIE_T8122 ||
            state->pcie_regs->type == APCIE_T8140) {
            if (state->pcie_regs->type == APCIE_T8140)
                printf("pcie: T8140 port %d PHY reg[%d] clock/reset sequence\n", port,
                       port * port_reg_cnt + state->pcie_regs->shared_reg_count + 2);
            clear32(state->port_phy_base[port] + APCIE_PHY_CTRL,
                    APCIE_PHY_CTRL_CLK0REQ | APCIE_PHY_CTRL_CLK1REQ);

            set32(state->port_phy_base[port] + APCIE_PHY_CTRL, APCIE_PHY_CTRL_CLK0REQ);
            if (poll32(state->port_phy_base[port] + APCIE_PHY_CTRL, APCIE_PHY_CTRL_CLK0ACK,
                       APCIE_PHY_CTRL_CLK0ACK, 50000)) {
                printf("pcie: Timeout enabling PHY CLK0\n");
                return -1;
            }

            set32(state->port_phy_base[port] + APCIE_PHY_CTRL, APCIE_PHY_CTRL_CLK1REQ);
            if (poll32(state->port_phy_base[port] + APCIE_PHY_CTRL, APCIE_PHY_CTRL_CLK1ACK,
                       APCIE_PHY_CTRL_CLK1ACK, 50000)) {
                printf("pcie: Timeout enabling PHY CLK1\n");
                return -1;
            }

            if (state->pcie_regs->type == APCIE_T602X) {
                clear32(state->port_phy_base[port] + APCIE_PHY_CTRL, 0x4000);
            } else if (state->pcie_regs->compat == APCIE_T8122 ||
                       state->pcie_regs->type == APCIE_T8140) {
                clear32(state->port_phy_base[port] + APCIE_PHY_CTRL, 0x10);
            }
            set32(state->port_phy_base[port] + APCIE_PHY_CTRL, 0x200);
            set32(state->port_phy_base[port] + APCIE_PHY_CTRL, 0x400);

            set32(state->port_base[port] + APCIE_T602X_PORT_RESET, APCIE_PORT_RESET_DIS);
        } else {
            /* PERSTN */
            set32(state->port_base[port] + APCIE_PORT_RESET, APCIE_PORT_RESET_DIS);
        }

        if (state->pcie_regs->type == APCIE_T8140)
            printf("pcie: T8140 port %d waiting for RUN\n", port);
        if (poll32(state->port_base[port] + APCIE_PORT_STATUS, APCIE_PORT_STATUS_RUN,
                   APCIE_PORT_STATUS_RUN, 250000)) {
            printf("pcie: Port failed to come up on %s\n", bridge);
            return -1;
        }

        if (state->pcie_regs->type == APCIE_T602X && controller != APCIE) {
            write32(state->port_ltssm_base[port] + 0x10, 0x2);
            write32(state->port_ltssm_base[port] + 0x1c, 0x4);
            set32(state->port_ltssm_base[port] + 0x20, 0x2);
            write32(state->port_ltssm_base[port] + 0x14, 0x1);

            clear32(state->port_base[port] + APCIE_PORT_APPCLK, 0x100);
        }

        if (poll32(state->port_base[port] + APCIE_PORT_LINKSTS, APCIE_PORT_LINKSTS_BUSY, 0,
                   250000)) {
            printf("pcie: Port failed to become idle on %s\n", bridge);
            return -1;
        }

        /* Do it again? */
        if (state->pcie_regs->type == APCIE_T602X && controller == APCIE) {
            clear32(state->port_base[port] + APCIE_T602X_PORT_RESET, APCIE_PORT_RESET_DIS);
            set32(state->port_base[port] + APCIE_T602X_PORT_RESET, APCIE_PORT_RESET_DIS);

            if (poll32(state->port_base[port] + APCIE_PORT_LINKSTS, APCIE_PORT_LINKSTS_BUSY, 0,
                       250000)) {
                printf("pcie: Port failed to become idle (2) on %s\n", bridge);
                return -1;
            }

            udelay(1000);

            write32(state->port_ltssm_base[port] + 0x10, 0x2);
            write32(state->port_ltssm_base[port] + 0x1c, 0x4);
            set32(state->port_ltssm_base[port] + 0x20, 0x2);
            write32(state->port_ltssm_base[port] + 0x14, 0x1);
        }

        /* Make Designware PCIe Core registers writable. */
        if (state->pcie_regs->type == APCIE_T8140)
            printf("pcie: T8140 port %d ECAM reg[0] DBI and tunables\n", port);
        set32(config_base + DWC_DBI_RO_WR, DWC_DBI_RO_WR_EN);

        if (tunables_apply_local_addr(bridge, "pcie-rc-tunables", config_base)) {
            printf("pcie: Error applying %s for %s\n", "pcie-rc-tunables", bridge);
            return -1;
        }
        if (tunables_apply_local_addr(bridge, "pcie-rc-gen3-shadow-tunables", config_base)) {
            printf("pcie: Error applying %s for %s\n", "pcie-rc-gen3-shadow-tunables", bridge);
            return -1;
        }
        if (tunables_apply_local_addr(bridge, "pcie-rc-gen4-shadow-tunables", config_base)) {
            printf("pcie: Error applying %s for %s\n", "pcie-rc-gen4-shadow-tunables", bridge);
            return -1;
        }

        u32 max_speed;
        if (ADT_GETPROP(adt, bridge_offset, "maximum-link-speed", &max_speed) >= 0) {
            /* Some devices override "maximum-link-speed" in the device child nodes.
             * The property used for the link speed seems to be ad-hoc made up.
             * The 10 GB ethernet adapter uses "target-link-speed" and the SD card
             * reader uses "expected-link-speed". Assume that PCIe link speed override
             * resides in the first (only?) child node.
             */
            if (max_speed == 1) {
                int np = adt_first_child_offset(adt, bridge_offset);
                if (np >= 0) {
                    int target_speed;
                    if (ADT_GETPROP(adt, np, "target-link-speed", &target_speed) >= 0 &&
                        target_speed > 0) {
                        max_speed = target_speed;
                    } else if (ADT_GETPROP(adt, np, "expected-link-speed", &target_speed) >= 0 &&
                               target_speed > 0) {
                        max_speed = target_speed;
                    }
                }
            }

            printf("pcie: Port %d max speed = %d\n", port, max_speed);

            if (max_speed == 0) {
                printf("pcie: Invalid max-speed\n");
                return -1;
            }
            if (max_speed > 4)
                max_speed = 4;

            mask32(config_base + PCIE_CAP_BASE + PCIE_LNKCAP, PCIE_LNKCAP_SLS,
                   FIELD_PREP(PCIE_LNKCAP_SLS, max_speed));

            mask32(config_base + PCIE_CAP_BASE + PCIE_LNKCAP2, PCIE_LNKCAP2_SLS,
                   FIELD_PREP(PCIE_LNKCAP2_SLS, (1 << max_speed) - 1));

            mask16(config_base + PCIE_CAP_BASE + PCIE_LNKCTL2, PCIE_LNKCTL2_TLS,
                   FIELD_PREP(PCIE_LNKCTL2_TLS, max_speed));

            set32(config_base + DWC_DBI_LINK_WIDTH_SPEED_CONTROL, DWC_DBI_SPEED_CHANGE);
        }

        /* Max link width */
        mask32(config_base + DWC_DBI_PORT_LINK_CONTROL, DWC_DBI_PORT_LINK_MODE,
               FIELD_PREP(DWC_DBI_PORT_LINK_MODE, lane_mode));
        mask32(config_base + DWC_DBI_LINK_WIDTH_SPEED_CONTROL, DWC_DBI_LINK_WIDTH,
               FIELD_PREP(DWC_DBI_LINK_WIDTH, link_width));
        mask32(config_base + PCIE_CAP_BASE + PCIE_LNKCAP, PCIE_LNKCAP_MLW,
               FIELD_PREP(PCIE_LNKCAP_MLW, link_width));

        /* Make Designware PCIe Core registers readonly. */
        clear32(config_base + DWC_DBI_RO_WR, DWC_DBI_RO_WR_EN);

        if (state->pcie_regs->type == APCIE_T602X || state->pcie_regs->type == APCIE_T6031) {
            write32(state->port_base[port] + 0x4020, 0x3);
            if (state->port_intr2axi_base[port])
                write32(state->port_intr2axi_base[port] + 0x80, 0x1);

            clear32(state->rc_base + 0x3c, 0x1);
            for (int i = 0; i < 32; i++)
                write32(state->port_base[port] + APCIE_T602X_PORT_MSIMAP + 4 * i, 0x80000000 | i);
        }

        u32 link_status = read32(state->port_base[port] + APCIE_PORT_LINKSTS);
        if (state->pcie_regs->type == APCIE_T8140)
            printf("pcie: T8140 port %d ready, link status 0x%x\n", port, link_status);
        state->active_ports |= BIT(port);

        /* Move to the next PCIe device on this bus. */
        config_base += (1 << 15);
    }

    printf("pcie: Initialized controller %d\n", controller);
    state->initialized = true;
    state->dirty = false;

    return 0;
}

static int pcie_init_body(void)
{
    bool success = false;

    if (pcie_initialized)
        return 0;

    if (chip_id == T8152 && (PCIE_T8152_RECON || PCIE_T8152_WRITES))
        return pcie_t8152_init("/arm-io/apcie");

    if (chip_id == T8140) {
        struct state *state = &controllers[APCIE];
        if (state->dirty) {
            printf("pcie: T8140 controller is partially initialized\n");
            return -1;
        }
        const char *path = "/arm-io/apcie";
        if (adt_path_offset(adt, path) < 0)
            path = "/arm-io/apcie0";
        if (adt_path_offset(adt, path) < 0) {
            printf("pcie: T8140 controller absent\n");
            return 1;
        }
        int ret = pcie_init_controller(APCIE, path);
        if (!ret) {
            pcie_initialized = true;
            return 0;
        }
        if (state->dirty) {
            printf("pcie: T8140 controller failed after power request\n");
            return -1;
        }
        printf("pcie: T8140 controller skipped before MMIO\n");
        return 1;
    }

    success |= pcie_init_controller(APCIE, "/arm-io/apcie") == 0;
    success |= pcie_init_controller(APCIE, "/arm-io/apcie0") == 0;
    success |= pcie_init_controller(APCIE_GE0, "/arm-io/apcie-ge0") == 0;
    success |= pcie_init_controller(APCIE_GE1, "/arm-io/apcie-ge1") == 0;

    if (success)
        pcie_initialized = true;

    return success ? 0 : -1;
}

int pcie_init(void)
{
    debug_capture_start(pcie_log_buf, PCIE_LOG_SIZE);
    int ret = pcie_init_body();
    pcie_log_len = debug_capture_stop();
    return ret;
}

int pcie_shutdown(void)
{
    if (chip_id == T8140) {
        printf("pcie: T8140 shutdown unsupported; leaving controller state intact\n");
        return -1;
    }

    if (!pcie_initialized)
        return 0;

    for (u32 controller = 0; controller < NUM_CONTROLLERS; controller++) {
        struct state *state = &controllers[controller];

        if (!state->initialized)
            continue;

        for (u32 port = 0; port < state->port_count; port++) {
            if (!(state->active_ports & BIT(port)))
                continue;
            if (state->pcie_regs->type == APCIE_T602X)
                clear32(state->port_base[port] + APCIE_T602X_PORT_RESET, APCIE_PORT_RESET_DIS);
            else
                clear32(state->port_base[port] + APCIE_PORT_RESET, APCIE_PORT_RESET_DIS);
            clear32(state->port_base[port] + APCIE_PORT_APPCLK, APCIE_PORT_APPCLK_EN);
        }

        for (int phy = 0; phy < state->num_phys; phy++) {
            if (state->pcie_regs->phy_ctrl_reset)
                clear32(state->phy_base[phy] + APCIE_PHY_CTRL, state->pcie_regs->phy_ctrl_reset);
            clear32(state->phy_base[phy] + APCIE_PHY_CTRL, APCIE_PHY_CTRL_CLK1REQ);
            clear32(state->phy_base[phy] + APCIE_PHY_CTRL, APCIE_PHY_CTRL_CLK0REQ);
        }

        state->initialized = false;
        state->active_ports = 0;
    }

    pcie_initialized = false;
    printf("pcie: Shutdown.\n");

    return 0;
}
