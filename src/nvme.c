/* SPDX-License-Identifier: MIT */

#include "adt.h"
#include "assert.h"
#include "malloc.h"
#include "nvme.h"
#include "pmgr.h"
#include "rtkit.h"
#include "sart.h"
#include "string.h"
#include "utils.h"

#define NVME_TIMEOUT          1000000
#define NVME_ENABLE_TIMEOUT   5000000
#define NVME_SHUTDOWN_TIMEOUT 5000000
#define NVME_QUEUE_SIZE       64
#define NVME_ADMIN_QUEUE_SIZE 2

#define NVME_CAP           0x00
#define NVME_CC            0x14
#define NVME_CC_SHN        GENMASK(15, 14)
#define NVME_CC_SHN_NONE   0
#define NVME_CC_SHN_NORMAL 1
#define NVME_CC_SHN_ABRUPT 2
#define NVME_CC_EN         BIT(0)
#define NVME_CC_IOSQES_64  FIELD_PREP(GENMASK(19, 16), 6)
#define NVME_CC_IOCQES_16  FIELD_PREP(GENMASK(23, 20), 4)
#define NVME_CC_CONFIG     (NVME_CC_IOSQES_64 | NVME_CC_IOCQES_16)

#define NVME_CSTS             0x1c
#define NVME_CSTS_SHST        GENMASK(3, 2)
#define NVME_CSTS_SHST_NORMAL 0
#define NVME_CSTS_SHST_BUSY   1
#define NVME_CSTS_SHST_DONE   2
#define NVME_CSTS_RDY         BIT(0)
#define NVME_CSTS_CFS         BIT(1)

#define NVME_AQA 0x24
#define NVME_ASQ 0x28
#define NVME_ACQ 0x30

#define NVME_DB_ACQ  0x1004
#define NVME_DB_IOCQ 0x100c

#define NVME_BOOT_STATUS    0x1300
#define NVME_BOOT_STATUS_OK 0xde71ce55

#define NVME_LINEAR_SQ_CTRL    0x24908
#define NVME_LINEAR_SQ_CTRL_EN BIT(0)

#define NVME_IOSQ_ADDR           0x1200
#define NVME_IOCQ_ADDR           0x1208
#define NVME_MAX_PEND_CMDS_CTRL  0x1210
#define NVME_DB_LINEAR_ASQ      0x2490c
#define NVME_DB_LINEAR_IOSQ     0x24910

#define NVMMU_NUM       0x28100
#define NVMMU_ASQ_BASE  0x28108
#define NVMMU_IOSQ_BASE 0x28110
#define NVMMU_TCB_INVAL 0x28118
#define NVMMU_TCB_STAT  0x28120

#define NVME_ADMIN_CMD_DELETE_SQ 0x00
#define NVME_ADMIN_CMD_CREATE_SQ 0x01
#define NVME_ADMIN_CMD_DELETE_CQ 0x04
#define NVME_ADMIN_CMD_CREATE_CQ 0x05
#define NVME_QUEUE_CONTIGUOUS    BIT(0)

#define NVME_CMD_FLUSH 0x00
#define NVME_CMD_WRITE 0x01
#define NVME_CMD_READ  0x02

#define NVMMU_TCB_DMA_FROM_DEVICE BIT(0)
#define NVMMU_TCB_DMA_TO_DEVICE   BIT(1)

struct nvme_command {
    u8 opcode;
    u8 flags;
    u8 tag;
    u8 rsvd; // normal NVMe has tag as u16
    u32 nsid;
    u32 cdw2;
    u32 cdw3;
    u64 metadata;
    u64 prp1;
    u64 prp2;
    u32 cdw10;
    u32 cdw11;
    u32 cdw12;
    u32 cdw13;
    u32 cdw14;
    u32 cdw15;
};

struct nvme_completion {
    u64 result;
    u32 rsvd; // normal NVMe has the sq_head and sq_id here
    u16 tag;
    u16 status;
};

struct apple_nvmmu_tcb {
    u8 opcode;
    u8 dma_flags;
    u8 slot_id;
    u8 unk0;
    u16 len;
    u8 unk1[18];
    u64 prp1;
    u64 prp2;
    u64 unk2[2];
    u8 aes_iv[8];
    u8 _aes_unk[64];
};

struct nvme_queue {
    struct apple_nvmmu_tcb *tcbs;
    struct nvme_command *cmds;
    struct nvme_completion *cqes;

    u8 cq_head;
    u8 cq_phase;
    u8 depth;

    bool adminq;
};

static_assert(sizeof(struct nvme_command) == 64, "invalid nvme_command size");
static_assert(sizeof(struct nvme_completion) == 16, "invalid nvme_completion size");
static_assert(sizeof(struct apple_nvmmu_tcb) == 128, "invalid apple_nvmmu_tcb size");

static bool nvme_initialized = false;
static u8 nvme_die;

/* Live bring-up breadcrumb.  This is intentionally exported so a host can
 * poll ANS progress while nvme_init() runs asynchronously on a secondary CPU. */
volatile u32 nvme_progress;
volatile u32 nvme_last_cc;
volatile u32 nvme_last_csts;
volatile u32 nvme_timerless_mode;
volatile u32 nvme_last_mode;
volatile u64 nvme_diag_asq;
volatile u64 nvme_diag_acq;
volatile u32 nvme_diag_aqa;
volatile u64 nvme_diag_admin_tcb;
volatile u64 nvme_diag_io_sq;
volatile u64 nvme_diag_io_cq;
volatile u64 nvme_diag_io_tcb;
volatile u32 nvme_diag_io_sq_sart;
volatile u32 nvme_diag_create_cq_seen;
volatile u32 nvme_diag_create_cq_status;
volatile u64 nvme_diag_create_cq_result;
volatile u32 nvme_diag_create_cq_dma_flags;
volatile u32 nvme_diag_create_sq_seen;
volatile u32 nvme_diag_create_sq_status;
volatile u64 nvme_diag_create_sq_result;
volatile u32 nvme_diag_create_sq_dma_flags;
volatile u32 nvme_diag_nvmmu_num_readback;
volatile u64 nvme_diag_nvmmu_asq_readback;
volatile u64 nvme_diag_nvmmu_iosq_readback;
volatile u32 nvme_diag_io_submit_opcode;
volatile u32 nvme_diag_io_submit_dma_flags;
volatile u32 nvme_diag_io_submit_len;
volatile u64 nvme_diag_io_submit_prp1;

