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

// Exercise every discoverable T8142 secondary except the separately quarantined
// CPU 0 below. Keeping an artificial count cap hides valid ADT topology from the
// hypervisor and PSCI implementation and prevents full-M5 validation.

//
// T8142: secondaries idle with architectural WFE rather than deep_wfi().
//
// deep_wfi() calls Apple's _deep_wfi_helper() retention sequence whenever
// AIDR_EL1 reports architectural retention. That path is unverified on this SoC
// -- features_m4 carries `sleep_mode = SLEEP_NONE` with the comment
// "XXX probably new mode required", and neither cpufreq nor MCC are implemented
// here, so nothing has configured the states it relies on.
//
// WFE is architectural, cannot enter Apple-specific retention, and lets the
// boot CPU wake a parked core with SEV. Plain WFI avoided retention too, but it
// required an unverified M4 fast-IPI write before P_SMP_CALL could dispatch any
// work. That left the proxy waiting forever despite a live secondary.
//
// Ruled out before this: the fast-IPI Apple IMPDEF writes
// (SYS_IMP_APL_IPI_RR_GLOBAL_EL1 / SYS_IMP_APL_IPI_SR_EL1). Skipping both of
// them entirely did not stop the reset, so they are not the cause and are left
// enabled -- IPIs are needed to wake a core out of WFI.
//
#define T8142_EVENT_IDLE (chip_id == T8142)

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
// CPU 0 used to be quarantined after its 100 ms startup handshake timed out.
// That exposed a more fundamental bug: reset stack and CPU identity were global
// hand-off variables.  Continuing to CPU 1 overwrote both while a late CPU 0 was
// still leaving reset, so the late core could run on CPU 1's stack and publish to
// CPU 1's spin table.  T8142 now derives the dense CPU index from MPIDR at the
// reset vector and uses the matching per-CPU stack.  A cold cluster-primary gets
// a longer observation window, but can no longer become a misidentified rogue.
#define T8142_CPU0_START_TIMEOUT_MS 2000
#define T8142_MISC_CORES_REG_INDEX  41

//
// T8142: drive the secondary release through ApplePMGR::MISC_CORES in addition
// to the PMGR start bank.
//
// DISABLED, and neither written nor read while disabled. Evidence from J813:
//
//   - The bring-up this file descends from
//     (OtherResources/Windows On Silicon/m1n1_windows.git/src/smp.c) starts
//     T8142 secondaries with the PMGR bank *alone*, has no MISC_CORES step at
//     all, and is recorded there as confirmed working on J704.
//   - Every J813 run carrying this step failed identically:
//     smp_start_fail_mask == 0x3bf with smp_reset_entered_mask == 0, i.e. not
//     one core ever reached the reset vector -- including cpu1/cpu2, which the
//     chainloader had supposedly re-pointed.
//   - Removing this step is what fixed it: the very next run reached
//     smp_started_mask == 0x3be with reset_entered == 0x3be, i.e. all nine
//     secondaries including every cluster-1 core.
//
// The window itself is fine, and an earlier draft of this comment was wrong to
// call it non-responding. A guarded probe (GUARD_MARK) of both this bank and
// the PMGR start bank fails to fault and simply reads zero, so these are
// write-only/auto-clearing registers rather than dead address space. The defect
// is therefore in what the sequence *does* -- writing BIT(0) here as a "disable"
// edge holds the armed cores in reset -- not in where it writes.
//
// The reg index itself is not obviously wrong -- /arm-io/pmgr on J813 really
// does have 54 reg tuples and reg[41] really is 0x3803c0000 -- so this is kept
// intact behind a switch rather than deleted. Re-enable only once the window is
// shown to respond.
//
#define T8142_MISC_CORES_RELEASE 0

//
// T8142: kick cpu0 with SEV + a targeted fast IPI while waiting for it to start.
//
// DISABLED. This was added on the theory that cpu0 was already awake and parked,
// and needed an architectural wake rather than a reset. A cold-boot measurement
// disproves the premise it rested on:
//
//   Straight after a hand cold boot, before anything had been chainloaded and
//   before smp_start_secondaries() had ever run, every stopped core -- cpu0
//   included -- read cpu_impl+0x100 = 0x0000d800, differing only in the core-id
//   nibbles at +0x104. cpu0 is born in exactly the same state as cpu1..cpu9.
//
// The 0x0f845102 "live/parked" status that cpu0 shows after a failed start is
// therefore something we do to it, not how we find it. cpu0 is also the only
// core this signalling is applied to, and smp_wait_cpu() fires the first kick at
// i == 0 -- immediately, before the core has had any time to leave reset. Firing
// an IMPDEF cross-core IPI at a core that is mid-reset is a far better candidate
// for wedging it than for waking it.
//
// Apple's own code supports leaving this off: ApplePMGR::enableCPUCore ->
// enableCPUCores -> configMiscCores contains no cpu0 special case whatsoever,
// and releases core 0 with the same two register writes as every other core.
//
#define T8142_SIGNAL_PARKED_CPU0 0

