/* SPDX-License-Identifier: MIT */

#include "smp.h"
#include "adt.h"
#include "aic.h"
#include "aic_regs.h"
#include "cpu_regs.h"
#include "memory.h"
#include "pmgr.h"
#include "soc.h"
#include "string.h"
#include "types.h"
#include "utils.h"
#include "xnuboot.h"

#define CPU_START_OFF_S5L8960X 0x30000
#define CPU_START_OFF_S8000    0xd4000
#define CPU_START_OFF_T8103    0x54000
#define CPU_START_OFF_T8112    0x34000
#define CPU_START_OFF_T6020    0x28000
#define CPU_START_OFF_T6031    0x88000

#define CPU_REG_CORE    GENMASK(7, 0)
#define CPU_REG_CLUSTER GENMASK(10, 8)
#define CPU_REG_DIE     GENMASK(14, 11)

#define SMP_SHARED __attribute__((section(".data.smp_shared")))

#define RVBAR_LOCK BIT(0)
#define RVBAR_ADDR GENMASK(47, 12)

struct spin_table {
    u64 mpidr;
    u64 flag;
    u64 target;
    u64 args[4];
    u64 retval;
};

void *_reset_stack SMP_SHARED;
void *_reset_stack_el1 SMP_SHARED;

#define DUMMY_STACK_SIZE 0x1000
u8 dummy_stack[DUMMY_STACK_SIZE];     // Highest EL
u8 dummy_stack_el1[DUMMY_STACK_SIZE]; // EL1 stack if EL3 exists

u8 secondary_stacks[MAX_CPUS][SECONDARY_STACK_SIZE] ALIGNED(0x4000);
u8 secondary_stacks_el3[MAX_EL3_CPUS][SECONDARY_STACK_SIZE] ALIGNED(0x4000);

static bool wfe_mode SMP_SHARED = false;

static int target_cpu SMP_SHARED;
static int cpu_nodes[MAX_CPUS];
static struct spin_table spin_table[MAX_CPUS] SMP_SHARED;

struct cpu_info {
    bool valid;
    u8 die;
    u8 cluster;
    u8 core;
    u64 impl_reg;
};

static bool smp_initialized = false;
static u64 cpu_start_base;
static struct cpu_info cpu_info[MAX_CPUS];

// Used from start.S to find the correct stack after the first entry
struct smp_reset_stack {
    u64 mpidr;
    u64 stack;
};

struct smp_reset_stack smp_reset_stacks[MAX_CPUS] SMP_SHARED = {
    [0 ... MAX_CPUS - 1] = {.mpidr = ~0ULL, .stack = 0},
};

extern u8 _vectors_start[0];
extern u8 _stack_bot[0];
int boot_cpu_idx SMP_SHARED = -1;
u64 boot_cpu_mpidr SMP_SHARED = 0;
static u64 installed_relay SMP_SHARED;

static bool smp_old_vector_owned(u64 locked)
{
    u64 ro_start, ro_end;
    u64 inherited_top = cur_boot_args.top_of_kernel_data;
    u64 image_start = (u64)_base;
    u64 image_end = (u64)_end;

    if (memory_fw_ro_range(&ro_start, &ro_end) != 1 || !cur_boot_args.mem_size ||
        cur_boot_args.phys_base > UINT64_MAX - cur_boot_args.mem_size ||
        locked < cur_boot_args.phys_base ||
        locked >= cur_boot_args.phys_base + cur_boot_args.mem_size || locked >= ro_start ||
        locked >= inherited_top || (locked >= image_start && locked < image_end))
        return false;

    /* A retired m1n1 image has the same four vector slot tags and bare tail. */
    u64 limit = min(ro_start, inherited_top);
    limit = min(limit, locked + min((u64)16 * SZ_1M, UINT64_MAX - locked));
    if (image_start > locked)
        limit = min(limit, image_start);
    if (limit - locked < 0x200 + sizeof("STACKBOT") - 1)
        return false;
    for (u64 off = 0; off <= 0x180; off += 0x80) {
        if (read32(locked + off) != read32((u64)_vectors_start + off))
            return false;
    }
    for (u64 p = locked + 0x200; p <= limit - (sizeof("STACKBOT") - 1); p += 8) {
        if (!memcmp((const void *)p, "STACKBOT", sizeof("STACKBOT") - 1))
            return true;
    }
    return false;
}