static asc_dev_t *nvme_asc = NULL;
static rtkit_dev_t *nvme_rtkit = NULL;
static sart_dev_t *nvme_sart = NULL;

/* Apple vendor/ANS registers remain in ADT reg[3].  Newer controllers expose
 * the standard NVMe register file through a separate secure BAR. */
static u64 nvme_base;
static u64 nvme_ctrl_base;

static struct nvme_queue adminq, ioq;

static u64 nvme_read64_dwords(u64 addr)
{
    u32 lo = read32(addr);
    u32 hi = read32(addr + 4);

    return ((u64)hi << 32) | lo;
}

static void nvme_t8142_register_io_queues(void)
{
    /*
     * The T8142 SPTM from the matching 25G76 IPSW admits the linear I/O
     * queues through the secure NVMe BAR in this exact order:
     *
     *   IOQA  +0x1210
     *   IOCQ  +0x1208/+0x120c
     *   IOSQ  +0x1200/+0x1204
     *
     * These are not the legacy ANS2 +0x24908 linear-SQ controls.  The
     * controller can accept the standard Create CQ/SQ commands without these
     * CoastGuard registers, but its first host I/O SQ fetch then fails with a
     * DECERR.  Preserve Apple's low-dword/DSB/high-dword ordering here.
     */
    u32 ioqa = ((NVME_QUEUE_SIZE - 1) << 16) | (NVME_QUEUE_SIZE - 1);

    write32(nvme_ctrl_base + NVME_MAX_PEND_CMDS_CTRL, ioqa);
    sysop("dsb sy");

    write32(nvme_ctrl_base + NVME_IOCQ_ADDR, (u32)(u64)ioq.cqes);
    sysop("dsb sy");
    write32(nvme_ctrl_base + NVME_IOCQ_ADDR + 4, (u64)ioq.cqes >> 32);
    sysop("dsb sy");

    write32(nvme_ctrl_base + NVME_IOSQ_ADDR, (u32)(u64)ioq.cmds);
    sysop("dsb sy");
    write32(nvme_ctrl_base + NVME_IOSQ_ADDR + 4, (u64)ioq.cmds >> 32);
    sysop("dsb sy");

    printf("nvme: T8142: secure I/O queues IOQA=0x%x IOSQ=0x%lx IOCQ=0x%lx\n",
           ioqa, (u64)ioq.cmds, (u64)ioq.cqes);
}

static bool nvme_t8142_power_on_domains(void)
{
    /*
     * The M5 ADT does not attach these domains to /arm-io/ans through its
     * intentionally empty clock-gates property.  CoastGuard's separate power
     * aperture aborts until the downstream storage fabric is ACTIVE, so mirror
     * the platform power-domain contract explicitly before the first ASC or
     * SART access.  The recursive helper also brings up each named domain's
     * parents (notably APCIE_SYS_GP for the shared PHY switch).
     */
    static const char *const domains[] = {
        "ANS",
        "APCIE_ST",
        "APCIE_SYS_ST",
        "APCIE_PHY_SW",
    };

    for (size_t i = 0; i < ARRAY_SIZE(domains); ++i) {
        if (pmgr_power_enable_name(nvme_die, domains[i]) < 0) {
            printf("nvme: T8142: failed to power on PMGR domain %s\n", domains[i]);
            return false;
        }
    }

    printf("nvme: T8142: ANS storage fabric power domains active\n");
    return true;
}

struct nvme_deadline {
    u64 value;
    bool timerless;
};

/*
 * A freshly released T8142 secondary can run ordinary C and MMIO at EL2, but
 * architectural timer/system-register reads are not usable until the complete
 * hypervisor per-CPU context is installed.  The read-only host probe explicitly
 * arms nvme_timerless_mode before running ANS on that secondary so the boot CPU
 * can keep servicing USB breadcrumbs.  Do not infer the mode by reading TPIDR:
 * that register access is itself part of the state under test on M5.
 *
 * Preserve real architectural deadlines everywhere else.  Only that diagnostic
 * worker receives a conservative iteration budget; no media write is issued by
 * the probe, and every controller/CQ wait remains bounded.
 */
static struct nvme_deadline nvme_deadline_start(u32 usec)
{
    if (nvme_timerless_mode)
        return (struct nvme_deadline){.value = (u64)usec * 8, .timerless = true};

    return (struct nvme_deadline){.value = timeout_calculate(usec), .timerless = false};
}

static bool nvme_deadline_expired(struct nvme_deadline *deadline)
{
    if (!deadline->timerless)
        return timeout_expired(deadline->value);

    if (!deadline->value)
        return true;

    deadline->value--;
    return false;
}

static bool alloc_queue(struct nvme_queue *q, u8 depth)
{
    if (!depth || depth > NVME_QUEUE_SIZE)
        return false;

    memset(q, 0, sizeof(*q));

    q->tcbs = memalign(SZ_16K, NVME_QUEUE_SIZE * sizeof(*q->tcbs));
    if (!q->tcbs)
        return false;

    q->cmds = memalign(SZ_16K, depth * sizeof(*q->cmds));
    if (!q->cmds)
        goto free_tcbs;

    q->cqes = memalign(SZ_16K, depth * sizeof(*q->cqes));
    if (!q->cqes)
        goto free_cmds;

    memset(q->tcbs, 0, NVME_QUEUE_SIZE * sizeof(*q->tcbs));
    memset(q->cmds, 0, depth * sizeof(*q->cmds));
    memset(q->cqes, 0, depth * sizeof(*q->cqes));
    q->cq_head = 0;
    q->cq_phase = 1;
    q->depth = depth;
    return true;

free_cmds:
    free(q->cmds);
free_tcbs:
    free(q->tcbs);
    return false;
}