//
// T8142: release cpu0 after its cluster siblings rather than before them.
//
// cpu0 is the lowest ADT index, so the start loop releases it first, while every
// other core in cluster 0 is still in reset. That ordering is the only remaining
// property unique to cpu0 once the fast-IPI kick is ruled out
// (T8142_SIGNAL_PARKED_CPU0), and the hardware does distinguish cpu0's outcome:
//
//   cpu_impl+0x104 bits [15:8] read 0x40 cold, 0x80 once a core is running, and
//   0x10 for cpu0 after a failed start -- a third state, not a failure to
//   respond. cpu0 does leave its cold state, it just does not land in the
//   running one.
//
// TESTED, MADE NO DIFFERENCE, so left off. Releasing cpu0 last on an otherwise
// clean cold boot still yields started == 0x3be and fail == 0x1, bit-for-bit the
// same as releasing it first. Ordering is not the cause. Kept behind the switch
// only so the next person does not have to re-run the experiment.
//
// NOTE when re-testing any of this: a RAM chainload does NOT reset cores that a
// previous run already started. Chainloading on top of a successful run leaves
// every secondary already released, so the next start request is a no-op for all
// of them and the result reads as a total failure (0x3bf, reset_entered 0). Cold
// boot or reboot between SMP tests or the measurement is meaningless.
//
#define T8142_START_CPU0_LAST 0

//
// Two further cpu0 theories eliminated, both from a live Windows run rather
// than a dedicated experiment, so neither cost a boot.
//
//   Cluster power. Ruled out by its siblings: cpu1..cpu5 share cluster 0 with
//   cpu0 and all five start and run Windows. The cluster is demonstrably
//   powered and clocked while cpu0 fails inside it.
//
//   The (die, cluster, core) decode for index 0. Ruled out by the reset
//   vectors m1n1 reserves, which are derived from that decode: all ten appear
//   and all ten are correctly strided -- cluster 0 at 0x210050000 + n*0x100000
//   for n = 0..5, cluster 1 at 0x211050000 + n*0x100000 for n = 0..3. cpu0's
//   entry at 0x210050000 is exactly where the pattern puts it, so the decode
//   produces the right address for index 0 and the entry point is written to
//   the right register.
//
// RESOLVED: cpu0 is not broken. The RAM chainload breaks it.
//
// Measured on J813 with the proxy, all within single boots:
//
//   - Released from cold by raw register writes, cpu0 starts perfectly:
//     +0x104 bits[15:8] go 0x40 -> 0x80 and +0x100 goes 0x0000d800 ->
//     0x0f205102, the exact running-core signature, stable indefinitely. Done
//     both after its cluster siblings and as the very first core released into
//     a completely cold cluster. The hardware start path for cpu0 is fine.
//
//   - cpu0 reads cold (0x40) in the resident image immediately before a
//     chainload, and already reads the stuck third state (0x10, +0x100 =
//     0x0f845102) at the *first instruction of _start* in the chainloaded
//     image -- with the PMGR start bank still untouched at 0x3bf and cpu1
//     still cold. A probe at every init step from image entry through
//     sep_init() shows the value never changes: this image never touches cpu0.
//
//   - Once in 0x10 the core is unrecoverable. No combination of the stop
//     register, the W1C release bit and the per-cluster trigger returns it to
//     cold or to running, which is why every previous attempt to "start cpu0
//     harder" failed: by the time smp_start_secondaries() runs, cpu0 is
//     already dead and the release is a no-op.
//
// So the damage happens inside the chainload handover -- the resident image's
// shutdown path and the copy stub -- before this image executes anything. That
// also disposes of the remaining theories: ordering (re-tested with the
// race-free breadcrumb below; cpu0 still fails with reset_entered[0] == 0),
// the fast-IPI kick, cluster power, and the address decode.
//
// Consequence for testing: a directly booted (installed) image has no handover
// window, so cpu0 should be cold when smp_start_secondaries() runs -- the state
// proven above to start. Confirming that means replacing the resident image,
// which is deliberately not done here. Until then every chainloaded run,
// including every Windows boot, will see nine cores and must not be read as
// evidence about cpu0.
//
// This matters more than it used to. Windows requests cpu0 first, and answering
// a CPU_ON failure truthfully makes it issue PSCI SYSTEM_RESET and end the boot
// -- measured. cpu0 is therefore a hard prerequisite for handing this machine
// ten cores, not an item that can be finished later; the MADT currently hides
// it to let the other nine run.
//

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