static int smp_prepare_rvbar(const struct cpu_info *cpu)
{
    u64 rvbar = read64(cpu->impl_reg);
    u64 locked = rvbar & RVBAR_ADDR;
    u64 target = (u64)_vectors_start;

    if (locked == target || (!(rvbar & RVBAR_LOCK) && cpu_features->apple_sysregs_unlocked))
        return 0;
    if (chip_id != T8140 || !(rvbar & RVBAR_LOCK)) {
        printf("SMP: RVBAR 0x%lx differs from vector 0x%lx; refusing start\n", locked, target);
        return -1;
    }

    int64_t distance = (int64_t)target - (int64_t)locked;
    if ((distance & 3) || distance < -0x8000000 || distance > 0x7fffffc)
        return -1;

    u32 branch = 0x14000000 | ((distance / 4) & 0x03ffffff);
    if (installed_relay == locked)
        return read32(locked) == branch ? 0 : -1;
    if (!smp_old_vector_owned(locked)) {
        printf("SMP: RVBAR 0x%lx is not a verified retired image vector\n", locked);
        return -1;
    }
    u64 page = ALIGN_DOWN(locked, get_page_size());
    if (mmu_active())
        mmu_add_mapping(page, page, get_page_size(), MAIR_IDX_NORMAL, PERM_RW);
    write32(locked, branch);
    dc_cvac_range((void *)locked, sizeof(branch));
    sysop("dsb sy");
    ic_ivau_range((void *)locked, sizeof(branch));
    sysop("dsb sy");
    sysop("isb");
    if (mmu_active())
        mmu_add_mapping(page, page, get_page_size(), MAIR_IDX_NORMAL, PERM_RX);

    if (read32(locked) != branch) {
        printf("SMP: RVBAR relay read-back failed at 0x%lx\n", locked);
        return -1;
    }
    installed_relay = locked;
    printf("SMP: installed RVBAR relay at 0x%lx -> 0x%lx (0x%x)\n", locked, target, branch);
    return 0;
}

void smp_secondary_entry(void)
{
    u64 mpidr = mrs(MPIDR_EL1) & 0xFFFFFF;
    int index = target_cpu;

    // target_cpu identifies us during the initial start handshake, but may
    // be stale on an RVBAR re-entry after deep wfi
    for (int i = 0; i < MAX_CPUS; i++) {
        if (smp_reset_stacks[i].mpidr == mpidr) {
            index = i;
            break;
        }
    }

    smp_reset_stacks[index].stack = (u64)secondary_stacks[index] + SECONDARY_STACK_SIZE;
    smp_reset_stacks[index].mpidr = mpidr;
    sysop("dsb sy");

    struct spin_table *me = &spin_table[index];

    if (in_el2())
        msr(TPIDR_EL2, index);
    else
        msr(TPIDR_EL1, index);

    printf("  Index: %d (table: %p)\n\n", index, me);

    me->mpidr = mpidr;

    sysop("dmb sy");
    me->flag++;
    sysop("dmb sy");
    u64 target;
    if (!cpu_features->fast_ipi)
        aic_write(AIC_IPI_MASK_SET, AIC_IPI_SELF); // we only use the "other" IPI

    while (1) {
        while (!(target = me->target)) {
            if (wfe_mode) {
                sysop("wfe");
            } else {
                deep_wfi();

                if (cpu_features->fast_ipi) {
                    msr(SYS_IMP_APL_IPI_SR_EL1, 1);
                } else {
                    aic_ack(); // Actually read IPI reason
                    aic_write(AIC_IPI_ACK, AIC_IPI_OTHER);
                    aic_write(AIC_IPI_MASK_CLR, AIC_IPI_OTHER);
                }
            }
            sysop("isb");
        }
        sysop("dmb sy");
        me->flag++;
        sysop("dmb sy");
        me->retval = ((u64 (*)(u64 a, u64 b, u64 c, u64 d))target)(me->args[0], me->args[1],
                                                                   me->args[2], me->args[3]);
        sysop("dmb sy");
        me->target = 0;
        sysop("dmb sy");
    }
}

void smp_secondary_prep_el3(void)
{
    msr(TPIDR_EL3, target_cpu);
    return;
}

