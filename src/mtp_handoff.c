/* SPDX-License-Identifier: MIT */

/*
 * J414s preboot MTP/DockChannel handoff for the Windows native-AIC profile.
 *
 * The MTP IOP advertises its HID endpoints by emitting INIT packets on
 * DockChannel.  Reading even one RX data register consumes that state, so
 * this file may only inspect RX_COUNT.  It must never configure thresholds,
 * clear IRQ flags, change masks, or drain the FIFO: AppleMtpHid owns all of
 * that after ExitBootServices.
 */

#include "../config.h"

#include "adt.h"
#include "asc.h"
#include "dapf.h"
#include "dart.h"
#include "hv.h"
#include "iova.h"
#include "mtp_handoff.h"
#include "platform_identity.h"
#include "pmgr.h"
#include "rtkit.h"
#include "string.h"
#include "utils.h"

#if defined(ENABLE_NATIVE_AIC_PASSTHROUGH) && defined(ENABLE_J414S_WINDOWS_MTP_HANDOFF)

#define MTP_PATH             "/arm-io/mtp"
#define MTP_DART_PATH        "/arm-io/dart-mtp"
#define MTP_DOCKCHANNEL_PATH "/arm-io/dockchannel-mtp"

/* DockChannel index 1 is the MTP transport on J414s and on J813. */
#define MTP_DOCKCHANNEL_INDEX 1

/*
 * Which reg tuple of the DART node carries the DAPF registers.  Not a stream
 * id: this is the index dapf_init() passes to adt_get_reg(), and it matches
 * dapf.c's own dapf_entries[] table, which lists {"/arm-io/dart-mtp", 1}.
 * It happened to equal the DART stream on J414s, which is why the two were
 * one constant until J813 pulled them apart.
 */
#define MTP_DAPF_REG_INDEX 1

/*
 * J414s' physical resource layout.  The values are checked before any state
 * is changed because the Windows ACPI resources are intentionally published
 * at these physical addresses too; a different layout needs an explicit port
 * rather than a best-effort boot with the wrong device behind the driver.
 */
#define J414S_MTP_IRQ_BASE      0x2a9b14000ULL
#define J414S_MTP_CONFIG_BASE   0x2a9b30000ULL
#define J414S_MTP_DATA_BASE     0x2a9b34000ULL
#define J414S_MTP_APERTURE_SIZE 0x1000

#define DOCKCHANNEL_DATA_OFFSET 0x4000
#define DOCKCHANNEL_RX_COUNT    0x2c

/*
 * IOP-owned fixed RTKit buffer window.
 *
 * This is deliberately a literal, not an ADT "reg" read.  The live J414s
 * /arm-io/mtp regs are 0x2a9400000/+0x6c000 (the ASC block asc_init() maps)
 * and 0x2a9050000/+0x4000; neither contains the IOP's fixed buffers.  On
 * hardware the first fixed request was ep 0x1 msg 0x101002a9ca8000, i.e. a
 * 4 KiB crashlog buffer at 0x2a9ca8000 -- inside this window and inside
 * nothing else the ADT regs describe.
 *
 * The window itself is pinned upstream: Asahi Linux t602x-die0.dtsi at commit
 * e8efe09d4f378992c890d181d65e2ed8d8cb1194 gives the mtp node
 * reg-names "asc", "sram" with sram = 0x2a9c00000/0x100000, and
 * drivers/soc/apple/rtkit-helper.c at the same commit accepts a nonzero-IOVA
 * buffer request only if it is fully contained in that "sram" resource --
 * exactly the admission rule rtkit_set_phys_window() enforces here.
 *
 * The ADT's own description of this region is the "segment-ranges" property
 * on the MTP IOP nub (the iBoot-preloaded firmware carveout).  We parse and
 * log it below as verification evidence, but do not derive the admission
 * window from it yet: the declared TEXT/DATA segments can end below the
 * IOP's heap allocations (the live crashlog request sits at +0xa8000), while
 * the Linux binding pins the full 1 MiB carveout.
 */
#define J414S_MTP_FIXED_BUFFER_BASE 0x2a9c00000ULL
#define J414S_MTP_FIXED_BUFFER_SIZE 0x100000ULL