// Consumed by start.S before a secondary has a C stack.  This is intentionally
// enabled only after the T8142 boot CPU has allocated per-CPU reset stacks.
volatile u32 smp_use_per_cpu_reset_stacks;

// Race-free reset breadcrumb, one byte per dense ADT CPU index, stored by the
// core itself from start.S before it has a stack.
//
// The _mask breadcrumbs below are read-modify-write from every core at once and
// silently lose updates, which is why they have disagreed with reality: a run
// that started nine cores recorded reset_entered 0x39a while started_mask was
// 0x3be.  A one-byte store per core cannot race, so this is the only breadcrumb
// safe to reason from.  Written with the MMU off, so invalidate before reading.
volatile u8 smp_reset_entered[MAX_CPUS];

// Read-only hardware breadcrumbs.  Each bit is the dense ADT CPU index.
volatile u64 smp_reset_entered_mask;
volatile u64 smp_c_entry_mask;
volatile u64 smp_init_complete_mask;
volatile u64 smp_spin_entry_mask;

/*
 * Times each parked secondary came back from its WFE with the register file
 * cleared.  Reported by smp_report_wfe_reg_loss(); see smp_secondary_entry().
 */
volatile u64 smp_wfe_reg_loss[MAX_CPUS];

static bool wfe_mode = false;

static int target_cpu;
static int cpu_nodes[MAX_CPUS];
static struct spin_table spin_table[MAX_CPUS];
static u64 pmgr_reg;
static u64 cpu_start_off;
static u64 t8142_misc_cores_reg;
static bool t8142_misc_cores_reg_known;

// T8142 shares the framebuffer/USB console between the boot CPU and newly
// released secondaries.  Printing from the startup loop can leave the proxy
// request waiting forever even though every secondary reached its spin table.
// Keep machine-readable results instead; host-side probes can resolve these
// symbols from the ELF and inspect them without touching the console.
volatile u64 smp_started_mask;
volatile u64 smp_start_fail_mask;
// Post-mortem for a core that never completed the handshake: the relay word we
// actually left at the locked RVBAR, and each core's impl status after its wait
// window ([0] = +0x100, [1] = +0x104; +0x104 bits[15:8] read 0x40 cold and 0x80
// running on T8142).
volatile u64 smp_t8142_relay_addr;
volatile u32 smp_t8142_relay_word;
volatile u32 smp_t8142_impl_status[MAX_CPUS][2];
// Where each core's cpu-impl-reg window lives, and the RVBAR word read out of
// it before this image touched anything.  Kept because the host cannot obtain
// either one: reading a stopped core's impl window over the proxy faults, and
// the reply arrives as console text rather than data.
volatile u64 smp_t8142_impl_addr[MAX_CPUS];
volatile u64 smp_t8142_rvbar_pre[MAX_CPUS];
// Impl status as this image first finds it, before it has released anything.
// This is the measurement that separates "the candidate wedges cpu0" from
// "cpu0 was already wedged when the candidate got here".
volatile u32 smp_t8142_status_pre[MAX_CPUS][2];
volatile u64 smp_cpu0_wake_attempts;
volatile u64 smp_cpu_release_base;
volatile u32 smp_t8142_misc_before[4];
volatile u32 smp_t8142_misc_after_disable[4];
volatile u32 smp_t8142_misc_after_enable[4];

static void t8142_capture_misc_cores(volatile u32 values[4])
{
    if (!t8142_misc_cores_reg_known)
        return;

    for (int i = 0; i < 4; i++)
        values[i] = read32(t8142_misc_cores_reg + 4 * i);

    sysop("dmb sy");
}

