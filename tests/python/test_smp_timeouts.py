"""Inject dispatch/completion failures into production SMP and its callers."""

from pathlib import Path
import subprocess


REPO = Path(__file__).resolve().parents[2]


def compile_and_run(tmp_path, name, source):
    binary = tmp_path / name
    subprocess.run(
        ["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
         "-Wno-unused-parameter", "-Wno-unused-function", "-x", "c", "-", "-o", str(binary)],
        input=source, text=True, check=True,
    )
    subprocess.run([str(binary)], check=True)


def test_timeout_status_and_late_completion(tmp_path):
    source = (REPO / "src/smp.c").read_text()
    table = source[source.index("struct spin_table {"):source.index("void *_reset_stack")]
    functions = source[source.index("int smp_call4("):source.index("void smp_set_wfe_mode(")]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint64_t u64;
typedef uint32_t u32;
#define MAX_CPUS 4
#define SMP_TIMEOUT -2
#define sysop(op) ((void)0)
''' + table + r'''
static struct spin_table spin_table[MAX_CPUS];
static int boot_cpu_idx;
static bool wfe_mode = true;
static u64 ticks;
static bool complete;
static u64 callback_result;
static void smp_send_ipi(int cpu) {}
static u64 get_ticks(void)
{
    ticks += 1000;
    if (complete && spin_table[1].target) {
        spin_table[1].flag++;
        spin_table[1].retval = callback_result;
        spin_table[1].target = 0;
    }
    return ticks;
}
static u64 ticks_to_msecs(u64 value) { return value; }
static void callback(void) {}
''' + functions + r'''
int main(void)
{
    u64 result = 0xfeed;
    assert(smp_call4(-1, callback, 0, 0, 0, 0) == -1);
    assert(smp_call4(MAX_CPUS, callback, 0, 0, 0, 0) == -1);
    assert(smp_call4(boot_cpu_idx, callback, 0, 0, 0, 0) == -1);
    assert(smp_call4(1, callback, 0, 0, 0, 0) == -1); /* offline */
    assert(smp_wait(1, &result) == -1 && result == 0xfeed);

    spin_table[1].flag = 1;
    assert(smp_call4(1, NULL, 0, 0, 0, 0) == -1);
    assert(smp_call4(1, callback, 11, 22, 33, 44) == SMP_TIMEOUT);
    assert(spin_table[1].target == (u64)callback);
    struct spin_table pending = spin_table[1];
    assert(smp_call4(1, callback, 55, 66, 77, 88) == -1);
    assert(!memcmp(&pending, &spin_table[1], sizeof(pending)));
    assert(smp_wait(1, &result) == SMP_TIMEOUT && result == 0xfeed);
    assert(smp_wait(1, NULL) == SMP_TIMEOUT);
    assert(!memcmp(&pending, &spin_table[1], sizeof(pending)));

    /* Completion after timeout remains observable; pending arguments survive. */
    complete = true;
    callback_result = 73;
    assert(smp_wait(1, &result) == 0 && result == 73);
    assert(spin_table[1].target == 0);

    /* A zero callback value is a successful completion, not an error sentinel. */
    callback_result = 0;
    assert(smp_call4(1, callback, 1, 2, 3, 4) == 0);
    assert(smp_wait(1, &result) == 0 && result == 0);
    assert(smp_wait(1, NULL) == 0);
    return 0;
}
'''
    compile_and_run(tmp_path, "smp-timeouts", harness)


def test_internal_callers_stop_on_failure(tmp_path):
    memory = (REPO / "src/memory.c").read_text()
    mmu = memory[memory.index("int mmu_init_secondary("):memory.index("void mmu_shutdown(")]
    mitigation = (REPO / "src/mitigations.c").read_text()
    mitigation = mitigation[mitigation.index("int mitigations_perform("):]
    hv = (REPO / "src/hv/hv.c").read_text()
    hv = hv[hv.index("int hv_start_secondary("):hv.index("void hv_exit_cpu(")]
    watchdog = (REPO / "src/hv/hv_wdt.c").read_text()
    watchdog = watchdog[watchdog.index("int hv_wdt_start("):]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint64_t u64;
#define MAX_CPUS 4
#define SMP_TIMEOUT -2
#define BIT(n) (1ULL << (n))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define sysop(op) ((void)0)
#define mrs(reg) 1000
#define WDT_TIMEOUT 1
#define smp_call0(cpu, fn) smp_call4(cpu, fn, 0, 0, 0, 0)
static int calls, waits, fail_call, fail_wait, dispatch_error = -1;
static int boot_cpu_idx;
static bool hv_started_cpus[MAX_CPUS];
static u64 hv_cpus_in_guest;
static int hv_secondary_info;
static u64 hv_secondary_regs[MAX_CPUS][4];
static u64 *queued_regs;
static bool hv_wdt_active, hv_wdt_enabled, hv_wdt_join_pending;
static int hv_wdt_cpu;
static u64 hv_wdt_timeout, hv_wdt_breadcrumbs[MAX_CPUS];
struct mitigation { const char *name; bool vulnerable, mitigated; };
static struct mitigation mitigations[] = {{"test", true, false}};
static void apply_mitigations(void) {}
static bool smp_is_alive(int cpu) { return cpu == 1; }
static void mmu_init_secondary_local(void) {}
static void hv_init_secondary(void) {}
static void hv_enter_secondary(void) {}
static void hv_wdt_main(void) {}
static void hv_wdt_pet(void) {}
static void iodev_console_flush(void) {}
static int smp_call4(int cpu, void *fn, u64 a, u64 b, u64 c, u64 d)
{
    if (fn == hv_enter_secondary)
        queued_regs = (u64 *)b;
    return ++calls == fail_call ? dispatch_error : 0;
}
static int smp_wait(int cpu, u64 *result)
{ return ++waits == fail_wait ? SMP_TIMEOUT : 0; }
''' + mmu + mitigation + hv + watchdog + r'''
static void reset(void)
{
    calls = waits = fail_call = fail_wait = 0;
    dispatch_error = -1;
    memset(hv_started_cpus, 0, sizeof(hv_started_cpus));
    hv_cpus_in_guest = 0;
    hv_wdt_active = hv_wdt_enabled = hv_wdt_join_pending = false;
}
int main(void)
{
    reset(); fail_call = 1;
    assert(mmu_init_secondary(1) == -1 && calls == 1 && waits == 0);
    reset(); fail_wait = 1;
    assert(mmu_init_secondary(1) == SMP_TIMEOUT);
    reset();
    assert(mmu_init_secondary(1) == 0);

    reset(); fail_call = 1;
    assert(mitigations_perform() == -1 && waits == 0);
    reset(); fail_wait = 1;
    assert(mitigations_perform() == -1);
    reset();
    assert(mitigations_perform() == 0);

    u64 regs[4] = {11, 22, 33, 44};
    for (int phase = 1; phase <= 2; phase++) {
        reset(); fail_call = phase;
        assert(hv_start_secondary(1, (void *)1, regs) == -1);
        assert(!hv_started_cpus[1] && !hv_cpus_in_guest);
        assert(calls == phase && waits == phase - 1);
        reset(); fail_wait = phase;
        assert(hv_start_secondary(1, (void *)1, regs) == -1);
        assert(!hv_started_cpus[1] && !hv_cpus_in_guest && calls == phase);
    }
    reset();
    assert(hv_start_secondary(1, (void *)1, regs) == 0);
    assert(hv_started_cpus[1] && hv_cpus_in_guest == BIT(1));
    reset(); fail_call = 3; dispatch_error = SMP_TIMEOUT;
    assert(hv_start_secondary(1, (void *)1, regs) == SMP_TIMEOUT);
    assert(hv_started_cpus[1] && hv_cpus_in_guest == BIT(1));
    assert(queued_regs != regs && !memcmp(queued_regs, regs, sizeof(regs)));
    memset(regs, 0, sizeof(regs)); /* The next proxy request reuses its buffer. */
    assert(queued_regs[0] == 11 && queued_regs[3] == 44);
    reset(); fail_call = 3;
    assert(hv_start_secondary(1, (void *)1, regs) == -1);
    assert(!hv_started_cpus[1] && !hv_cpus_in_guest);

    reset(); fail_call = 1;
    assert(hv_wdt_start(1) == -1);
    assert(!hv_wdt_active && !hv_wdt_enabled && !hv_wdt_join_pending);
    assert(hv_wdt_stop() == 0 && waits == 0);

    reset(); fail_call = 1; dispatch_error = SMP_TIMEOUT;
    assert(hv_wdt_start(1) == -1);
    assert(!hv_wdt_active && !hv_wdt_enabled && hv_wdt_join_pending);
    assert(hv_wdt_start(2) == -1 && calls == 1);
    fail_wait = 1;
    assert(hv_wdt_stop() == -1 && hv_wdt_join_pending);
    assert(hv_wdt_stop() == 0 && !hv_wdt_join_pending && waits == 2);

    reset();
    assert(hv_wdt_start(1) == 0 && hv_wdt_active && hv_wdt_join_pending);
    fail_wait = 1;
    assert(hv_wdt_stop() == -1 && hv_wdt_join_pending);
    assert(hv_wdt_stop() == 0 && !hv_wdt_join_pending);
    return 0;
}
'''
    compile_and_run(tmp_path, "smp-callers", harness)