/*
 * J813 (M5 MacBook Air, T8142).  Same topology as J414s down to the register
 * offsets inside the DockChannel block -- irq at +0xb14000, config at
 * +0xb30000, data at +0xb34000 from the arm-io base -- so the ADT reg indices
 * and DOCKCHANNEL_DATA_OFFSET below carry over unchanged.
 *
 * ADT reg values under arm-io are arm-io relative; absolute is +0x210000000,
 * verified against uart0 (0x195200000 -> 0x3a5200000, which is EARLY_UART_BASE
 * in soc.h).
 *
 * The fixed-buffer window is the one value that could not be read from the
 * IPSW device tree: iBoot fills segment-ranges in at runtime.  Read from the
 * live machine it is __TEXT 0x394c00000/+0x54000 and __DATA 0x394c54000/
 * +0x6c000, so the carveout starts at 0x394c00000 and the same 1 MiB window
 * the Linux binding pins for J414s covers it with room for the IOP's own heap
 * allocations.
 */
#define J813_MTP_IRQ_BASE           0x394b14000ULL
#define J813_MTP_CONFIG_BASE        0x394b30000ULL
#define J813_MTP_DATA_BASE          0x394b34000ULL
#define J813_MTP_FIXED_BUFFER_BASE  0x394c00000ULL
#define J813_MTP_FIXED_BUFFER_SIZE  0x100000ULL

struct mtp_platform {
    const char *name;
    u64 irq_base;
    u64 config_base;
    u64 data_base;
    u64 fixed_buffer_base;
    u64 fixed_buffer_size;
    /*
     * DART stream the IOP's own DMA goes through, from the ADT: the mapper
     * child of /arm-io/dart-mtp named by /arm-io/mtp's iommu-parent, whose
     * "reg" is the stream id.  J414s uses 1; J813's /arm-io/dart-mtp/mapper-mtp
     * has reg=0 and both /arm-io/mtp and mtp-transport point at it.
     *
     * Getting this wrong is silent and total: every mapping lands in a stream
     * the IOP never consults, so it boots from its carveout, answers HELLO,
     * completes the endpoint map, asks for its crashlog buffer, and then waits
     * forever -- still running, with the buffer untouched.
     */
    u32 dart_stream;
};

static const struct mtp_platform mtp_platform_j414s = {
    .name = "J414s",
    .irq_base = J414S_MTP_IRQ_BASE,
    .config_base = J414S_MTP_CONFIG_BASE,
    .data_base = J414S_MTP_DATA_BASE,
    .fixed_buffer_base = J414S_MTP_FIXED_BUFFER_BASE,
    .fixed_buffer_size = J414S_MTP_FIXED_BUFFER_SIZE,
    .dart_stream = 1,
};

static const struct mtp_platform mtp_platform_j813 = {
    .name = "J813",
    .irq_base = J813_MTP_IRQ_BASE,
    .config_base = J813_MTP_CONFIG_BASE,
    .data_base = J813_MTP_DATA_BASE,
    .fixed_buffer_base = J813_MTP_FIXED_BUFFER_BASE,
    .fixed_buffer_size = J813_MTP_FIXED_BUFFER_SIZE,
    .dart_stream = 0,
};

static const struct mtp_platform *mtp_platform;

/*
 * t8110 DART registers needed to leave stream 1 provably inert if the
 * handoff rolls back.  Offsets mirror src/dart.c (DART_T8110_TCR_OFF,
 * DART_T8110_TLB_CMD, DART_T8110_PROTECT, DART_T8110_DISABLE_STREAMS);
 * dart.c does not export them and dart_shutdown() leaves the stream in
 * BYPASS, which is the one state a failed handoff must not persist.
 */
#define MTP_DART_T8110_TLB_CMD              0x80
#define MTP_DART_T8110_TLB_CMD_BUSY         BIT(31)
#define MTP_DART_T8110_TLB_CMD_OP_FLUSH_SID (1 << 8)
#define MTP_DART_T8110_PROTECT              0x200
#define MTP_DART_T8110_PROTECT_TTBR_TCR     BIT(0)
#define MTP_DART_T8110_DISABLE_STREAMS      0xc20
#define MTP_DART_T8110_TCR(sid)             (0x1000 + 4 * (sid))