static void free_queue(struct nvme_queue *q)
{
    free(q->cmds);
    free(q->tcbs);
    free(q->cqes);
}

static void nvme_poll_syslog(void)
{
    struct rtkit_message msg;
    rtkit_recv(nvme_rtkit, &msg);
}

static bool nvme_ctrl_disable(void)
{
    struct nvme_deadline timeout = nvme_deadline_start(NVME_TIMEOUT);

    nvme_progress = 0x701;
    nvme_last_cc = read32(nvme_ctrl_base + NVME_CC);
    nvme_last_csts = read32(nvme_ctrl_base + NVME_CSTS);
    nvme_progress = 0x702;
    clear32(nvme_ctrl_base + NVME_CC, NVME_CC_EN);
    nvme_progress = 0x703;
    while ((nvme_last_csts = read32(nvme_ctrl_base + NVME_CSTS)) & NVME_CSTS_RDY &&
           !nvme_deadline_expired(&timeout)) {
        nvme_progress = 0x704;
        nvme_poll_syslog();
        nvme_progress = 0x705;
    }

    nvme_last_cc = read32(nvme_ctrl_base + NVME_CC);
    nvme_last_csts = read32(nvme_ctrl_base + NVME_CSTS);
    nvme_progress = 0x706;
    return !(nvme_last_csts & NVME_CSTS_RDY);
}

static bool nvme_ctrl_enable(void)
{
    struct nvme_deadline timeout = nvme_deadline_start(NVME_ENABLE_TIMEOUT);

    if (chip_id == T8142) {
        /* Match Linux/Aurora's two-stage cold enable. CAP is sampled as two
         * 32-bit dwords because this ANS aperture rejects m1n1's read64().
         */
        (void)read32(nvme_ctrl_base + NVME_CAP);
        (void)read32(nvme_ctrl_base + NVME_CAP + 4);
        write32(nvme_ctrl_base + NVME_CC, NVME_CC_CONFIG);
        (void)read32(nvme_ctrl_base + NVME_CAP);
        (void)read32(nvme_ctrl_base + NVME_CAP + 4);
        write32(nvme_ctrl_base + NVME_CC, NVME_CC_CONFIG | NVME_CC_EN);
    } else {
        mask32(nvme_ctrl_base + NVME_CC, NVME_CC_SHN, NVME_CC_EN);
    }
    while (!(read32(nvme_ctrl_base + NVME_CSTS) & NVME_CSTS_RDY) &&
           !nvme_deadline_expired(&timeout))
        nvme_poll_syslog();

    return read32(nvme_ctrl_base + NVME_CSTS) & NVME_CSTS_RDY;
}

static bool nvme_ctrl_shutdown(void)
{
    struct nvme_deadline timeout = nvme_deadline_start(NVME_SHUTDOWN_TIMEOUT);

    mask32(nvme_ctrl_base + NVME_CC, NVME_CC_SHN,
           FIELD_PREP(NVME_CC_SHN, NVME_CC_SHN_NORMAL));
    while (FIELD_GET(NVME_CSTS_SHST, read32(nvme_ctrl_base + NVME_CSTS)) !=
               NVME_CSTS_SHST_DONE &&
           !nvme_deadline_expired(&timeout))
        nvme_poll_syslog();

    return FIELD_GET(NVME_CSTS_SHST, read32(nvme_ctrl_base + NVME_CSTS)) ==
           NVME_CSTS_SHST_DONE;
}