def test_hv_join_failure_retains_state_and_joins_other_cpus(tmp_path):
    hv = (REPO / "src/hv/hv.c").read_text()
    join = hv[hv.index("static int hv_join_cpus("):hv.index("int hv_init(")]
    # Exercise the same exit path that returns the P_HV_START reply to the host.
    start = hv[hv.index("int hv_start("):hv.index("static void hv_init_secondary(")]
    tail = start[start.index("    __atomic_and_fetch("):]
    guard = start[:start.index("    memset(hv_should_exit")]
    guard = guard.replace("int hv_start(", "static int check_start(") + "    return 0;\n}\n"
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
typedef uint64_t u64;
#define MAX_CPUS 4
#define BIT(n) (1ULL << (n))
static bool hv_started_cpus[MAX_CPUS], hv_should_exit[MAX_CPUS];
static bool hv_shutdown_failed;
static u64 hv_cpus_in_guest;
static int boot_cpu_idx, bhl, locked, waits, failed_cpu = -1, watchdog_error;
static void spin_lock(int *lock) { assert(!locked); locked = 1; }
static void spin_unlock(int *lock) { assert(locked); locked = 0; }
static int smp_id(void) { return boot_cpu_idx; }
static void udelay(unsigned delay) {}
static int hv_wdt_stop(void) { return watchdog_error; }
static int smp_wait(int cpu, u64 *result)
{
    assert(!locked);
    for (int i = 0; i < MAX_CPUS; i++)
        assert(hv_should_exit[i]);
    waits |= BIT(cpu);
    if (cpu == failed_cpu)
        return -2;
    hv_cpus_in_guest &= ~BIT(cpu); /* The returning callback clears its bit. */
    return 0;
}
''' + join + guard + '\nstatic int finish_guest(void)\n{\n' + tail + r'''
int main(void)
{
    hv_cpus_in_guest = BIT(0) | BIT(1) | BIT(2);
    hv_started_cpus[0] = hv_started_cpus[1] = hv_started_cpus[2] = true;
    failed_cpu = 1;
    assert(finish_guest() == -1);
    assert(hv_shutdown_failed && !locked);
    assert(waits == (BIT(1) | BIT(2)));
    assert(!hv_started_cpus[0] && hv_started_cpus[1] && !hv_started_cpus[2]);
    assert(hv_cpus_in_guest == BIT(1));
    failed_cpu = -1;
    assert(hv_join_cpus() == 0 && !hv_shutdown_failed && !hv_started_cpus[1]);
    watchdog_error = -1;
    assert(finish_guest() == -1 && hv_shutdown_failed && !locked);
    watchdog_error = 0;
    assert(check_start(NULL, NULL) == -1 && !hv_shutdown_failed);
    assert(check_start(NULL, NULL) == 0);
    return 0;
}
'''
    compile_and_run(tmp_path, "hv-join", harness)


def test_stop_attempts_all_cpus_and_tracks_late_sleep(tmp_path):
    smp = (REPO / "src/smp.c").read_text()
    stop = smp[smp.index("static int smp_stop_cpu("):smp.index("static int smp_init_t8152(")]
    all_cpus = smp[smp.index("int smp_stop_secondaries("):smp.index("void smp_send_ipi(")]
    state = smp[smp.index("static struct {\n    bool pending;"):smp.index("u64 smp_started_mask;")]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint64_t u64;
#define MAX_CPUS 4
#define SMP_TIMEOUT -2
#define T8152 0x8152
#define PMGR_DIE_OFFSET 0x1000
#define sysop(op) ((void)0)
#define smp_call1(cpu, fn, a) smp_call4(cpu, fn, a, 0, 0, 0)
struct spin_table { u64 flag, target; };
static struct spin_table spin_table[MAX_CPUS];
struct cpu_info { bool valid; int die, cluster, core; u64 impl_reg; };
static struct cpu_info cpu_info[MAX_CPUS];
static u64 cpu_start_base;
static int boot_cpu_idx, chip_id, calls[MAX_CPUS], writes, pending_failure = -1;
static int ack_failure = -1, shutdown_failure = -1;
static bool smp_initialized = true;
static void cpu_sleep(bool deep) {}
static void smp_set_wfe_mode(bool mode) {}
static void udelay(unsigned delay) {}
static u64 cpu_start_bit(int index, const struct cpu_info *cpu) { return 1ULL << index; }
static void write32(u64 address, u64 value) { writes++; }
static u64 read64(u64 address)
{
    for (int i = 0; i < MAX_CPUS; i++)
        if (address == cpu_info[i].impl_reg + 0x100)
            return i == shutdown_failure;
    assert(0); return 0;
}
static int smp_wait(int cpu, u64 *result)
{
    if (cpu == pending_failure)
        return SMP_TIMEOUT;
    spin_table[cpu].target = 0;
    return 0;
}
static int smp_call4(int cpu, void *fn, u64 a, u64 b, u64 c, u64 d)
{
    calls[cpu]++;
    assert(!spin_table[cpu].target);
    spin_table[cpu].target = (u64)fn;
    if (cpu == ack_failure)
        return SMP_TIMEOUT;
    spin_table[cpu].flag++;
    return 0;
}
''' + state + stop + all_cpus + r'''
static void reset(void)
{
    writes = 0;
    pending_failure = ack_failure = shutdown_failure = -1;
    memset(calls, 0, sizeof(calls));
    memset(cpu_stop_state, 0, sizeof(cpu_stop_state));
    for (int i = 0; i < MAX_CPUS; i++) {
        cpu_info[i].valid = true;
        cpu_info[i].impl_reg = 0x1000 * (i + 1);
        spin_table[i].flag = 1;
        spin_table[i].target = 0;
    }
}
int main(void)
{
    reset(); pending_failure = 1;
    spin_table[1].target = 0x1234;
    assert(smp_stop_secondaries(false) == -1);
    assert(calls[1] == 0 && calls[2] == 1 && calls[3] == 1 && writes == 2);
    assert(spin_table[1].target == 0x1234 && spin_table[1].flag == 1);
    assert(!spin_table[2].flag && !spin_table[3].flag);

    reset(); spin_table[1].target = 0x1234;
    assert(smp_stop_cpu(1, &cpu_info[1], false) == 0 && calls[1] == 1 && writes == 1);

    reset(); ack_failure = 1;
    assert(smp_stop_cpu(1, &cpu_info[1], true) == SMP_TIMEOUT);
    assert(cpu_stop_state[1].pending && spin_table[1].target == (u64)cpu_sleep);
    assert(smp_stop_cpu(1, &cpu_info[1], true) == SMP_TIMEOUT);
    assert(calls[1] == 1 && writes == 1);
    spin_table[1].flag++; /* Late acknowledgement of the original deep sleep. */
    assert(smp_stop_cpu(1, &cpu_info[1], false) == 0);
    assert(calls[1] == 1 && writes == 1 && !spin_table[1].flag);
    assert(!cpu_stop_state[1].pending);

    reset(); shutdown_failure = 1;
    assert(smp_stop_secondaries(false) == -1);
    assert(calls[1] == 1 && calls[2] == 1 && calls[3] == 1);
    assert(cpu_stop_state[1].pending && spin_table[1].target == (u64)cpu_sleep);
    shutdown_failure = -1;
    assert(smp_stop_cpu(1, &cpu_info[1], false) == 0 && calls[1] == 1);
    return 0;
}
'''
    compile_and_run(tmp_path, "smp-stop", harness)


def test_guest_runner_retains_shell_and_attempts_sleep_after_errors():
    source = (REPO / "proxyclient/tools/run_guest.py").read_text()
    tail = source[source.index("try:\n    hv.start()\n"):]
    events = []

    class RemoteError(Exception):
        pass

    class Guest:
        shell_locals = {}

        def start(self):
            raise RemoteError("join failed")

    class Proxy:
        def smp_stop_secondaries(self, deep):
            events.append("stop")
            raise RemoteError("one CPU did not stop")

        def sleep(self, deep):
            events.append("sleep")

    exec(tail, {
        "hv": Guest(), "p": Proxy(), "ProxyRemoteError": RemoteError,
        "run_shell": lambda *_args: events.append("shell"),
    })
    assert events == ["shell", "stop", "sleep"]