/*
 * Keep RTKit's boot-time DART mappings out of the low/null IOVA region and
 * leave a compact, isolated window for the system endpoint buffers it maps.
 *
 * SZ_32M is also the smallest base iovad_init() accepts, and measurement showed
 * the value does not matter here: a window at 0x8000, matching the range the
 * working reference driver uses, produced exactly the same IOP stall with the
 * grant reading back correctly translated.
 */
#define MTP_IOVA_WINDOW_BASE SZ_32M
#define MTP_IOVA_WINDOW_SIZE 0x10000000ULL
/*
 * Bounded by the proxy, not by the IOP: mtp_handoff_init() runs inside
 * hv_init(), which is a proxy call, and proxyclient's UART read timeout is 3
 * seconds (M1N1TIMEOUT in proxy.py).  Blocking m1n1 here for longer than that
 * makes the Python side give up with UartTimeout before the guest ever starts,
 * so this ceiling is a transport constraint and raising it is not an option.
 */
#define MTP_READY_TIMEOUT    (3 * USEC_PER_SEC)

/*
 * Multitouch firmware staging window, in lockstep with the fourth _CRS
 * memory resource in the Windows MTP SSDT (mu/Platform/MacBookAir2026Pkg/
 * AcpiTables/MTP.asl; the J414s original it was ported from lives in
 * MacBookProEarly2023Pkg) and with the Mu MemoryInitPeiLib reservation
 * overlay.
 * AppleMtpHid writes the firmware payload at the CPU physical address and
 * sends the bus address in command 0x95, so the mapping must be established
 * here and must survive into Windows.  The bus address sits above the IOP
 * firmware segment VAs (~0x10c1000) and below the RTKit IOVA window at
 * 0x2000000; the physical carveout is DRAM base + 512 MiB, which Mu removes
 * from the UEFI memory map so Windows never allocates it.
 */
#define MTP_FW_STAGING_DVA  0x1800000ULL
#define MTP_FW_STAGING_PHYS 0x10020000000ULL
#define MTP_FW_STAGING_SIZE 0x100000ULL

/*
 * Preboot RTKit buffer pool: the second half of the same reserved carveout.
 * The MTP IOP requests at least one AP-allocated buffer during boot (oslog,
 * 0x6000 bytes, observed live as 0x0106000000000000) and keeps DMA-writing
 * into every granted buffer after Windows owns the machine, so grants must
 * never come from the m1n1 heap -- that is conventional memory to Mu and
 * Windows.  The Mu MemoryInitPeiLib overlay reserves the full 2 MiB
 * [staging | pool] region out of the UEFI memory map.
 */
#define MTP_RTKIT_POOL_PHYS (MTP_FW_STAGING_PHYS + MTP_FW_STAGING_SIZE)
#define MTP_RTKIT_POOL_SIZE 0x100000ULL

struct mtp_handoff_state {
    asc_dev_t *asc;
    dart_dev_t *dart;
    iova_domain_t *iovad;
    rtkit_dev_t *rtkit;
    u64 irq_base;
    u64 config_base;
    u64 data_base;
    u64 sram_base;
    u64 sram_size;
    u64 dart_regs;
    u32 initial_rx_count;
    bool ready;
};

/*
 * One record of the ADT "segment-ranges" property carried by the MTP IOP nub
 * (compatible "iop-nub,rtbuddy-v2").  Verified against a live T6050 dump:
 * three 32-byte records whose (phys, size) tuples were TEXT 0x294c00000/
 * +0x55000, DATA 0x294c55000/+0x6c000 (the contiguous preloaded carveout)
 * and OS_LOG 0x1000d6a0000/+0x3000 (DRAM), with remap == phys throughout and
 * segment-names "__TEXT;__DATA;__OS_LOG".
 */
struct mtp_segment_range {
    u64 phys;
    u64 iop_va;
    u64 remap;
    u32 size;
    u32 flags;
} PACKED;