static int smp_start_cpu(int index, const struct cpu_info *cpu)
{
    int i;

    if (index >= MAX_CPUS)
        return -1;

    if (has_el3() && index >= MAX_EL3_CPUS)
        return -1;

    if (spin_table[index].flag)
        return 0;

    if (smp_prepare_rvbar(cpu))
        return -1;

    printf("Starting CPU %d (%d:%d:%d)... ", index, cpu->die, cpu->cluster, cpu->core);

    memset(&spin_table[index], 0, sizeof(struct spin_table));

    target_cpu = index;
    if (has_el3()) {
        _reset_stack = secondary_stacks_el3[index] + SECONDARY_STACK_SIZE; // EL3
        _reset_stack_el1 = secondary_stacks[index] + SECONDARY_STACK_SIZE; // EL1
    } else
        _reset_stack = secondary_stacks[index] + SECONDARY_STACK_SIZE;

    sysop("dsb sy");

    if (cpu_features->apple_sysregs_unlocked) {
        // This also clears RVBAR_LOCK, so that HV can set RVBAR later when the core is running
        write64(cpu->impl_reg, (u64)_vectors_start);
    }

    u64 start_base = cpu_start_base + cpu->die * PMGR_DIE_OFFSET;

    // Some kind of system level startup/status bit
    // Without this, IRQs don't work
    write32(start_base + 0x4, 1 << (4 * cpu->cluster + cpu->core));

    // Actually start the core
    write32(start_base + 0x8 + 4 * cpu->cluster, 1 << cpu->core);

    for (i = 0; i < 100; i++) {
        sysop("dmb ld");
        if (spin_table[index].flag)
            break;
        udelay(1000);
    }

    if (i >= 100) {
        printf("Failed!\n");
    } else {
        printf("  Started.\n");
    }

    _reset_stack = dummy_stack + DUMMY_STACK_SIZE;
    _reset_stack_el1 = dummy_stack_el1 + DUMMY_STACK_SIZE;
    return i >= 100 ? -1 : 0;
}

static void smp_stop_cpu(int index, const struct cpu_info *cpu, bool deep_sleep)
{
    int i;

    if (index >= MAX_CPUS)
        return;

    if (!spin_table[index].flag)
        return;

    printf("Stopping CPU %d (%d:%d:%d)... ", index, cpu->die, cpu->cluster, cpu->core);

    u64 start_base = cpu_start_base + cpu->die * PMGR_DIE_OFFSET;

    // Request CPU stop
    write32(start_base + 0x0, 1 << (4 * cpu->cluster + cpu->core));

    u64 dsleep = deep_sleep;
    // Put the CPU to sleep
    smp_call1(index, cpu_sleep, dsleep);

    // If going into deep sleep, powering off the last core in a cluster kills our register
    // access, so just wait a bit.
    if (deep_sleep) {
        udelay(10000);
        printf("  Presumed stopped.\n");
        memset(&spin_table[index], 0, sizeof(struct spin_table));
        return;
    }

    // Check that it actually shut down
    for (i = 0; i < 50; i++) {
        sysop("dmb ld");
        if (!(read64(cpu->impl_reg + 0x100) & 0xff))
            break;
        udelay(1000);
    }

    if (i >= 50) {
        printf("Failed!\n");
    } else {
        printf("  Stopped.\n");

        memset(&spin_table[index], 0, sizeof(struct spin_table));
    }
}