static bool nvme_exec_command(struct nvme_queue *q, struct nvme_command *cmd, u64 *result)
{
    bool found = false;
    struct nvme_deadline timeout;
    /* Admin and I/O queues share the NVMMU tag space.  Linux reserves tags
     * 0 and 1 for the two-entry admin queue, so I/O commands start at 2. */
    u8 tag = q->adminq ? 0 : NVME_ADMIN_QUEUE_SIZE;
    struct nvme_command *queue_cmd = &q->cmds[tag];
    struct apple_nvmmu_tcb *tcb = &q->tcbs[tag];

    memcpy(queue_cmd, cmd, sizeof(*cmd));
    queue_cmd->tag = tag;

    memset(tcb, 0, sizeof(*tcb));
    /* The NVMMU shadow must describe the same command as the SQE. */
    tcb->opcode = queue_cmd->opcode;
    if (!queue_cmd->prp1)
        tcb->dma_flags = 0;
    else if (!q->adminq && queue_cmd->opcode == NVME_CMD_WRITE)
        tcb->dma_flags = NVMMU_TCB_DMA_TO_DEVICE;
    else
        tcb->dma_flags = NVMMU_TCB_DMA_FROM_DEVICE;
    tcb->slot_id = tag;
    tcb->len = queue_cmd->cdw12;
    tcb->prp1 = queue_cmd->prp1;
    tcb->prp2 = queue_cmd->prp2;

    if (!q->adminq) {
        nvme_diag_io_submit_opcode = tcb->opcode;
        nvme_diag_io_submit_dma_flags = tcb->dma_flags;
        nvme_diag_io_submit_len = tcb->len;
        nvme_diag_io_submit_prp1 = tcb->prp1;
    }

    if (q->adminq && queue_cmd->opcode == NVME_ADMIN_CMD_CREATE_CQ)
        nvme_diag_create_cq_dma_flags = tcb->dma_flags;
    else if (q->adminq && queue_cmd->opcode == NVME_ADMIN_CMD_CREATE_SQ)
        nvme_diag_create_sq_dma_flags = tcb->dma_flags;

    /* make sure ANS2 can see the command and tcb before triggering it */
    dma_wmb();

    if (nvme_timerless_mode)
        printf("nvme: submit %s opcode=0x%02x tag=%u prp1=0x%lx cdw10=0x%08x "
               "cdw11=0x%08x tcb-flags=0x%02x tcb-len=0x%x\n",
               q->adminq ? "admin" : "io", queue_cmd->opcode, tag, queue_cmd->prp1,
               queue_cmd->cdw10, queue_cmd->cdw11, tcb->dma_flags, tcb->len);

    nvme_poll_syslog();
    if (q->adminq)
        write32(nvme_base + NVME_DB_LINEAR_ASQ, tag);
    else
        write32(nvme_base + NVME_DB_LINEAR_IOSQ, tag);
    nvme_poll_syslog();

    timeout = nvme_deadline_start(NVME_TIMEOUT);
    struct nvme_completion cqe;
    while (!nvme_deadline_expired(&timeout)) {
        nvme_poll_syslog();

        /* we need a DMA read barrier here since the CQ will be updated using DMA */
        dma_rmb();
        memcpy(&cqe, &q->cqes[q->cq_head], sizeof(cqe));
        if ((cqe.status & 1) != q->cq_phase)
            continue;

        if (cqe.tag == tag) {
            found = true;
            if (result)
                *result = cqe.result;
            if (q->adminq && queue_cmd->opcode == NVME_ADMIN_CMD_CREATE_CQ) {
                nvme_diag_create_cq_seen = 1;
                nvme_diag_create_cq_status = cqe.status;
                nvme_diag_create_cq_result = cqe.result;
            } else if (q->adminq && queue_cmd->opcode == NVME_ADMIN_CMD_CREATE_SQ) {
                nvme_diag_create_sq_seen = 1;
                nvme_diag_create_sq_status = cqe.status;
                nvme_diag_create_sq_result = cqe.result;
            }
        } else {
            printf("nvme: invalid tag in CQ: expected %d but got %d\n", tag, cqe.tag);
        }

        write32(nvme_base + NVMMU_TCB_INVAL, cqe.tag);
        if (read32(nvme_base + NVMMU_TCB_STAT))
            printf("nvme: NVMMU invalidation for tag %d failed\n", cqe.tag);

        /* increment head and switch phase once the end of the queue has been reached */
        q->cq_head += 1;
        if (q->cq_head == q->depth) {
            q->cq_head = 0;
            q->cq_phase ^= 1;
        }

        if (q->adminq)
            write32(nvme_ctrl_base + NVME_DB_ACQ, q->cq_head);
        else
            write32(nvme_ctrl_base + NVME_DB_IOCQ, q->cq_head);
        break;
    }

    if (!found) {
        printf("nvme: could not find command completion in CQ\n");
        return false;
    }

    if (nvme_timerless_mode)
        printf("nvme: complete %s opcode=0x%02x tag=%u raw-status=0x%04x "
               "result=0x%lx cq-head=%u phase=%u\n",
               q->adminq ? "admin" : "io", queue_cmd->opcode, tag, cqe.status, cqe.result,
               q->cq_head, q->cq_phase);

    cqe.status >>= 1;
    if (cqe.status) {
        printf("nvme: command failed with status %d\n", cqe.status);
        return false;
    }

    return true;
}