static struct mtp_handoff_state mtp_handoff;

static bool mtp_power_enable_if_gated(const char *path)
{
    int node = adt_path_offset(adt, path);

    if (node < 0) {
        printf("mtp-handoff: missing ADT node %s\n", path);
        return false;
    }

    /*
     * Some MTP-related nodes have no PMGR gates.  On J414s /arm-io/mtp carries
     * a clock-gates property that is present but empty, so testing existence
     * alone sent it to pmgr, which requires at least one 32-bit entry and
     * failed with "Error getting /arm-io/mtp clock-gates".  An empty property
     * means the same thing as an absent one: nothing here to power up.
     */
    u32 gates_len = 0;
    if (!adt_getprop(adt, node, "clock-gates", &gates_len) || gates_len < sizeof(u32))
        return true;

    if (pmgr_adt_power_enable(path) < 0) {
        printf("mtp-handoff: could not power %s\n", path);
        return false;
    }

    return true;
}

/*
 * Log the IOP nub's declared firmware carveout.  Read-only ADT evidence: the
 * next hardware capture tells us whether the J414s segment layout matches the
 * pinned 1 MiB window so a follow-up can derive it instead of pinning it.
 * Never fails the handoff.
 */
static void mtp_log_segment_ranges(void)
{
    int node = adt_path_offset(adt, MTP_PATH);
    if (node < 0)
        return;

    u32 len = 0;
    const struct mtp_segment_range *seg = adt_getprop(adt, node, "segment-ranges", &len);
    if (!seg) {
        node = adt_first_child_offset(adt, node);
        if (node >= 0)
            seg = adt_getprop(adt, node, "segment-ranges", &len);
    }

    if (!seg) {
        printf("mtp-handoff: no segment-ranges property on %s or its nub\n", MTP_PATH);
        return;
    }
    if (!len || (len % sizeof(*seg)) != 0) {
        printf("mtp-handoff: unparsed segment-ranges (len %u)\n", len);
        return;
    }

    for (u32 i = 0; i < len / sizeof(*seg); i++)
        printf("mtp-handoff: IOP segment %u phys=%#lx iova=%#lx remap=%#lx size=%#x "
               "flags=%#x\n",
               i, seg[i].phys, seg[i].iop_va, seg[i].remap, seg[i].size, seg[i].flags);

    if (seg[0].phys != mtp_platform->fixed_buffer_base)
        printf("mtp-handoff: WARNING: carveout starts at %#lx, fixed-buffer window "
               "pinned at %#lx\n",
               seg[0].phys, mtp_platform->fixed_buffer_base);
}

static bool mtp_handoff_get_resources(void)
{
    int dockchannel_path[8];
    u64 irq_size;
    u64 config_size;

    if (adt_path_offset_trace(adt, MTP_DOCKCHANNEL_PATH, dockchannel_path) < 0 ||
        adt_get_reg(adt, dockchannel_path, "reg", 1, &mtp_handoff.irq_base, &irq_size) < 0 ||
        adt_get_reg(adt, dockchannel_path, "reg", 2, &mtp_handoff.config_base,
                    &config_size) < 0) {
        printf("mtp-handoff: incomplete DockChannel ADT resources\n");
        return false;
    }

    mtp_handoff.data_base = mtp_handoff.config_base + DOCKCHANNEL_DATA_OFFSET;

    if (irq_size < J414S_MTP_APERTURE_SIZE || config_size < J414S_MTP_APERTURE_SIZE ||
        mtp_handoff.irq_base != mtp_platform->irq_base ||
        mtp_handoff.config_base != mtp_platform->config_base ||
        mtp_handoff.data_base != mtp_platform->data_base) {
        printf("mtp-handoff: unexpected %s DockChannel map irq=%#lx/+%#lx "
               "config=%#lx/+%#lx data=%#lx\n",
               mtp_platform->name, mtp_handoff.irq_base, irq_size, mtp_handoff.config_base,
               config_size, mtp_handoff.data_base);
        return false;
    }

    /* See the *_MTP_FIXED_BUFFER_* comments for why this is not an ADT read. */
    mtp_handoff.sram_base = mtp_platform->fixed_buffer_base;
    mtp_handoff.sram_size = mtp_platform->fixed_buffer_size;

    mtp_log_segment_ranges();

    return true;
}