int smp_init(void)
{
    if (smp_initialized)
        return 0;

    int pmgr_path[8];
    u64 pmgr_reg;
    u64 cpu_start_off;

    if (pmgr_adt_path_offset_trace(adt, pmgr_path) < 0) {
        printf("Error getting %s node\n", pmgr_name);
        return -1;
    }
    if (adt_get_reg(adt, pmgr_path, "reg", 0, &pmgr_reg, NULL) < 0) {
        printf("Error getting %s regs\n", pmgr_name);
        return -1;
    }

    int arm_io_node;
    if ((arm_io_node = adt_path_offset(adt, "/arm-io")) < 0) {
        printf("Error getting /arm-io node\n");
        return -1;
    }

    int node = adt_path_offset(adt, "/cpus");
    if (node < 0) {
        printf("Error getting /cpus node\n");
        return -1;
    }

    memset(cpu_nodes, 0, sizeof(cpu_nodes));
    memset(cpu_info, 0, sizeof(cpu_info));

    switch (chip_id) {
        case S5L8960X:
        case T7000:
        case T7001:
            cpu_start_off = CPU_START_OFF_S5L8960X;
            break;
        case S8000:
        case S8001:
        case S8003:
        case T8010:
        case T8011:
        case T8012:
        case T8015:
            cpu_start_off = CPU_START_OFF_S8000;
            break;
        case T8103:
        case T6000:
        case T6001:
        case T6002:
            cpu_start_off = CPU_START_OFF_T8103;
            break;
        case T8112:
        case T8122:
        case T8132:
        case T8140:
        case T8142:
        case T8152:
            cpu_start_off = CPU_START_OFF_T8112;
            break;
        case T6020:
        case T6021:
        case T6022:
            cpu_start_off = CPU_START_OFF_T6020;
            break;
        case T6030:
            cpu_start_off = CPU_START_OFF_T8112;
            break;
        case T6031:
        case T6034:
        case T6040:
        case T6041:
        case T6050:
        case T6051:
            cpu_start_off = CPU_START_OFF_T6031;
            break;
        default:
            printf("CPU start offset is unknown for this SoC!\n");
            return -1;
    }

    ADT_FOREACH_CHILD(adt, node)
    {
        u32 cpu_id;

        if (ADT_GETPROP(adt, node, "cpu-id", &cpu_id) < 0)
            if (ADT_GETPROP(adt, node, "reg", &cpu_id) < 0)
                continue;

        if (cpu_id >= MAX_CPUS) {
            printf("cpu-id %d exceeds max CPU count %d: increase MAX_CPUS\n", cpu_id, MAX_CPUS);
            continue;
        }

        cpu_nodes[cpu_id] = node;
    }

    int running_cpu = -1;
    for (int i = 0; i < MAX_CPUS; i++) {
        int cpu_node = cpu_nodes[i];
        if (!cpu_node)
            continue;
        const char *state = adt_getprop(adt, cpu_node, "state", NULL);
        if (state && !strcmp(state, "running")) {
            if (running_cpu >= 0) {
                printf("SMP: multiple running CPUs in ADT\n");
                return -1;
            }
            running_cpu = i;
        }
    }
    if (running_cpu < 0 || (boot_cpu_idx >= 0 && boot_cpu_idx != running_cpu))
        return -1;
    if (boot_cpu_idx == -1) {
        boot_cpu_idx = running_cpu;
        boot_cpu_mpidr = mrs(MPIDR_EL1);
        if (in_el2())
            msr(TPIDR_EL2, boot_cpu_idx);
        else
            msr(TPIDR_EL1, boot_cpu_idx);
    }

    if (boot_cpu_idx == -1) {
        printf(
            "Could not find currently running CPU in cpu table, can't start other processors!\n");
        return -1;
    }

    spin_table[boot_cpu_idx].mpidr = mrs(MPIDR_EL1) & 0xFFFFFF;
    smp_reset_stacks[boot_cpu_idx].stack = (u64)_stack_bot;
    smp_reset_stacks[boot_cpu_idx].mpidr = spin_table[boot_cpu_idx].mpidr;
    sysop("dsb sy");

    for (int i = 0; i < MAX_CPUS; i++) {
        int cpu_node = cpu_nodes[i];

        if (!cpu_node)
            continue;

        u32 reg;
        u64 cpu_impl_reg[2];
        if (ADT_GETPROP(adt, cpu_node, "reg", &reg) < 0)
            continue;
        if (ADT_GETPROP_ARRAY(adt, cpu_node, "cpu-impl-reg", cpu_impl_reg) < 0) {
            u32 reg_len;
            const u64 *regs = adt_getprop(adt, arm_io_node, "reg", &reg_len);
            if (!regs)
                continue;
            u32 index = 2 * i + 2;
            if (reg_len < index)
                continue;
            memcpy(cpu_impl_reg, &regs[index], 16);
        }

        cpu_info[i].valid = true;
        cpu_info[i].core = FIELD_GET(CPU_REG_CORE, reg);
        cpu_info[i].cluster = FIELD_GET(CPU_REG_CLUSTER, reg);
        cpu_info[i].die = FIELD_GET(CPU_REG_DIE, reg);
        cpu_info[i].impl_reg = cpu_impl_reg[0];
    }

    cpu_start_base = pmgr_reg + cpu_start_off;
    smp_initialized = true;

    return 0;
}