bool nvme_init(u64 diagnostic_mode)
{
    /* The T8142 diagnostic worker must select its own timeout mode.  A flag
     * written by the boot CPU is not guaranteed to be coherent before the
     * secondary enters ANS, while x0 is delivered as part of the SMP call. */
    bool timerless = diagnostic_mode != 0;
    nvme_timerless_mode = timerless;
    nvme_last_mode = diagnostic_mode;
    nvme_diag_create_cq_seen = 0;
    nvme_diag_create_cq_status = 0xffffffff;
    nvme_diag_create_cq_result = 0;
    nvme_diag_create_cq_dma_flags = 0xffffffff;
    nvme_diag_create_sq_seen = 0;
    nvme_diag_create_sq_status = 0xffffffff;
    nvme_diag_create_sq_result = 0;
    nvme_diag_create_sq_dma_flags = 0xffffffff;
    nvme_diag_nvmmu_num_readback = 0xffffffff;
    nvme_diag_nvmmu_asq_readback = 0;
    nvme_diag_nvmmu_iosq_readback = 0;
    nvme_diag_io_submit_opcode = 0xffffffff;
    nvme_diag_io_submit_dma_flags = 0xffffffff;
    nvme_diag_io_submit_len = 0xffffffff;
    nvme_diag_io_submit_prp1 = 0;
    sysop("dmb sy");
    nvme_progress = 0x01;
    if (timerless)
        printf("nvme: diag progress 0x%03x\n", 0x01);
    if (nvme_initialized) {
        nvme_progress = 0x100;
        printf("nvme: already initialized\n");
        return true;
    }

    int adt_path[8];
    int node = adt_path_offset_trace(adt, "/arm-io/ans", adt_path);
    if (node < 0) {
        nvme_progress = 0x80000001;
        printf("nvme: Error getting NVMe node /arm-io/ans\n");
        return NULL;
    }
    nvme_progress = 0x02;
    if (timerless)
        printf("nvme: diag progress 0x%03x\n", 0x02);

    if (adt_get_reg(adt, adt_path, "reg", 3, &nvme_base, NULL) < 0) {
        nvme_progress = 0x80000002;
        printf("nvme: Error getting NVMe base address.\n");
        return NULL;
    }
    nvme_ctrl_base = nvme_base;
    if (adt_get_property(adt, node, "nvme-secure-bar")) {
        if (adt_get_reg(adt, adt_path, "reg", 9, &nvme_ctrl_base, NULL) < 0) {
            nvme_progress = 0x80000003;
            printf("nvme: nvme-secure-bar is present but ADT reg[9] is missing\n");
            return NULL;
        }
        printf("nvme: standard controller registers use secure BAR 0x%lx\n",
               nvme_ctrl_base);
    }
    nvme_progress = 0x03;
    if (timerless)
        printf("nvme: diag progress 0x%03x\n", 0x03);
    u32 cg_size = 0;
    const u32 *clock_gates = adt_getprop(adt, node, "clock-gates", &cg_size);
    if (!clock_gates || cg_size < sizeof(*clock_gates)) {
        /* J813 is a single-die T8142 machine and its ANS node carries an
         * intentionally empty clock-gates property.  Do not infer a die from
         * the new M5 MMIO aperture using the pre-M5 address encoding. */
        nvme_die = chip_id == T8142 ? 0 : (nvme_base >> 37) & 3;
    } else {
        nvme_die = FIELD_GET(PMGR_DIE_ID, clock_gates[0]);
    }
    nvme_progress = 0x04;
    if (timerless)
        printf("nvme: diag progress 0x%03x\n", 0x04);

    if (chip_id == T8142 && !nvme_t8142_power_on_domains()) {
        nvme_progress = 0x80000005;
        return false;
    }
    nvme_progress = 0x05;
    if (timerless || chip_id == T8142)
        printf("nvme: diag progress 0x%03x\n", 0x05);

    if (!alloc_queue(&adminq, NVME_ADMIN_QUEUE_SIZE)) {
        nvme_progress = 0x80000010;
        printf("nvme: Error allocating admin queue\n");
        return NULL;
    }
    nvme_progress = 0x10;
    if (timerless || chip_id == T8142)
        printf("nvme: diag progress 0x%03x\n", 0x10);
    if (!alloc_queue(&ioq, NVME_QUEUE_SIZE)) {
        nvme_progress = 0x80000011;
        printf("nvme: Error allocating I/O queue\n");
        goto out_adminq;
    }
    nvme_progress = 0x11;
    if (timerless || chip_id == T8142)
        printf("nvme: diag progress 0x%03x\n", 0x11);

    ioq.adminq = false;
    adminq.adminq = true;

    nvme_asc = asc_init("/arm-io/ans");
    if (!nvme_asc) {
        nvme_progress = 0x80000020;
        goto out_ioq;
    }
    nvme_progress = 0x20;
    if (timerless || chip_id == T8142)
        printf("nvme: diag progress 0x%03x\n", 0x20);

    nvme_sart = sart_init("/arm-io/sart-ans");
    if (!nvme_sart) {
        nvme_progress = 0x80000030;
        goto out_asc;
    }
    nvme_diag_admin_tcb = (u64)adminq.tcbs;
    nvme_diag_io_sq = (u64)ioq.cmds;
    nvme_diag_io_cq = (u64)ioq.cqes;
    nvme_diag_io_tcb = (u64)ioq.tcbs;
    nvme_diag_io_sq_sart = 0;

    /* T8142 reports a DECERR while ANS fetches the first host I/O SQ entry.
     * The admin queue is fetched through the secure controller aperture, but
     * the linear I/O SQ path crosses the SART v3 CoastGuard named by the ANS
     * iommu-parent.  Admit the 4 KiB command array, its 8 KiB NVMMU TCB
     * array, and the 4 KiB page containing the completion queue.  Media
     * operations remain read-only and sart_free() removes these temporary
     * entries.
     */
    if (chip_id == T8142) {
        if (!sart_add_allowed_region(nvme_sart, ioq.cmds,
                                     NVME_QUEUE_SIZE * sizeof(*ioq.cmds))) {
            nvme_progress = 0x80000031;
            printf("nvme: T8142: failed to SART-admit I/O submission queue\n");
            goto out_sart;
        }
        nvme_diag_io_sq_sart = 1;
        printf("nvme: T8142: SART-admitted I/O SQ [%p, %p)\n", ioq.cmds,
               (u8 *)ioq.cmds + NVME_QUEUE_SIZE * sizeof(*ioq.cmds));

        if (!sart_add_allowed_region(nvme_sart, ioq.tcbs,
                                     NVME_QUEUE_SIZE * sizeof(*ioq.tcbs))) {
            nvme_progress = 0x80000032;
            printf("nvme: T8142: failed to SART-admit I/O TCB array\n");
            goto out_sart;
        }
        nvme_diag_io_sq_sart |= 2;
        printf("nvme: T8142: SART-admitted I/O TCB [%p, %p)\n", ioq.tcbs,
               (u8 *)ioq.tcbs + NVME_QUEUE_SIZE * sizeof(*ioq.tcbs));

        if (!sart_add_allowed_region(nvme_sart, ioq.cqes, SZ_4K)) {
            nvme_progress = 0x80000033;
            printf("nvme: T8142: failed to SART-admit I/O CQ page\n");
            goto out_sart;
        }
        nvme_diag_io_sq_sart |= 4;
        printf("nvme: T8142: SART-admitted I/O CQ [%p, %p)\n", ioq.cqes,
               (u8 *)ioq.cqes + SZ_4K);
    }
    nvme_progress = 0x30;
    if (timerless || chip_id == T8142)
        printf("nvme: diag progress 0x%03x\n", 0x30);

    if (chip_id == T8142)
        printf("nvme: T8142: creating RTKit transport\n");
    nvme_rtkit = rtkit_init("nvme", nvme_asc, NULL, NULL, nvme_sart, false);
    if (!nvme_rtkit) {
        nvme_progress = 0x80000040;
        goto out_sart;
    }
    nvme_progress = 0x40;
    if (timerless || chip_id == T8142)
        printf("nvme: diag progress 0x%03x\n", 0x40);

    nvme_progress = 0x50;
    if (timerless || chip_id == T8142)
        printf("nvme: diag progress 0x%03x\n", 0x50);
    if (!rtkit_boot(nvme_rtkit)) {
        nvme_progress = 0x80000050;
        goto out_rtkit;
    }
    nvme_progress = 0x51;
    if (timerless || chip_id == T8142)
        printf("nvme: diag progress 0x%03x\n", 0x51);

    if (poll32(nvme_base + NVME_BOOT_STATUS, 0xffffffff, NVME_BOOT_STATUS_OK, USEC_PER_SEC) < 0) {
        nvme_progress = 0x80000060;
        printf("nvme: ANS did not boot correctly.\n");
        goto out_shutdown;
    }
    nvme_progress = 0x60;
    if (timerless || chip_id == T8142)
        printf("nvme: diag progress 0x%03x\n", 0x60);

    /* setup controller and NVMMU for linear submission queue */
    if (chip_id != T8142) {
        set32(nvme_base + NVME_LINEAR_SQ_CTRL, NVME_LINEAR_SQ_CTRL_EN);
        write32(nvme_base + NVME_MAX_PEND_CMDS_CTRL,
                ((NVME_QUEUE_SIZE - 1) << 16) | (NVME_QUEUE_SIZE - 1));
    } else {
        /*
         * T8142 removes the older ANS2 LINEAR_SQ_CTRL and MAX_PEND_CMDS
         * registers.  Touching +0x24908 raises an asynchronous external
         * abort (L2C error address ...e4908) before the first command can be
         * submitted.  Linear NVMMU submission is already the only T8142
         * contract, so neither legacy enable write is required.
         */
        printf("nvme: T8142: skipping legacy linear-SQ control registers\n");
    }
    write32(nvme_base + NVMMU_NUM, NVME_QUEUE_SIZE - 1);
    nvme_diag_nvmmu_num_readback = read32(nvme_base + NVMMU_NUM);
    nvme_progress = 0x70;
    if (timerless || chip_id == T8142)
        printf("nvme: diag progress 0x%03x\n", 0x70);

    /* setup admin queue */
    bool controller_disabled;
    nvme_progress = 0x6f1;
    if (timerless) {
        /* Keep this sequence in nvme_init() for the read-only secondary probe.
         * Entering the generic helper is currently the M5 fault boundary. */
        nvme_progress = 0x700;
        printf("nvme: diag progress 0x%03x\n", 0x700);
        sysop("dmb sy");
        nvme_last_cc = read32(nvme_ctrl_base + NVME_CC);
        nvme_last_csts = read32(nvme_ctrl_base + NVME_CSTS);
        nvme_progress = 0x701;
        printf("nvme: diag progress 0x%03x\n", 0x701);
        clear32(nvme_ctrl_base + NVME_CC, NVME_CC_EN);
        nvme_progress = 0x702;
        printf("nvme: diag progress 0x%03x\n", 0x702);

        u64 budget = (u64)NVME_TIMEOUT * 8;
        while ((nvme_last_csts = read32(nvme_ctrl_base + NVME_CSTS)) & NVME_CSTS_RDY &&
               budget--) {
            nvme_progress = 0x703;
            nvme_poll_syslog();
        }
        controller_disabled = !(nvme_last_csts & NVME_CSTS_RDY);
        nvme_progress = 0x706;
        printf("nvme: diag progress 0x%03x\n", 0x706);
    } else {
        controller_disabled = nvme_ctrl_disable();
    }

    if (!controller_disabled) {
        nvme_progress = 0x80000071;
        printf("nvme: timeout while waiting for CSTS.RDY to clear\n");
        goto out_shutdown;
    }
    nvme_progress = 0x71;
    if (timerless)
        printf("nvme: diag progress 0x%03x\n", 0x71);
    nvme_diag_asq = (u64)adminq.cmds;
    nvme_diag_acq = (u64)adminq.cqes;
    nvme_diag_aqa = ((NVME_ADMIN_QUEUE_SIZE - 1) << 16) | (NVME_ADMIN_QUEUE_SIZE - 1);
    if (diagnostic_mode == 2) {
        /* Leave ANS powered, RTKit running, and the controller disabled so
         * the boot CPU can inspect the untouched queue-register aperture.
         * This mode performs no queue-register writes and no media I/O. */
        nvme_progress = 0x7100;
        sysop("dmb sy");
        return false;
    }
    if (timerless) {
        nvme_progress = 0x720;
        /* T8142 validates the 64-bit queue base when its low dword is
         * written.  DRAM lives above 1 TiB, so low-then-high momentarily
         * presents an invalid 0x00000000xxxxxxxx address and wedges the
         * register aperture.  Seed the high dword before committing low. */
        write32(nvme_ctrl_base + NVME_ASQ + 4, nvme_diag_asq >> 32);
        nvme_progress = 0x721;
        nvme_last_csts = read32(nvme_ctrl_base + NVME_CSTS);
        if (nvme_last_csts & NVME_CSTS_CFS) {
            nvme_progress = 0x80000721;
            goto out_shutdown;
        }
        nvme_progress = 0x722;
        write32(nvme_ctrl_base + NVME_ASQ, (u32)nvme_diag_asq);
        nvme_progress = 0x723;
        nvme_last_csts = read32(nvme_ctrl_base + NVME_CSTS);
        if (nvme_last_csts & NVME_CSTS_CFS) {
            nvme_progress = 0x80000723;
            goto out_shutdown;
        }
        nvme_progress = 0x724;
        write32(nvme_ctrl_base + NVME_ACQ + 4, nvme_diag_acq >> 32);
        nvme_progress = 0x725;
        nvme_last_csts = read32(nvme_ctrl_base + NVME_CSTS);
        if (nvme_last_csts & NVME_CSTS_CFS) {
            nvme_progress = 0x80000725;
            goto out_shutdown;
        }
        nvme_progress = 0x726;
        write32(nvme_ctrl_base + NVME_ACQ, (u32)nvme_diag_acq);
        nvme_progress = 0x727;
        nvme_last_csts = read32(nvme_ctrl_base + NVME_CSTS);
        if (nvme_last_csts & NVME_CSTS_CFS) {
            nvme_progress = 0x80000727;
            goto out_shutdown;
        }
        nvme_progress = 0x728;
        write32(nvme_ctrl_base + NVME_AQA, nvme_diag_aqa);
        nvme_progress = 0x729;
        nvme_last_csts = read32(nvme_ctrl_base + NVME_CSTS);
        if (nvme_last_csts & NVME_CSTS_CFS) {
            nvme_progress = 0x80000729;
            goto out_shutdown;
        }
        nvme_progress = 0x72a;
    } else {
        if (chip_id == T8142) {
            write32(nvme_ctrl_base + NVME_ASQ + 4, nvme_diag_asq >> 32);
            write32(nvme_ctrl_base + NVME_ASQ, (u32)nvme_diag_asq);
            write32(nvme_ctrl_base + NVME_ACQ + 4, nvme_diag_acq >> 32);
            write32(nvme_ctrl_base + NVME_ACQ, (u32)nvme_diag_acq);
        } else {
            write64_lo_hi(nvme_ctrl_base + NVME_ASQ, nvme_diag_asq);
            write64_lo_hi(nvme_ctrl_base + NVME_ACQ, nvme_diag_acq);
        }
        write32(nvme_ctrl_base + NVME_AQA, nvme_diag_aqa);
    }

    /* Keep the Apple ANS queue-programming order used by Linux and Aurora:
     * standard admin queues first, then the linear-submission NVMMU TCB
     * bases.  Programming the TCB bases before ASQ on T8142 makes the first
     * ASQ write poison the controller and the following CSTS access never
     * completes. */
    nvme_progress = 0x72b;
    if (chip_id == T8142) {
        write32(nvme_base + NVMMU_ASQ_BASE + 4, (u64)adminq.tcbs >> 32);
        write32(nvme_base + NVMMU_ASQ_BASE, (u32)(u64)adminq.tcbs);
    } else {
        write64_lo_hi(nvme_base + NVMMU_ASQ_BASE, (u64)adminq.tcbs);
    }
    nvme_progress = 0x72c;
    if (chip_id == T8142) {
        write32(nvme_base + NVMMU_IOSQ_BASE + 4, (u64)ioq.tcbs >> 32);
        write32(nvme_base + NVMMU_IOSQ_BASE, (u32)(u64)ioq.tcbs);
    } else {
        write64_lo_hi(nvme_base + NVMMU_IOSQ_BASE, (u64)ioq.tcbs);
    }
    nvme_progress = 0x72d;
    nvme_diag_nvmmu_asq_readback = nvme_read64_dwords(nvme_base + NVMMU_ASQ_BASE);
    nvme_diag_nvmmu_iosq_readback = nvme_read64_dwords(nvme_base + NVMMU_IOSQ_BASE);

    if (chip_id == T8142) {
        nvme_t8142_register_io_queues();
        nvme_progress = 0x72e;
    }

    bool controller_enabled;
    if (timerless) {
        nvme_progress = 0x710;
        printf("nvme: diag progress 0x%03x\n", 0x710);

        /* Linux and Aurora both use a two-step controller enable sequence.
         * In particular, do not preserve iBoot's inherited CC fields here:
         * T8142 faults when the stale 0x00474000 value is enabled with an RMW.
         */
        /* T8142's CAP register is readable from the primary, but CAP access
         * from the secondary CPU used by this non-destructive probe parks the
         * worker. Use a known-safe CC readback as the ordering barrier here;
         * the primary/production path performs the full CAP sampling sequence.
         */
        nvme_last_cc = read32(nvme_ctrl_base + NVME_CC);
        nvme_progress = 0x712;
        printf("nvme: diag progress 0x%03x\n", 0x712);
        write32(nvme_ctrl_base + NVME_CC, NVME_CC_CONFIG);
        nvme_progress = 0x713;
        printf("nvme: diag progress 0x%03x\n", 0x713);
        nvme_last_cc = read32(nvme_ctrl_base + NVME_CC);
        nvme_progress = 0x714;
        printf("nvme: diag progress 0x%03x\n", 0x714);
        write32(nvme_ctrl_base + NVME_CC, NVME_CC_CONFIG | NVME_CC_EN);
        nvme_progress = 0x716;
        printf("nvme: diag progress 0x%03x\n", 0x716);

        u64 budget = (u64)NVME_ENABLE_TIMEOUT * 8;
        while (!(read32(nvme_ctrl_base + NVME_CSTS) & NVME_CSTS_RDY) && budget--) {
            nvme_progress = 0x717;
            nvme_poll_syslog();
        }
        controller_enabled = read32(nvme_ctrl_base + NVME_CSTS) & NVME_CSTS_RDY;
        nvme_progress = 0x718;
        printf("nvme: diag progress 0x%03x\n", 0x718);
    } else {
        controller_enabled = nvme_ctrl_enable();
    }

    if (!controller_enabled) {
        nvme_progress = 0x80000072;
        printf("nvme: timeout while waiting for CSTS.RDY to be set\n");
        goto out_disable_ctrl;
    }
    nvme_progress = 0x72;
    if (timerless)
        printf("nvme: diag progress 0x%03x\n", 0x72);

    /* setup IO queue */
    struct nvme_command cmd;

    memset(&cmd, 0, sizeof(cmd));
    cmd.opcode = NVME_ADMIN_CMD_CREATE_CQ;
    cmd.prp1 = (u64)ioq.cqes;
    cmd.cdw10 = 1; // cq id
    cmd.cdw10 |= (NVME_QUEUE_SIZE - 1) << 16;
    cmd.cdw11 = NVME_QUEUE_CONTIGUOUS;
    if (!nvme_exec_command(&adminq, &cmd, NULL)) {
        nvme_progress = 0x80000080;
        printf("nvme: create cq command failed\n");
        goto out_disable_ctrl;
    }
    nvme_progress = 0x80;
    if (timerless)
        printf("nvme: diag progress 0x%03x\n", 0x80);

    memset(&cmd, 0, sizeof(cmd));
    cmd.opcode = NVME_ADMIN_CMD_CREATE_SQ;
    cmd.prp1 = (u64)ioq.cmds;
    cmd.cdw10 = 1; // sq id
    cmd.cdw10 |= (NVME_QUEUE_SIZE - 1) << 16;
    cmd.cdw11 = NVME_QUEUE_CONTIGUOUS;
    cmd.cdw11 |= 1 << 16; // cq id for this sq
    if (!nvme_exec_command(&adminq, &cmd, NULL)) {
        nvme_progress = 0x80000081;
        printf("nvme: create sq command failed\n");
        goto out_delete_cq;
    }
    nvme_progress = 0x81;
    if (timerless)
        printf("nvme: diag progress 0x%03x\n", 0x81);

    nvme_initialized = true;
    nvme_progress = 0x100;
    if (timerless)
        printf("nvme: diag progress 0x%03x\n", 0x100);
    printf("nvme: initialized at 0x%lx\n", nvme_base);
    return true;

out_delete_cq:
    memset(&cmd, 0, sizeof(cmd));
    cmd.opcode = NVME_ADMIN_CMD_DELETE_CQ;
    cmd.cdw10 = 1; // cq id
    if (!nvme_exec_command(&adminq, &cmd, NULL))
        printf("nvme: delete cq command failed\n");
out_disable_ctrl:
    nvme_ctrl_shutdown();
    nvme_ctrl_disable();
    nvme_poll_syslog();
out_shutdown:
    rtkit_sleep(nvme_rtkit);
    // Some machines call this ANS, some ANS2...
    pmgr_reset(nvme_die, "ANS");
    pmgr_reset(nvme_die, "ANS2");
out_rtkit:
    rtkit_free(nvme_rtkit);
out_sart:
    sart_free(nvme_sart);
out_asc:
    asc_free(nvme_asc);
out_ioq:
    free_queue(&ioq);
out_adminq:
    free_queue(&adminq);
    return false;
}

