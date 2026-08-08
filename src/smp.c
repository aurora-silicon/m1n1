/* SPDX-License-Identifier: MIT */

#include "smp.h"
#include "adt.h"
#include "aic.h"
#include "aic_regs.h"
#include "cpu_regs.h"
#include "malloc.h"
#include "memory.h"
#include "pmgr.h"
#include "soc.h"
#include "string.h"
#include "types.h"
#include "utils.h"

#define CPU_START_OFF_S5L8960X 0x30000
#define CPU_START_OFF_S8000    0xd4000
#define CPU_START_OFF_T8103    0x54000
#define CPU_START_OFF_T8112    0x34000
#define CPU_START_OFF_T6020    0x28000
#define CPU_START_OFF_T6031    0x88000

//
// How many secondary CPUs to bring up. -1 means "all of them".
//
// T8142 (M5) can start secondaries -- the offset above is confirmed working, and
// CPU 1 and CPU 2 both come up and complete the spin-table handshake.
//
// **SMP works.** m1n1 runs boot CPU + 2 secondaries on J704 and gets all the way
// through hypervisor init, PSCI and vGIC setup.
//
// The long-running reset was **CPU 0**, not the number of cores, not any of the
// register writes. See T8142_SKIP_CPU0 below. Skipping it fixed the machine
// outright.
//
// Ruled out along the way, each by its own build:
//   - the third CPU's start (resets at cap 2 as well)
//   - PMU overcurrent / thermal (it resets, it does not power off)
//   - the HV watchdog (hv_wdt_start() only runs from hv.start())
//   - the fast-IPI Apple IMPDEF writes, SYS_IMP_APL_IPI_RR_GLOBAL_EL1 /
//     SYS_IMP_APL_IPI_SR_EL1 -- skipping both entirely did not help
//   - deep_wfi() retention (T8142_PLAIN_WFI, kept anyway)
//   - secondary console traffic (T8142_QUIET_SECONDARY, kept anyway)
//
// Still capped at 2 because 3+ is simply untested, not because it is known bad.
// Raise it and see; the machine is stable at 2.
//
#define SMP_MAX_SECONDARIES ((chip_id == T8142) ? 2 : -1)

//
// T8142: secondaries idle with plain WFI rather than deep_wfi().
//
// deep_wfi() calls Apple's _deep_wfi_helper() retention sequence whenever
// AIDR_EL1 reports architectural retention. That path is unverified on this SoC
// -- features_m4 carries `sleep_mode = SLEEP_NONE` with the comment
// "XXX probably new mode required", and neither cpufreq nor MCC are implemented
// here, so nothing has configured the states it relies on.
//
// A started secondary reaches its idle loop and calls deep_wfi() within
// microseconds of reporting "Started.", which matches when the machine resets.
//
// Plain WFI is architectural: the core parks until an interrupt or event and
// cannot enter an Apple-specific retention state. It idles less efficiently,
// which is irrelevant here.
//
// Ruled out before this: the fast-IPI Apple IMPDEF writes
// (SYS_IMP_APL_IPI_RR_GLOBAL_EL1 / SYS_IMP_APL_IPI_SR_EL1). Skipping both of
// them entirely did not stop the reset, so they are not the cause and are left
// enabled -- IPIs are needed to wake a core out of WFI.
//
#define T8142_PLAIN_WFI (chip_id == T8142)