int smp_start_secondaries(void)
{
    printf("Starting secondary CPUs...\n");

    if (!smp_initialized)
        return -1;

    if (chip_id == T8140)
        smp_set_wfe_mode(true);

    bool failed = false;
    for (int i = 0; i < MAX_CPUS; i++) {
        struct cpu_info *cpu = &cpu_info[i];

        if (!cpu->valid) {
            if (chip_id == T8140 && cpu_nodes[i]) {
                printf("SMP: ADT CPU %d has no valid implementation register\n", i);
                failed = true;
            }
            continue;
        }

        if (i == boot_cpu_idx) {
            if (smp_prepare_rvbar(cpu)) {
                printf("SMP: boot CPU RVBAR check failed\n");
                return -1;
            }
            // Check if already locked
            if (FIELD_GET(RVBAR_LOCK, read64(cpu->impl_reg)))
                continue;

            // Unlocked, write _vectors_start into boot CPU's rvbar
            write64(cpu->impl_reg, (u64)_vectors_start);
            sysop("dmb sy");

            continue;
        }

        if (smp_start_cpu(i, cpu))
            failed = true;
    }

    if (chip_id == T8140 && failed)
        return -1;
    return 0;
}

void smp_stop_secondaries(bool deep_sleep)
{
    printf("Stopping secondary CPUs...\n");

    if (!smp_initialized)
        return;

    smp_set_wfe_mode(true);

    for (int i = 0; i < MAX_CPUS; i++) {
        struct cpu_info *cpu = &cpu_info[i];

        if (!cpu->valid || i == boot_cpu_idx)
            continue;

        smp_stop_cpu(i, cpu, deep_sleep);
    }
}

void smp_send_ipi(int cpu)
{
    if (cpu >= MAX_CPUS)
        return;

    u64 mpidr = spin_table[cpu].mpidr;
    if (cpu_features->fast_ipi) {
        msr(SYS_IMP_APL_IPI_RR_GLOBAL_EL1, (mpidr & 0xff) | ((mpidr & 0xff00) << 8));
    } else {
        aic_write(AIC_IPI_SEND, AIC_IPI_SEND_CPU(cpu));
    }
}

int smp_call4(int cpu, void *func, u64 arg0, u64 arg1, u64 arg2, u64 arg3)
{
    if (cpu < 0 || cpu >= MAX_CPUS || cpu == boot_cpu_idx || !func)
        return -1;

    struct spin_table *target = &spin_table[cpu];
    if (!target->flag || target->target)
        return -1;

    u64 flag = target->flag;
    target->args[0] = arg0;
    target->args[1] = arg1;
    target->args[2] = arg2;
    target->args[3] = arg3;
    sysop("dmb sy");
    target->target = (u64)func;
    sysop("dsb sy");

    if (wfe_mode)
        sysop("sev");
    else
        smp_send_ipi(cpu);

    u64 started = get_ticks();
    while (target->flag == flag) {
        sysop("dmb sy");
        if (ticks_to_msecs(get_ticks() - started) >= 1000) {
            printf("SMP: CPU %d did not acknowledge call\n", cpu);
            return -1;
        }
    }
    return 0;
}

int smp_wait_timed(int cpu, u64 *retval, u32 timeout_ms)
{
    if (cpu < 0 || cpu >= MAX_CPUS || !retval || !spin_table[cpu].flag)
        return -1;

    struct spin_table *target = &spin_table[cpu];
    u64 started = get_ticks();
    while (target->target) {
        sysop("dmb sy");
        if (ticks_to_msecs(get_ticks() - started) >= timeout_ms) {
            printf("SMP: CPU %d call did not complete\n", cpu);
            return -1;
        }
    }

    *retval = target->retval;
    return 0;
}

u64 smp_wait(int cpu)
{
    u64 retval = 0;
    smp_wait_timed(cpu, &retval, 300000);
    return retval;
}

void smp_set_wfe_mode(bool new_mode)
{
    if (chip_id == T8140 && !new_mode) {
        printf("SMP: T8140 secondaries must remain in WFE mode\n");
        return;
    }
    wfe_mode = new_mode;
    sysop("dsb sy");

    for (int cpu = 0; cpu < MAX_CPUS; cpu++)
        if (cpu != boot_cpu_idx && smp_is_alive(cpu))
            smp_send_ipi(cpu);

    sysop("sev");
}

bool smp_is_alive(int cpu)
{
    if (cpu < 0 || cpu >= MAX_CPUS)
        return false;

    return spin_table[cpu].flag;
}

uint64_t smp_get_mpidr(int cpu)
{
    if (cpu < 0 || cpu >= MAX_CPUS)
        return 0;

    return spin_table[cpu].mpidr;
}

u64 smp_get_release_addr(int cpu)
{
    if (cpu < 0 || cpu >= MAX_CPUS)
        return 0;

    struct spin_table *target = &spin_table[cpu];

    target->args[0] = 0;
    target->args[1] = 0;
    target->args[2] = 0;
    target->args[3] = 0;
    return (u64)&target->target;
}