void nvme_ensure_shutdown(void)
{
    nvme_asc = asc_init("/arm-io/ans");
    if (!nvme_asc)
        return;

    if (!asc_cpu_running(nvme_asc)) {
        printf("nvme: ANS not running\n");
        asc_free(nvme_asc);
        nvme_asc = NULL;
        return;
    }

    printf("nvme: Found ANS left powered, doing a proper shutdown\n");

    nvme_sart = sart_init("/arm-io/sart-ans");
    if (!nvme_sart)
        goto fail;

    nvme_rtkit = rtkit_init("nvme", nvme_asc, NULL, NULL, nvme_sart, false);
    if (!nvme_rtkit)
        goto fail;

    if (!rtkit_boot(nvme_rtkit))
        goto fail;

    rtkit_sleep(nvme_rtkit);

fail:
    if (nvme_rtkit) {
        rtkit_free(nvme_rtkit);
        nvme_rtkit = NULL;
    }
    if (nvme_sart) {
        sart_free(nvme_sart);
        nvme_sart = NULL;
    }
    asc_free(nvme_asc);
    nvme_asc = NULL;

    // Some machines call this ANS, some ANS2...
    pmgr_reset(nvme_die, "ANS");
    pmgr_reset(nvme_die, "ANS2");
}

void nvme_shutdown(void)
{
    if (!nvme_initialized) {
        // nvme_ensure_shutdown();
        return;
    }

    struct nvme_command cmd;

    memset(&cmd, 0, sizeof(cmd));
    cmd.opcode = NVME_ADMIN_CMD_DELETE_SQ;
    cmd.cdw10 = 1; // sq id
    if (!nvme_exec_command(&adminq, &cmd, NULL))
        printf("nvme: delete sq command failed\n");

    memset(&cmd, 0, sizeof(cmd));
    cmd.opcode = NVME_ADMIN_CMD_DELETE_CQ;
    cmd.cdw10 = 1; // cq id
    if (!nvme_exec_command(&adminq, &cmd, NULL))
        printf("nvme: delete cq command failed\n");

    if (!nvme_ctrl_shutdown())
        printf("nvme: timeout while waiting for controller shutdown\n");
    if (!nvme_ctrl_disable())
        printf("nvme: timeout while waiting for CSTS.RDY to clear\n");

    rtkit_sleep(nvme_rtkit);
    // Some machines call this ANS, some ANS2...
    pmgr_reset(nvme_die, "ANS");
    pmgr_reset(nvme_die, "ANS2");
    rtkit_free(nvme_rtkit);
    sart_free(nvme_sart);
    asc_free(nvme_asc);
    free_queue(&ioq);
    free_queue(&adminq);
    nvme_initialized = false;

    printf("nvme: shutdown done\n");
}