/*
 * dart_shutdown() leaves the stream TCR in BYPASS_DAPF|BYPASS_DART.  That is
 * acceptable for m1n1's own transient users, but this rollback runs with an
 * MTP IOP that was started and then clamped without a quiesce handshake.  A
 * bypassed stream would let any late or future IOP access reach physical
 * memory unfiltered.  Force the stream to the blocked state instead: TCR 0
 * (neither translate nor bypass), stream disabled, TLB flushed.
 */
static void mtp_handoff_block_dart_stream(void)
{
    if (!mtp_handoff.dart_regs)
        return;

    if (read32(mtp_handoff.dart_regs + MTP_DART_T8110_PROTECT) &
        MTP_DART_T8110_PROTECT_TTBR_TCR) {
        printf("mtp-handoff: DART locked; cannot block stream %d\n", mtp_platform->dart_stream);
        return;
    }

    write32(mtp_handoff.dart_regs + MTP_DART_T8110_TCR(mtp_platform->dart_stream), 0);
    write32(mtp_handoff.dart_regs + MTP_DART_T8110_DISABLE_STREAMS,
            BIT(mtp_platform->dart_stream));
    write32(mtp_handoff.dart_regs + MTP_DART_T8110_TLB_CMD,
            MTP_DART_T8110_TLB_CMD_OP_FLUSH_SID | mtp_platform->dart_stream);
    if (poll32(mtp_handoff.dart_regs + MTP_DART_T8110_TLB_CMD, MTP_DART_T8110_TLB_CMD_BUSY,
               0, 100))
        printf("mtp-handoff: DART TLB flush did not complete\n");

    printf("mtp-handoff: DART stream %d left blocked\n", mtp_platform->dart_stream);
}

static void mtp_handoff_rollback(void)
{
    /* Stop DMA-producing firmware before releasing any RTKit/DART state. */
    if (mtp_handoff.asc)
        asc_cpu_stop(mtp_handoff.asc);
    if (mtp_handoff.rtkit)
        rtkit_free(mtp_handoff.rtkit);
    if (mtp_handoff.asc) {
        asc_free(mtp_handoff.asc);
    }
    if (mtp_handoff.iovad)
        iovad_shutdown(mtp_handoff.iovad, mtp_handoff.dart);
    if (mtp_handoff.dart) {
        dart_shutdown(mtp_handoff.dart);
        mtp_handoff_block_dart_stream();
    }

    memset(&mtp_handoff, 0, sizeof(mtp_handoff));
}

