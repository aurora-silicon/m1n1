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
{ return ++calls == fail_call ? dispatch_error : 0; }
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

    u64 regs[4] = {0};
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