//
// T8142: keep secondary CPUs off the console.
//
// Narrowed from the logs: with 0 secondaries the boot log continues past
// smp_start_secondaries() into hv_pt_init() ("HV: Initializing for 42-bit PA
// range"). With 2 secondaries it stops right after "Started." and that line never
// appears -- so the machine dies inside smp_start_secondaries() or immediately
// after. The only code in that window is the remaining loop iterations printing
// "Not starting CPU N", smp_set_wfe_mode() and hv_wdt_init(). None of it is
// lethal on its own, and the IMPDEF content of smp_set_wfe_mode() was already
// ruled out by skipping the fast-IPI writes.
//
// What is left is not logic but **contention**. A freshly started secondary
// prints from _cpu_reset_c() and smp_secondary_entry() while the boot CPU is
// also printing, through one spinlock-protected console that drives both the USB
// proxy and the framebuffer. Concurrent USB/dwc3 access from two cores is a good
// way to lose the controller, and it also explains the interleaved text on the
// panel.
//
// So: secondaries stay silent here. Costs the "RVBAR entry on secondary CPU" /
// "Index: N" lines, which we have already seen work.
//
// Ruled out before this: third-CPU start, PMU thermal/overcurrent, HV watchdog,
// the fast-IPI IMPDEF writes, and deep_wfi() retention.
//
#define T8142_QUIET_SECONDARY (chip_id == T8142)

//
// T8142: do not attempt to start CPU 0 at all.
//
// CPU 0 reports "Failed!" on every single run while CPU 1 and CPU 2 start
// cleanly. That is not a no-op: smp_start_cpu() had already written the RVBAR and
// both PMGR start registers before the 100 ms handshake timeout expired. So CPU 0
// is very likely out of reset and executing, just wedged somewhere before it can
// set spin_table[0].flag.
//
// A core loose in that state is a far better candidate for the reset than
// anything else tried so far, and it fits the one observation none of the other
// theories explained: removing unrelated work moved the death point later, which
// is what you would expect if the boot CPU is racing a rogue core rather than
// executing a fatal instruction.
//
// Why CPU 0 specifically is unknown. Bit 0 *is* set in the 0x3bf mask at
// pmgr+0x34000, so it is not masked off there. Possibilities: it is the cluster-0
// primary and needs different handling, its cluster is not powered, or the
// (die, cluster, core) decoding from its ADT reg is wrong for index 0.
//
#define T8142_SKIP_CPU0 (chip_id == T8142)

#define CPU_REG_CORE    GENMASK(7, 0)
#define CPU_REG_CLUSTER GENMASK(10, 8)
#define CPU_REG_DIE     GENMASK(14, 11)

#define RVBAR_LOCK BIT(0)
#define RVBAR_ADDR GENMASK(47, 12)

struct spin_table {
    u64 mpidr;
    u64 flag;
    u64 target;
    u64 args[4];
    u64 retval;
};

void *_reset_stack;
void *_reset_stack_el1;

#define DUMMY_STACK_SIZE 0x1000
u8 dummy_stack[DUMMY_STACK_SIZE];     // Highest EL
u8 dummy_stack_el1[DUMMY_STACK_SIZE]; // EL1 stack if EL3 exists

u8 *secondary_stacks[MAX_CPUS] = {dummy_stack};
u8 *secondary_stacks_el3[MAX_EL3_CPUS];

static bool wfe_mode = false;

static int target_cpu;
static int cpu_nodes[MAX_CPUS];
static struct spin_table spin_table[MAX_CPUS];
static u64 pmgr_reg;
static u64 cpu_start_off;
//
// Whether cpu_start_off above is actually known for this SoC. T8142 was in this
// position until its offset was found empirically (see the T8142 case below);
// any future unknown SoC lands here rather than writing an arbitrary PMGR
// register on a guess.
//
// This must not be conflated with "cannot proceed at all". Discovering which
// CPU we are booted on (boot_cpu_idx) and writing the boot CPU's own RVBAR use
// no PMGR start register whatsoever -- only starting *secondary* cores does.
// Bailing out early left boot_cpu_idx == -1, which made hv_start() refuse with
// "Boot CPU has not been found, can't start hypervisor" and blocked the
// hypervisor entirely on an otherwise perfectly usable single core.
//
static bool cpu_start_off_known;

extern u8 _vectors_start[0];
int boot_cpu_idx = -1;
u64 boot_cpu_mpidr = 0;