void mtp_handoff_init(void)
{
    if (mtp_handoff.ready)
        return;

    if (platform_is_j414s())
        mtp_platform = &mtp_platform_j414s;
    else if (platform_is_j813())
        mtp_platform = &mtp_platform_j813;
    else
        return;

    printf("mtp-handoff: preparing %s MTP for Windows DockChannel ownership\n",
           mtp_platform->name);

    if (!mtp_handoff_get_resources() || !mtp_power_enable_if_gated(MTP_PATH) ||
        !mtp_power_enable_if_gated(MTP_DART_PATH) ||
        !mtp_power_enable_if_gated(MTP_DOCKCHANNEL_PATH))
        goto fail;

    /*
     * Program DAPF first.  dapf_init() transiently powers a gated DART down
     * once it has programmed the filter, so the persistent power enable above
     * is repeated afterwards before touching the stream's page tables.
     */
    /*
     * Only this DART.  dapf_init_all() was tried, to match the reference's
     * opening p.dapf_init_all() and because the transport is muxed through AOP
     * (hid-transport-mux = "mtp-aop-mux"), but it takes an SError here: the
     * other DARTs it walks (aop, pmp, isp) are still clock-gated this early in
     * hv_init, which kboot.c's late call never has to deal with.
     */
    if (dapf_init(MTP_DART_PATH, MTP_DAPF_REG_INDEX) < 0 ||
        !mtp_power_enable_if_gated(MTP_DART_PATH)) {
        printf("mtp-handoff: DAPF setup failed\n");
        goto fail;
    }

    /*
     * Record the DART MMIO base and the stream's cold TCR before dart_init_adt
     * programs it: the TCR value is the capture that tells us what state a
     * clean rollback should ideally restore, and the base is what lets the
     * rollback force the stream to blocked.
     */
    int dart_path[8];
    if (adt_path_offset_trace(adt, MTP_DART_PATH, dart_path) < 0 ||
        adt_get_reg(adt, dart_path, "reg", 0, &mtp_handoff.dart_regs, NULL) < 0) {
        printf("mtp-handoff: DART stream %d setup failed\n", mtp_platform->dart_stream);
        goto fail;
    }
    printf("mtp-handoff: DART stream %d cold TCR=%#x\n", mtp_platform->dart_stream,
           read32(mtp_handoff.dart_regs + MTP_DART_T8110_TCR(mtp_platform->dart_stream)));

    mtp_handoff.dart = dart_init_adt(MTP_DART_PATH, 0, mtp_platform->dart_stream, false);
    if (!mtp_handoff.dart) {
        printf("mtp-handoff: DART stream %d setup failed\n", mtp_platform->dart_stream);
        goto fail;
    }

    mtp_handoff.iovad =
        iovad_init(MTP_IOVA_WINDOW_BASE, MTP_IOVA_WINDOW_BASE + MTP_IOVA_WINDOW_SIZE);
    if (!mtp_handoff.iovad) {
        printf("mtp-handoff: IOVA allocator setup failed\n");
        goto fail;
    }

    /*
     * Pre-map the firmware staging window before the IOP boots.  Wiping the
     * whole reserved carveout (staging + RTKit pool) first means the IOP can
     * never observe stale DRAM contents through a mapping, and Windows only
     * ever sends command 0x95 after writing a validated payload here.
     */
    memset((void *)MTP_FW_STAGING_PHYS, 0, MTP_FW_STAGING_SIZE + MTP_RTKIT_POOL_SIZE);
    if (dart_map(mtp_handoff.dart, MTP_FW_STAGING_DVA, (void *)MTP_FW_STAGING_PHYS,
                 MTP_FW_STAGING_SIZE) < 0) {
        printf("mtp-handoff: could not map firmware staging window\n");
        goto fail;
    }
    printf("mtp-handoff: firmware staging DVA %#llx -> %#llx/+%#llx\n", MTP_FW_STAGING_DVA,
           MTP_FW_STAGING_PHYS, MTP_FW_STAGING_SIZE);

    mtp_handoff.asc = asc_init(MTP_PATH);
    if (!mtp_handoff.asc) {
        printf("mtp-handoff: MTP ASC setup failed\n");
        goto fail;
    }

    mtp_handoff.rtkit = rtkit_init("mtp-handoff", mtp_handoff.asc, mtp_handoff.dart,
                                   mtp_handoff.iovad, NULL, false);
    /*
     * early AP power: the J813 MTP IOP gates its own ON transition on the AP
     * declaring itself ON first, so m1n1's default order (wait for IOP, then
     * announce AP) deadlocks -- measured as the IOP completing the endpoint
     * map, asking for its crashlog buffer, and then going silent forever with
     * that buffer still all zeros.  Neither the DVA (0x2000000 vs 0x8000) nor
     * the backing physical memory (reserved pool vs m1n1 heap) changed the
     * outcome; the ordering is the only thing that differed from the working
     * reference driver, which announces AP power straight after starting the
     * system endpoints.
     */
    if (!mtp_handoff.rtkit ||
        !rtkit_set_phys_window(mtp_handoff.rtkit, mtp_handoff.sram_base,
                               mtp_handoff.sram_size) ||
        !rtkit_set_buffer_pool(mtp_handoff.rtkit, MTP_RTKIT_POOL_PHYS,
                               MTP_RTKIT_POOL_SIZE) ||
        !rtkit_set_early_ap_power(mtp_handoff.rtkit, true) ||
        !rtkit_boot_timed(mtp_handoff.rtkit, MTP_READY_TIMEOUT)) {
        printf("mtp-handoff: MTP RTKit boot failed\n");
        goto fail;
    }
    /*
     * RX_COUNT is the sole DockChannel register polled. It is non-consuming;
     * reading RX_8/RX_32 would steal INIT from the Windows driver. Do not
     * acknowledge IRQs or set masks/thresholds here. Requiring queued data
     * closes the race between AP=ON and Windows taking transport ownership.
     *
     * Keep servicing the RTKit mailbox while waiting: the IOP logs over
     * syslog during HID bringup and each MSG_SYSLOG_LOG wants an ack, which
     * is exactly what the working Python flow does in wait_init() via
     * mtp.work().  rtkit_recv() touches only the ASC mailbox, never
     * DockChannel, so the non-consuming invariant holds.  After the INIT
     * data shows up we stop for good; from then on the mailbox is
     * deliberately unserviced, per the ownership split with AppleMtpHid.
     */
    u64 timeout = timeout_calculate(MTP_READY_TIMEOUT);
    do {
        struct rtkit_message rtk_msg;
        int ret = rtkit_recv(mtp_handoff.rtkit, &rtk_msg);
        if (ret < 0) {
            printf("mtp-handoff: MTP RTKit failed while waiting for INIT data\n");
            goto fail;
        }
        if (ret > 0)
            printf("mtp-handoff: ignoring app message to endpoint 0x%02x: %lx\n", rtk_msg.ep,
                   rtk_msg.msg);

        mtp_handoff.initial_rx_count =
            read32(mtp_handoff.data_base + DOCKCHANNEL_RX_COUNT);
        if (mtp_handoff.initial_rx_count)
            break;
    } while (!timeout_expired(timeout));

    if (!mtp_handoff.initial_rx_count) {
        printf("mtp-handoff: no DockChannel INIT data after RTKit AP reached ON\n");
        goto fail;
    }
    mtp_handoff.ready = true;

    printf("mtp-handoff: RTKit ready; DockChannel[%d] RX=%u, FIFO preserved "
           "(irq=%#lx config=%#lx data=%#lx sram=%#lx/+%#lx)\n",
           MTP_DOCKCHANNEL_INDEX, mtp_handoff.initial_rx_count, mtp_handoff.irq_base,
           mtp_handoff.config_base, mtp_handoff.data_base, mtp_handoff.sram_base,
           mtp_handoff.sram_size);
    return;

fail:
    mtp_handoff_rollback();
    printf("mtp-handoff: disabled after setup failure; Windows will not receive partial state\n");
}