bool nvme_flush(u32 nsid)
{
    struct nvme_command cmd;

    if (!nvme_initialized)
        return false;

    memset(&cmd, 0, sizeof(cmd));
    cmd.opcode = NVME_CMD_FLUSH;
    cmd.nsid = nsid;

    return nvme_exec_command(&ioq, &cmd, NULL);
}

bool nvme_read(u32 nsid, u64 lba, void *buffer)
{
    struct nvme_command cmd;
    u64 buffer_addr = (u64)buffer;
    bool buffer_allowed = false;
    bool result;

    if (!nvme_initialized)
        return false;

    /* no need for 16K alignment here since the NVME page size is 4k */
    if (buffer_addr & (SZ_4K - 1))
        return false;

    memset(&cmd, 0, sizeof(cmd));
    cmd.opcode = NVME_CMD_READ;
    cmd.nsid = nsid;
    cmd.prp1 = (u64)buffer_addr;
    cmd.cdw10 = lba;
    cmd.cdw11 = lba >> 32;
    cmd.cdw12 = 0; // #blocks, 0-based -> 1 block a 4096 bytes

    /* Linux obtains a DMA address for every data buffer before placing it in
     * a PRP.  m1n1 uses physical addresses directly, so T8142's CoastGuard
     * must explicitly admit the one 4 KiB destination page for the duration
     * of this read. */
    if (chip_id == T8142) {
        buffer_allowed = sart_add_allowed_region(nvme_sart, buffer, SZ_4K);
        if (!buffer_allowed) {
            printf("nvme: T8142: failed to SART-admit read buffer %p\n", buffer);
            return false;
        }
    }

    result = nvme_exec_command(&ioq, &cmd, NULL);

    if (buffer_allowed && !sart_remove_allowed_region(nvme_sart, buffer, SZ_4K))
        printf("nvme: T8142: failed to remove read-buffer SART entry\n");

    return result;
}