void smp_secondary_entry(void)
{
    struct spin_table *me = &spin_table[target_cpu];

    if (in_el2())
        msr(TPIDR_EL2, target_cpu);
    else
        msr(TPIDR_EL1, target_cpu);

    // See T8142_QUIET_SECONDARY.
    if (!T8142_QUIET_SECONDARY)
        printf("  Index: %d (table: %p)\n\n", target_cpu, me);

    me->mpidr = mrs(MPIDR_EL1) & 0xFFFFFF;

    sysop("dmb sy");
    me->flag = 1;
    sysop("dmb sy");
    u64 target;
    if (!cpu_features->fast_ipi)
        aic_write(AIC_IPI_MASK_SET, AIC_IPI_SELF); // we only use the "other" IPI

    while (1) {
        while (!(target = me->target)) {
            if (wfe_mode) {
                sysop("wfe");
            } else {
                // See T8142_PLAIN_WFI.
                if (T8142_PLAIN_WFI)
                    sysop("wfi");
                else
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
        me->retval = ((u64(*)(u64 a, u64 b, u64 c, u64 d))target)(me->args[0], me->args[1],
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

//
// Bit position for a core in the *global* CPU start/stop bitmaps at
// cpu_start_base + 0x0 and cpu_start_base + 0x4.
//
// The stock formula, 1 << (4 * cluster + core), assumes every cluster holds four
// cores so that cluster N begins at bit 4*N. That is wrong on T8142, whose two
// clusters are asymmetric -- 6 E-cores (cpu0..cpu5) then 4 P-cores (cpu6..cpu9):
//
//   - the mask this SoC actually reports at pmgr+0x34000 is 0x3bf: bits 0..9
//     set except bit 6, and bit 6 is the boot CPU (smp_id 0x6 == cpu6 ==
//     cluster 1 core 0). That is a linear cpu index, not a 4-wide stride.
//   - 4 * cluster + core cannot even reach bits 8 and 9 here -- its maximum is
//     4*1 + 3 = 7 -- so it is incapable of producing the observed value.
//
// So on T8142 these bitmaps are indexed by the linear ADT cpu index.
//
// This has been latent rather than fatal only because the sole secondaries ever
// started are cpu1 and cpu2, both in cluster 0, where the two formulas agree.
// Starting any cluster-1 core would have written bits 4..7 -- including bit 6,
// the *running boot CPU*.
//
// The per-cluster register at cpu_start_base + 0x8 + 4*cluster is unaffected: it
// is already cluster-indexed and its bit is core-relative, which stays correct
// for a 6-core cluster.
//
static inline u32 cpu_start_bit(int index, int cluster, int core)
{
    if (chip_id == T8142)
        return 1 << index;

    return 1 << (4 * cluster + core);
}

static void smp_start_cpu(int index, int die, int cluster, int core, u64 impl, u64 cpu_start_base)
{
    int i;

    if (index >= MAX_CPUS)
        return;

    if (has_el3() && index >= MAX_EL3_CPUS)
        return;

    if (spin_table[index].flag)
        return;

    if ((read64(impl) & RVBAR_LOCK) &&
        (read64(impl) & RVBAR_ADDR) != (u64)_vectors_start) {
        printf("Failed! \n    RVBAR (=0x%lx) is locked and differs from entry point (=0x%lx)\n",
               read64(impl) & RVBAR_ADDR, (u64)_vectors_start);
    }

    printf("Starting CPU %d (%d:%d:%d)... ", index, die, cluster, core);

    memset(&spin_table[index], 0, sizeof(struct spin_table));

    target_cpu = index;
    secondary_stacks[index] = memalign(0x4000, SECONDARY_STACK_SIZE);
    if (has_el3()) {
        secondary_stacks_el3[index] = memalign(0x4000, SECONDARY_STACK_SIZE);
        _reset_stack = secondary_stacks_el3[index] + SECONDARY_STACK_SIZE; // EL3
        _reset_stack_el1 = secondary_stacks[index] + SECONDARY_STACK_SIZE; // EL1

        dc_civac_range(&_reset_stack_el1, sizeof(void *));
    } else
        _reset_stack = secondary_stacks[index] + SECONDARY_STACK_SIZE;

    dc_civac_range(&_reset_stack, sizeof(void *));

    sysop("dsb sy");

    if (!(read64(impl) & RVBAR_LOCK)) {
        // This also clears RVBAR_LOCK, so that HV can set RVBAR later when the core is running
        write64(impl, (u64)_vectors_start);
    }

    cpu_start_base += die * PMGR_DIE_OFFSET;

    // Some kind of system level startup/status bit
    // Without this, IRQs don't work
    write32(cpu_start_base + 0x4, cpu_start_bit(index, cluster, core));

    // Actually start the core
    write32(cpu_start_base + 0x8 + 4 * cluster, 1 << core);

    for (i = 0; i < 100; i++) {
        sysop("dmb ld");
        if (spin_table[index].flag)
            break;
        udelay(1000);
    }

    if (i >= 100)
        printf("Failed!\n");
    else
        printf("  Started.\n");

    _reset_stack = dummy_stack + DUMMY_STACK_SIZE;
    _reset_stack_el1 = dummy_stack_el1 + DUMMY_STACK_SIZE;
}

static void smp_stop_cpu(int index, int die, int cluster, int core, u64 impl, u64 cpu_start_base,
                         bool deep_sleep)
{
    int i;

    if (index >= MAX_CPUS)
        return;

    if (!spin_table[index].flag)
        return;

    printf("Stopping CPU %d (%d:%d:%d)... ", index, die, cluster, core);

    cpu_start_base += die * PMGR_DIE_OFFSET;

    // Request CPU stop
    write32(cpu_start_base + 0x0, cpu_start_bit(index, cluster, core));

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
        if (!(read64(impl + 0x100) & 0xff))
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

void smp_start_secondaries(void)
{
    printf("Starting secondary CPUs...\n");

    int pmgr_path[8];

    if (adt_path_offset_trace(adt, "/arm-io/pmgr", pmgr_path) < 0) {
        printf("Error getting /arm-io/pmgr node\n");
        return;
    }
    if (adt_get_reg(adt, pmgr_path, "reg", 0, &pmgr_reg, NULL) < 0) {
        printf("Error getting /arm-io/pmgr regs\n");
        return;
    }

    int arm_io_node;
    if ((arm_io_node = adt_path_offset(adt, "/arm-io")) < 0) {
        printf("Error getting /arm-io node\n");
        return;
    }

    int node = adt_path_offset(adt, "/cpus");
    if (node < 0) {
        printf("Error getting /cpus node\n");
        return;
    }

    memset(cpu_nodes, 0, sizeof(cpu_nodes));

    cpu_start_off_known = true;

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
        case T8142:
            //
            // Determined empirically on J704 (M5) by dumping the pmgr window
            // read-only from the hypervisor shell, 0x4000 at a time:
            //
            //   pmgr reg[0] = 0x380700000
            //   0x30000: 012206d7 00000000 00000000 00000000
            //   0x34000: 000003bf 000003bf 00000000 00000000   <--
            //   0x38000: 00000001 00000000 00000000 00000000
            //   0x3c000 .. 0x58000: all zero
            //
            // 0x3bf is 0b11_1011_1111: bits 0..9 set except bit 6. That is ten
            // cores with exactly the running one masked out -- m1n1 boots on
            // smp_id 6, which is cpu6. These registers are indexed by the
            // *linear* cpu index on this SoC, not 1 << (4*cluster + core): the
            // clusters are 6 E-cores (cpu0..5) then 4 P-cores (cpu6..9), and a
            // 4-wide stride could not set bits 8/9 at all. See cpu_start_bit().
            // The surrounding block being zero marks it out from the pmgr
            // power-state registers, which read like 0x...0f.
            //
            // Same offset T8112 / T8122 / T6030 use, which is corroboration but
            // was not the reason for choosing it.
            //
            cpu_start_off = CPU_START_OFF_T8112;
            break;
        case T6031:
        case T6034:
            cpu_start_off = CPU_START_OFF_T6031;
            break;
        default:
            printf("CPU start offset is unknown for this SoC! Secondary CPUs will not be "
                   "started; continuing single-core.\n");
            cpu_start_off_known = false;
            break;
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

    /* The boot cpu id never changes once set */
    if (boot_cpu_idx == -1) {
        /* Figure out which CPU we are on by seeing which CPU is running */

        /* This seems silly but it's what XNU does */
        for (int i = 0; i < MAX_CPUS; i++) {
            int cpu_node = cpu_nodes[i];
            if (!cpu_node)
                continue;
            const char *state = adt_getprop(adt, cpu_node, "state", NULL);
            if (!state)
                continue;
            if (strcmp(state, "running") == 0) {
                boot_cpu_idx = i;
                boot_cpu_mpidr = mrs(MPIDR_EL1);
                if (in_el2())
                    msr(TPIDR_EL2, boot_cpu_idx);
                else
                    msr(TPIDR_EL1, boot_cpu_idx);
                break;
            }
        }
    }

    if (boot_cpu_idx == -1) {
        printf(
            "Could not find currently running CPU in cpu table, can't start other processors!\n");
        return;
    }

    spin_table[boot_cpu_idx].mpidr = mrs(MPIDR_EL1) & 0xFFFFFF;

    int started = 0;

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

        if (i == boot_cpu_idx) {
            // Check if already locked
            if (FIELD_GET(RVBAR_LOCK, read64(cpu_impl_reg[0])))
                continue;

            // Unlocked, write _vectors_start into boot CPU's rvbar
            write64(cpu_impl_reg[0], (u64)_vectors_start);
            sysop("dmb sy");

            continue;
        }

        // Everything above this point -- boot CPU discovery and the boot CPU's
        // own RVBAR write -- works without cpu_start_off. Only the actual
        // secondary start below needs it.
        if (!cpu_start_off_known)
            continue;

        // See T8142_SKIP_CPU0.
        if (T8142_SKIP_CPU0 && i == 0) {
            printf("Not starting CPU 0: known to fail on this SoC, see T8142_SKIP_CPU0\n");
            continue;
        }

        u8 core = FIELD_GET(CPU_REG_CORE, reg);
        u8 cluster = FIELD_GET(CPU_REG_CLUSTER, reg);
        u8 die = FIELD_GET(CPU_REG_DIE, reg);

        //
        // Cap on how many secondaries we bring up. See SMP_MAX_SECONDARIES.
        //
        if (SMP_MAX_SECONDARIES >= 0 && started >= SMP_MAX_SECONDARIES) {
            printf("Not starting CPU %d: at the SMP_MAX_SECONDARIES limit of %d\n", i,
                   SMP_MAX_SECONDARIES);
            continue;
        }

        smp_start_cpu(i, die, cluster, core, cpu_impl_reg[0], pmgr_reg + cpu_start_off);

        if (spin_table[i].flag)
            started++;
    }
}

void smp_stop_secondaries(bool deep_sleep)
{
    // Nothing was ever started, and pmgr_reg + cpu_start_off would be an
    // arbitrary PMGR register. See cpu_start_off_known.
    if (!cpu_start_off_known)
        return;

    printf("Stopping secondary CPUs...\n");
    int arm_io_node;
    if ((arm_io_node = adt_path_offset(adt, "/arm-io")) < 0) {
        printf("Error getting /arm-io node\n");
        return;
    }
    smp_set_wfe_mode(true);

    for (int i = 0; i < MAX_CPUS; i++) {
        int node = cpu_nodes[i];

        if (!node)
            continue;

        u32 reg;
        u64 cpu_impl_reg[2];
        if (ADT_GETPROP(adt, node, "reg", &reg) < 0)
            continue;
        if (ADT_GETPROP_ARRAY(adt, node, "cpu-impl-reg", cpu_impl_reg) < 0) {
            u32 reg_len;
            const u64 *regs = adt_getprop(adt, arm_io_node, "reg", &reg_len);
            if (!regs)
                continue;
            u32 index = 2 * i + 2;
            if (reg_len < index)
                continue;
            memcpy(cpu_impl_reg, &regs[index], 16);
        }

        u8 core = FIELD_GET(CPU_REG_CORE, reg);
        u8 cluster = FIELD_GET(CPU_REG_CLUSTER, reg);
        u8 die = FIELD_GET(CPU_REG_DIE, reg);

        smp_stop_cpu(i, die, cluster, core, cpu_impl_reg[0], pmgr_reg + cpu_start_off, deep_sleep);
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

void smp_call4(int cpu, void *func, u64 arg0, u64 arg1, u64 arg2, u64 arg3)
{
    if (cpu >= MAX_CPUS)
        return;

    struct spin_table *target = &spin_table[cpu];

    if (cpu == boot_cpu_idx)
        return;

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

    while (target->flag == flag)
        sysop("dmb sy");
}

u64 smp_wait(int cpu)
{
    if (cpu >= MAX_CPUS)
        return 0;

    struct spin_table *target = &spin_table[cpu];

    while (target->target)
        sysop("dmb sy");

    return target->retval;
}

void smp_set_wfe_mode(bool new_mode)
{
    wfe_mode = new_mode;
    sysop("dsb sy");

    for (int cpu = 0; cpu < MAX_CPUS; cpu++)
        if (cpu != boot_cpu_idx && smp_is_alive(cpu))
            smp_send_ipi(cpu);

    sysop("sev");
}

bool smp_is_alive(int cpu)
{
    if (cpu >= MAX_CPUS)
        return false;

    return spin_table[cpu].flag;
}

uint64_t smp_get_mpidr(int cpu)
{
    if (cpu >= MAX_CPUS)
        return 0;

    return spin_table[cpu].mpidr;
}

int smp_get_id(uint64_t mpidr){
    for(int cpu = 0; cpu < MAX_CPUS; cpu++){
        if(MPIDR_AFF3(spin_table[cpu].mpidr) == MPIDR_AFF3(mpidr) &&
            MPIDR_AFF2(spin_table[cpu].mpidr) == MPIDR_AFF2(mpidr) &&
            MPIDR_AFF1(spin_table[cpu].mpidr) == MPIDR_AFF1(mpidr) &&
            MPIDR_AFF0(spin_table[cpu].mpidr) == MPIDR_AFF0(mpidr)){
            return cpu;
        }
    }
    return 0;
}

/*
 * Number of CPU nodes actually populated in the ADT, i.e. the number of
 * entries `smp_start_secondaries()` found while walking `/cpus` and
 * recording each child's `cpu-id`/`reg` into `cpu_nodes[]` (above). This is
 * the SoC's *real*, bin-aware core count -- e.g. a binned 10-core M2 Pro
 * (T6020) reports 10 here, a full 12-core M2 Pro/Max reports 12 -- as
 * opposed to a chip-id-keyed literal, which cannot distinguish bins of the
 * same chip_id. Only valid after smp_start_secondaries() has run (hv_init()
 * calls it before hv_vgicv3_init(), hv.c:64,119).
 */
int smp_cpu_count(void)
{
    int count = 0;
    for (int cpu = 0; cpu < MAX_CPUS; cpu++) {
        if (cpu_nodes[cpu])
            count++;
    }
    return count;
}

u64 smp_get_release_addr(int cpu, bool from_adt)
{
    if(from_adt){
        u64 *release_addr;
        u32 length;
        char cpu_str[32];
        snprintf(cpu_str, sizeof(cpu_str), "/cpus/cpu%d", cpu);
        int node = adt_path_offset(adt, cpu_str);
        release_addr = (u64*)adt_getprop(adt, node, "reg-private", &length);
        return *release_addr;
    }

    struct spin_table *target = &spin_table[cpu];

    if (cpu >= MAX_CPUS)
        return 0;

    target->args[0] = 0;
    target->args[1] = 0;
    target->args[2] = 0;
    target->args[3] = 0;
    return (u64)&target->target;
}