/*
 * Service the MTP IOP's RTKit mailbox for as long as the guest runs.
 *
 * Handing the DockChannel to Windows does not hand over the mailbox: MTP.asl
 * publishes no ASC aperture and AppleMtpHid contains no mailbox code at all, so
 * once m1n1 stops polling, nothing in the system ever reads it again.
 *
 * That is not merely untidy, it stops input dead.  RTKit syslog requires the AP
 * to acknowledge every MSG_SYSLOG_LOG so the IOP can reuse the log buffer.
 * Measured on J813 with delivery working end to end: the IOP had filled its
 * outbound mailbox with eight unacknowledged syslog entries plus one oslog
 * message (I2A_CONTROL = 0x810801, FULL) and had stopped emitting HID reports
 * entirely -- 60 seconds of typing and swiping produced zero DockChannel bytes.
 * Draining the mailbox by hand emptied it but did not restart the IOP, because
 * what it waits for is the acknowledgement, not the space.
 *
 * The first attempt called rtkit_recv() from hv_tick() under the big
 * hypervisor lock, and Setup crawled.  That call was wrong three ways, none
 * of them frequency: rtkit_recv() drains the entire mailbox per call (the
 * old budget bounded calls, not messages), its replies go through asc_send()
 * which spins up to 200 ms when A2I is full, and a crashed IOP walks the
 * crashlog with a printf per entry -- all under the lock every other core
 * needs to exit the guest.
 *
 * rtkit_service_quiet() exists for exactly this call site: message-bounded,
 * silent on every path, and it refuses to consume anything it could not
 * immediately reply to.  The primary caller is the WFI-idle path in
 * hv_exc.c, which runs without the big lock (same slot as the framebuffer
 * slice work, and for the same reason); hv_tick() calls it too as a 1 Hz
 * floor for the case where no core ever idles.  The busy flag keeps
 * concurrently idling cores from stacking up on the ASC MMIO, and the
 * interval keeps the cost of a WFI trap at one counter read.
 */