static int t8142_cpu_index_from_mpidr(u64 mpidr)
{
    u32 core = FIELD_GET(CPU_REG_CORE, mpidr);
    u32 cluster = FIELD_GET(CPU_REG_CLUSTER, mpidr);

    if (cluster == 0 && core < 6)
        return core;
    if (cluster == 1 && core < 4)
        return 6 + core;

    return -1;
}

int smp_secondary_prepare(void)
{
    int index = target_cpu;

    if (chip_id == T8142)
        index = t8142_cpu_index_from_mpidr(mrs(MPIDR_EL1));

    if (index < 0 || index >= MAX_CPUS)
        return -1;

    if (in_el3())
        msr(TPIDR_EL3, index);
    else if (in_el2())
        msr(TPIDR_EL2, index);
    else
        msr(TPIDR_EL1, index);

    smp_c_entry_mask |= BIT(index);
    sysop("dmb sy");
    return index;
}

void smp_secondary_mark_init_complete(int cpu)
{
    if (cpu < 0 || cpu >= MAX_CPUS)
        return;

    smp_init_complete_mask |= BIT(cpu);
    sysop("dmb sy");
}
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

// A RAM-chainloaded m1n1 can grow or shrink enough to move _vectors_start while
// T8142 keeps each secondary RVBAR locked to the resident image's old vector
// address. In that case the core wakes into the resident image and updates the
// wrong spin table. Install a single-instruction, RAM-only relay at the locked
// vector address so reset enters this candidate's vector table. A reboot reloads
// the resident image, so this never modifies the installed m1n1 artifact.
static bool t8142_prepare_locked_rvbar_relay(u64 locked_rvbar)
{
    if (chip_id != T8142 || !locked_rvbar)
        return false;

    s64 delta = (s64)(u64)_vectors_start - (s64)locked_rvbar;

    // AArch64 B uses a signed imm26 scaled by four: +/-128 MiB.
    if ((delta & 3) || delta < -(1LL << 27) || delta >= (1LL << 27)) {
        printf("Failed!\n    Locked RVBAR relay target is out of range "
               "(RVBAR=0x%lx target=0x%lx)\n",
               locked_rvbar, (u64)_vectors_start);
        return false;
    }

    u32 branch = 0x14000000 | (((u64)(delta >> 2)) & 0x03ffffff);
    u32 *relay = (u32 *)locked_rvbar;

    if (*relay != branch) {
        // m1n1 remaps its text through _rodata_end RX after MMU startup. The
        // resident vector page falls inside that range even after a different
        // RAM image has overwritten the surrounding allocation. Make exactly
        // one 16 KiB page writable for the patch, then restore the normal RX
        // permission before any secondary can fetch from it.
        bool remapped = mmu_active();
        if (remapped)
            mmu_add_mapping(locked_rvbar, locked_rvbar, 0x4000, MAIR_IDX_NORMAL, PERM_RWX);

        write32(locked_rvbar, branch);
        //
        // Clean to the Point of Coherency, not just the Point of Unification.
        //
        // The core that consumes this relay is coming out of reset with its
        // MMU and caches OFF, so its instruction fetch is Normal Non-cacheable
        // and resolves at the PoC (DRAM).  DC CVAU only guarantees the line has
        // reached the PoU, which on this SoC is the *writing* core's cluster
        // L2.  m1n1 boots on cpu6, a cluster-1 P-core, and cluster 0 does not
        // share that L2, so a PoU-only clean can leave a released cluster-0
        // core fetching the stale pre-relay instruction -- which on T8142 is
        // the resident image's "mov x9, '0'" falling through into the resident
        // m1n1's cpu_reset.  That core then runs, and updates the *resident*
        // spin table, so this image's smp_wait_cpu() times out on a CPU that is
        // demonstrably alive.  See the cpu0 note above.
        //
        dc_cvac_range(relay, sizeof(*relay));
        ic_ivau_range(relay, sizeof(*relay));
        sysop("dsb sy");
        sysop("isb");

        if (remapped)
            mmu_add_mapping(locked_rvbar, locked_rvbar, 0x4000, MAIR_IDX_NORMAL, PERM_RX_EL0);
    }

    smp_t8142_relay_addr = locked_rvbar;
    smp_t8142_relay_word = *relay;

    if (*relay != branch) {
        printf("Failed!\n    Locked RVBAR relay write did not stick at 0x%lx\n", locked_rvbar);
        return false;
    }

    return true;
}