static u32 mtp_handoff_poll_interval;
static u64 mtp_handoff_poll_next;
static u32 mtp_handoff_poll_busy;
static bool mtp_handoff_crash_pending;

void mtp_handoff_poll(void)
{
    if (!mtp_handoff.ready || !mtp_handoff.rtkit)
        return;

    if (__atomic_exchange_n(&mtp_handoff_poll_busy, 1, __ATOMIC_ACQUIRE))
        return;

    if (!mtp_handoff_poll_interval)
        mtp_handoff_poll_interval = mrs(CNTFRQ_EL0) / 100; /* 10 ms */

    u64 now = hv_host_counter();
    if (now >= mtp_handoff_poll_next) {
        mtp_handoff_poll_next = now + mtp_handoff_poll_interval;

        if (rtkit_can_recv(mtp_handoff.rtkit) &&
            rtkit_service_quiet(mtp_handoff.rtkit, 4) < 0) {
            /*
             * Crashed IOP: stop touching it for good.  The notice is
             * printed by mtp_handoff_report() from the tick, where the big
             * lock is already held and one bounded printf is acceptable.
             */
            mtp_handoff.ready = false;
            mtp_handoff_crash_pending = true;
        }
    }

    __atomic_store_n(&mtp_handoff_poll_busy, 0, __ATOMIC_RELEASE);
}

void mtp_handoff_report(void)
{
    if (!mtp_handoff_crash_pending)
        return;

    mtp_handoff_crash_pending = false;
    printf("mtp-handoff: MTP IOP crashed; mailbox service stopped\n");
}

void mtp_handoff_map_guest_staging(void)
{
    /*
     * Give the guest the staging window at the *bus* address as well.
     *
     * The fourth _CRS memory resource declares AddressMinimum 0x1800000 (the
     * MTP DART bus address) with an AddressTranslation that raises it to the
     * reserved CPU carveout, on the assumption that Windows applies the
     * translation and hands the driver the physical address.  Measured on J813:
     * it does not.  Windows maps the raw AddressMinimum, so AppleMtpHid's 1 MiB
     * firmware copy faulted at "Unmapped IPA 0x1800000" the moment interrupt
     * delivery let it get that far.
     *
     * Backing that IPA with the same carveout the DART already maps makes the
     * two agree by construction and removes the dependency on translation
     * entirely: the address the driver writes to and the address it sends in
     * command 0x95 are then the same number, and both name the memory the IOP
     * reads.  The window sits well below DRAM base and inside no device
     * aperture, so it collides with nothing else in the guest's IPA space.
     */
    /*
     * Only when the preboot handoff actually ran.  Without it nothing has
     * programmed the DART, the carveout holds no firmware, and there is no
     * reason to place a DRAM window at a low IPA on a board that has no MTP.
     */
    if (!mtp_handoff.ready)
        return;

    if (hv_map_hw(MTP_FW_STAGING_DVA, MTP_FW_STAGING_PHYS, MTP_FW_STAGING_SIZE) < 0) {
        printf("mtp-handoff: could not map guest staging window %#llx -> %#llx\n",
               MTP_FW_STAGING_DVA, MTP_FW_STAGING_PHYS);
        return;
    }

    printf("mtp-handoff: guest staging IPA %#llx -> %#llx/+%#llx\n", MTP_FW_STAGING_DVA,
           MTP_FW_STAGING_PHYS, MTP_FW_STAGING_SIZE);
}

#else

void mtp_handoff_init(void)
{
}

void mtp_handoff_map_guest_staging(void)
{
}

void mtp_handoff_poll(void)
{
}

void mtp_handoff_report(void)
{
}

#endif