void smp_secondary_entry(void)
{
    int index = chip_id == T8142 ? smp_id() : target_cpu;

    if (index < 0 || index >= MAX_CPUS)
        while (1)
            sysop("wfe");

    struct spin_table *me = &spin_table[index];

    if (chip_id != T8142) {
        if (in_el2())
            msr(TPIDR_EL2, index);
        else
            msr(TPIDR_EL1, index);
    }

    smp_spin_entry_mask |= BIT(index);
    sysop("dmb sy");

    // See T8142_QUIET_SECONDARY.
    if (!T8142_QUIET_SECONDARY)
        printf("  Index: %d (table: %p)\n\n", index, me);

    me->mpidr = mrs(MPIDR_EL1) & 0xFFFFFF;

    sysop("dmb sy");
    me->flag = 1;
    sysop("dmb sy");
    u64 target;
    if (!cpu_features->fast_ipi)
        aic_write(AIC_IPI_MASK_SET, AIC_IPI_SELF); // we only use the "other" IPI

    while (1) {
        while (!(target = me->target)) {
            if (wfe_mode || T8142_EVENT_IDLE) {
                /*
                 * `me` and `index` live in callee-saved registers across this
                 * wait, and a parked secondary sits here for minutes -- from
                 * smp_start_secondaries() until the guest's first CPU_ON.  On
                 * T8142 a wait can clear the register file (that is what
                 * destroys the guest's x26 across HalProcessorIdle; see the
                 * HCR_EL2.TWI trap in hv_exc.c), and a core that resumes with
                 * `me` cleared never increments its flag again.
                 *
                 * cpu_wfe_stateless() both closes that hole and measures it.
                 * So far it has measured zero: WFE has not been observed losing
                 * the file on this SoC, so this is a guard rather than a known
                 * bug, and it is *not* the explanation for the intermittent
                 * hv_start_secondary() hang -- that still reproduces with this
                 * in place.  See the retry loop in smp_call4().
                 */
                smp_wfe_reg_loss[index] += cpu_wfe_stateless();
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
    if (chip_id != T8142)
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

static bool smp_prepare_cpu(int index, int die, int cluster, int core, u64 impl)
{
    if (index >= MAX_CPUS)
        return false;

    if (has_el3() && index >= MAX_EL3_CPUS)
        return false;

    if (spin_table[index].flag)
        return false;

    u64 rvbar = read64(impl);
    if ((rvbar & RVBAR_LOCK) && (rvbar & RVBAR_ADDR) != (u64)_vectors_start) {
        u64 locked_rvbar = rvbar & RVBAR_ADDR;
        if (!t8142_prepare_locked_rvbar_relay(locked_rvbar)) {
            smp_start_fail_mask |= BIT(index);
            if (chip_id != T8142)
                printf("Failed! \n    RVBAR (=0x%lx) is locked and differs from entry point (=0x%lx)\n",
                       locked_rvbar, (u64)_vectors_start);
            return false;
        }

        if (chip_id != T8142)
            printf("RVBAR relay 0x%lx -> 0x%lx; ", locked_rvbar, (u64)_vectors_start);
    }

    if (chip_id != T8142)
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

    dc_civac_range(&secondary_stacks[index], sizeof(secondary_stacks[index]));
    if (has_el3())
        dc_civac_range(&secondary_stacks_el3[index], sizeof(secondary_stacks_el3[index]));

    dc_civac_range(&_reset_stack, sizeof(void *));

    sysop("dsb sy");

    if (!(read64(impl) & RVBAR_LOCK)) {
        // This also clears RVBAR_LOCK, so that HV can set RVBAR later when the core is running
        write64(impl, (u64)_vectors_start);
    }

    return true;
}

// CPU 0 is not in the same cold-off state as the other T8142 secondaries.
// AppleARMCPU::startCPU() first releases a core through ApplePMGR, and the
// non-boot init path then calls ml_cpu_signal().  On J813 the PMGR release is
// sufficient for cpu1..5 and cpu7..9, while cpu0 keeps reporting a live/parked
// status and never refetches RVBAR.  Mirror the missing architectural wake here
// after the reset vector and per-CPU stack are ready.
//
// MPIDR 0 is cpu0 on T8142.  A zero fast-IPI route value is therefore a valid
// target, not an empty mask.  SEV covers a core parked in WFE; the targeted IPI
// covers Apple's interrupt-based park path.  This remains RAM-chainload-only
// until the hardware breadcrumb proves cpu0 reached spin_table[0].
static void t8142_signal_parked_cpu0(void)
{
    if (chip_id != T8142)
        return;

    sysop("dsb sy");
    sysop("sev");

    if (cpu_features->fast_ipi)
        msr(SYS_IMP_APL_IPI_RR_GLOBAL_EL1, 0);
    else
        aic_write(AIC_IPI_SEND, AIC_IPI_SEND_CPU(0));

    sysop("isb");
    sysop("sev");
    smp_cpu0_wake_attempts++;
    sysop("dmb sy");
}

static void smp_wait_cpu(int index)
{
    int i;

    int timeout_ms = chip_id == T8142 && index == 0 ? T8142_CPU0_START_TIMEOUT_MS : 100;
    for (i = 0; i < timeout_ms; i++) {
        if (T8142_SIGNAL_PARKED_CPU0 && chip_id == T8142 && index == 0 &&
            (i == 0 || i == 10 || i == 100 || i == 500))
            t8142_signal_parked_cpu0();

        sysop("dmb ld");
        if (spin_table[index].flag)
            break;
        udelay(1000);
    }

    if (i >= timeout_ms) {
        smp_start_fail_mask |= BIT(index);
        if (chip_id != T8142)
            printf("Failed!\n");
    } else {
        smp_started_mask |= BIT(index);
        if (chip_id != T8142)
            printf("  Started.\n");
    }
}

static void smp_finish_cpu_start(void)
{
    _reset_stack = dummy_stack + DUMMY_STACK_SIZE;
    _reset_stack_el1 = dummy_stack_el1 + DUMMY_STACK_SIZE;
}

static bool smp_start_cpu(int index, int die, int cluster, int core, u64 impl,
                          u64 cpu_start_base)
{
    if (!smp_prepare_cpu(index, die, cluster, core, impl))
        return false;

    cpu_start_base += die * PMGR_DIE_OFFSET;

    // Some kind of system level startup/status bit
    // Without this, IRQs don't work
    write32(cpu_start_base + 0x4, cpu_start_bit(index, cluster, core));

    // Actually start the core
    write32(cpu_start_base + 0x8 + 4 * cluster, 1 << core);

    smp_wait_cpu(index);

    // Post-mortem: whether the core is cold (0x40), running (0x80) or somewhere
    // else, plus its own race-free reset breadcrumb.  Written with the MMU off
    // by the core itself, so invalidate before believing what we read.
    smp_t8142_impl_status[index][0] = read32(impl + 0x100);
    smp_t8142_impl_status[index][1] = read32(impl + 0x104);
    dc_ivac_range((void *)smp_reset_entered, sizeof(smp_reset_entered));

    smp_finish_cpu_start();
    return true;
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
    smp_started_mask = 0;
    smp_start_fail_mask = 0;
    smp_reset_entered_mask = 0;
    smp_c_entry_mask = 0;
    smp_init_complete_mask = 0;
    smp_spin_entry_mask = 0;
    smp_cpu0_wake_attempts = 0;
    smp_cpu_release_base = 0;
    memset((void *)smp_t8142_misc_before, 0, sizeof(smp_t8142_misc_before));
    memset((void *)smp_t8142_misc_after_disable, 0,
           sizeof(smp_t8142_misc_after_disable));
    memset((void *)smp_t8142_misc_after_enable, 0,
           sizeof(smp_t8142_misc_after_enable));
    smp_use_per_cpu_reset_stacks = chip_id == T8142;
    dc_civac_range((void *)&smp_use_per_cpu_reset_stacks,
                   sizeof(smp_use_per_cpu_reset_stacks));

    memset((void *)smp_reset_entered, 0, sizeof(smp_reset_entered));
    dc_civac_range((void *)smp_reset_entered, sizeof(smp_reset_entered));

    //
    // Publish this whole image to the Point of Coherency before releasing any
    // core.
    //
    // A released core runs with MMU and caches off until it reaches its own
    // init, so every instruction it fetches and every hand-off variable it
    // reads resolves at the PoC.  A RAM-chainloaded image never gets there on
    // its own: proxyclient/tools/chainload.py copies it with a "dc cvau" loop,
    // which only reaches the Point of Unification -- the boot CPU's cluster L2.
    // m1n1 boots on cpu6 in cluster 1, so nothing in cluster 0 shares that L2.
    //
    // The cores released late were only ever working by accident: cpu0 is
    // released first and given a 2000 ms handshake window, and that delay is
    // long enough for the dirty lines to be evicted to DRAM naturally before
    // any other core is released.  Publishing explicitly removes the race
    // instead of depending on cpu0's timeout to launder it.
    //
    // Text and rodata only, not _end: the tail of BSS is not backed by a live
    // mapping and cleaning it faults (measured -- a dc cvac at _base + 0x154000
    // took a level-3 translation fault and rebooted the machine).  Instruction
    // fetch is what has to be visible; the individual hand-off variables below
    // are already published with dc_civac_range at their own sites.
    dc_cvac_range((void *)_base, (size_t)(_rodata_end - _base));
    sysop("dsb sy");

    if (chip_id != T8142)
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

    t8142_misc_cores_reg = 0;
    t8142_misc_cores_reg_known = false;
    //
    // Leaving t8142_misc_cores_reg_known false is what disables the MISC_CORES
    // path: t8142_capture_misc_cores() early-returns on it and the release block
    // at the end of this function is gated on it, so nothing reads or writes
    // that window. See T8142_MISC_CORES_RELEASE.
    //
    if (T8142_MISC_CORES_RELEASE && chip_id == T8142) {
        /*
         * AppleT8142PMGR::initRegMaps() maps ApplePMGR::MISC_CORES
         * (RegMap 8) to the 42nd /arm-io/pmgr register tuple.  Do not derive
         * this from reg[0]: on J813 the block is reg[41] at 0x3803c0000,
         * while reg[0] + the legacy 0x34000 offset points at a different
         * status block.
         */
        if (adt_get_reg(adt, pmgr_path, "reg", T8142_MISC_CORES_REG_INDEX,
                        &t8142_misc_cores_reg, NULL) < 0) {
            printf("Error getting T8142 MISC_CORES register\n");
        } else {
            t8142_misc_cores_reg_known = true;
        }
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
        case T6040:
        case T6041:
        case T6050:
        case T6051:
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

    if (chip_id == T8142) {
        // The historical hardware-proven build released cpu1..5 and cpu7..9
        // through the ordinary PMGR start bank.  MISC_CORES is useful as a
        // read-only breadcrumb, but replacing PMGR release writes with it held
        // every secondary in reset on J813.
        smp_cpu_release_base = pmgr_reg + cpu_start_off;
        t8142_capture_misc_cores(smp_t8142_misc_before);
    }

    u32 t8142_global_mask = 0;
    u32 t8142_cluster_masks[2] = {0};
    u64 t8142_prepared_mask = 0;

    for (int n = 0; n < MAX_CPUS; n++) {
        // See T8142_START_CPU0_LAST: visit 1,2,..,MAX_CPUS-1,0 so cpu0 is
        // released after its cluster siblings instead of before them.
        int i = (T8142_START_CPU0_LAST && chip_id == T8142) ? (n + 1) % MAX_CPUS : n;
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

        if (chip_id == T8142) {
            smp_t8142_impl_addr[i] = cpu_impl_reg[0];
            smp_t8142_rvbar_pre[i] = read64(cpu_impl_reg[0]);
            smp_t8142_status_pre[i][0] = read32(cpu_impl_reg[0] + 0x100);
            smp_t8142_status_pre[i][1] = read32(cpu_impl_reg[0] + 0x104);
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

        u8 core = FIELD_GET(CPU_REG_CORE, reg);
        u8 cluster = FIELD_GET(CPU_REG_CLUSTER, reg);
        u8 die = FIELD_GET(CPU_REG_DIE, reg);

        bool prepared =
            smp_start_cpu(i, die, cluster, core, cpu_impl_reg[0], pmgr_reg + cpu_start_off);
        if (chip_id == T8142 && prepared) {
            t8142_prepared_mask |= BIT(i);
            t8142_global_mask |= cpu_start_bit(i, cluster, core);
            if (cluster < ARRAY_SIZE(t8142_cluster_masks))
                t8142_cluster_masks[cluster] |= BIT(core);
        }
    }

    if (chip_id == T8142 && t8142_prepared_mask && t8142_misc_cores_reg_known) {
        // J813 needs both edges, in this order.  PMGR above arms each reset
        // vector and per-cluster start request; publishing the complete desired
        // state through MISC_CORES then lets the armed cores leave reset.
        sysop("dsb sy");
        write32(t8142_misc_cores_reg, BIT(0));
        sysop("dsb sy");
        udelay(100);
        t8142_capture_misc_cores(smp_t8142_misc_after_disable);

        write32(t8142_misc_cores_reg + 0x4, t8142_global_mask);
        write32(t8142_misc_cores_reg + 0x8, t8142_cluster_masks[0]);
        write32(t8142_misc_cores_reg + 0xc, t8142_cluster_masks[1]);
        sysop("dsb sy");
        udelay(100);
        t8142_capture_misc_cores(smp_t8142_misc_after_enable);

        for (int i = 0; i < MAX_CPUS; i++) {
            if (t8142_prepared_mask & BIT(i))
                smp_wait_cpu(i);
        }
        smp_finish_cpu_start();
    }

    if (chip_id == T8142) {
        // One line per core, printed once, after every start attempt has
        // settled.  T8142_QUIET_SECONDARY silences the per-core progress
        // messages because secondaries sharing the console wedged the machine,
        // so without this the boot CPU reports nothing at all about who came
        // up -- and the masks it does export lose concurrent updates
        // (smp_reset_entered[] is the byte array that does not).
        //
        // rvbar is the raw cpu-impl-reg word: bit 0 is the lock, bits 47:12 the
        // vector address.  A core still locked to the *resident* image's vector
        // after a RAM chainload is the case the relay exists to cover.
        dc_ivac_range((void *)smp_reset_entered, sizeof(smp_reset_entered));
        printf("T8142 SMP: prepared 0x%lx started 0x%lx failed 0x%lx (vectors 0x%lx, relay "
               "0x%lx=0x%08x)\n",
               t8142_prepared_mask, smp_started_mask, smp_start_fail_mask, (u64)_vectors_start,
               smp_t8142_relay_addr, smp_t8142_relay_word);

        for (int i = 0; i < MAX_CPUS; i++) {
            if (!smp_t8142_impl_addr[i])
                continue;

            u64 rvbar_now = read64(smp_t8142_impl_addr[i]);

            printf("T8142 SMP:  cpu%d impl 0x%lx rvbar %s0x%lx -> %s0x%lx status "
                   "0x%08x/0x%08x -> 0x%08x/0x%08x entered %d%s\n",
                   i, smp_t8142_impl_addr[i], (smp_t8142_rvbar_pre[i] & RVBAR_LOCK) ? "L" : "u",
                   smp_t8142_rvbar_pre[i] & RVBAR_ADDR, (rvbar_now & RVBAR_LOCK) ? "L" : "u",
                   rvbar_now & RVBAR_ADDR, smp_t8142_status_pre[i][0], smp_t8142_status_pre[i][1],
                   read32(smp_t8142_impl_addr[i] + 0x100),
                   read32(smp_t8142_impl_addr[i] + 0x104), smp_reset_entered[i],
                   i == boot_cpu_idx ? " (boot)" : "");
        }
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

    /*
     * Keep signalling while we wait, rather than trusting the single wakeup
     * above.  A secondary parked here has been in WFE since
     * smp_start_secondaries(), which on a Windows boot is minutes -- the guest
     * does not ask for its first CPU_ON until the NT kernel starts its APs --
     * and one event that fails to land leaves this loop spinning forever with
     * the target still asleep.  Measured on J813: 5 of the 11 boots that
     * reached the guest's first CPU_ON hung exactly here, in hv_start_secondary
     * on cpu0, with no other symptom.  Re-arming costs one instruction per
     * iteration of a loop that is already spinning.
     */
    while (target->flag == flag) {
        if (wfe_mode)
            sysop("sev");
        sysop("dmb sy");
    }
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

void smp_report_wfe_reg_loss(void)
{
    static u64 last_total;
    u64 total = 0;

    for (int cpu = 0; cpu < MAX_CPUS; cpu++)
        total += smp_wfe_reg_loss[cpu];

    if (total == last_total)
        return;
    last_total = total;

    printf("SMP: parked-WFE register file lost:");
    for (int cpu = 0; cpu < MAX_CPUS; cpu++)
        if (smp_wfe_reg_loss[cpu])
            printf(" cpu%d=%ld", cpu, smp_wfe_reg_loss[cpu]);
    printf("\n");
}

void smp_set_wfe_mode(bool new_mode)
{
    wfe_mode = new_mode;
    sysop("dsb sy");

    // T8142 secondaries are already parked in WFE, so SEV below is sufficient
    // and deliberately avoids the still-unverified M4 fast-IPI registers.
    if (chip_id != T8142) {
        for (int cpu = 0; cpu < MAX_CPUS; cpu++)
            if (cpu != boot_cpu_idx && smp_is_alive(cpu))
                smp_send_ipi(cpu);
    }

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
