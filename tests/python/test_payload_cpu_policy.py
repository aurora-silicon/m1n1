"""Exercise payload CPU setup with failures on older chips and T8140."""

from test_smp_timeouts import REPO, compile_and_run


def test_payload_handoff_policy_and_best_effort_mitigations(tmp_path):
    payload = (REPO / "src/payload.c").read_text()
    setup = payload[payload.index("        cpufreq_init();"):
                    payload.index("        for (size_t i = 0; i < chosen_cnt; i++)")]
    mitigations = (REPO / "src/mitigations.c").read_text()
    mitigations = mitigations[mitigations.index("int mitigations_perform("):]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
typedef uint64_t u64;
#define MAX_CPUS 4
#define T8140 0x8140
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define smp_call0(cpu, fn) smp_call4(cpu, fn, 0, 0, 0, 0)
static int chip_id, boot_cpu_idx, start_error, fail_mitigation_cpu, fail_tso_cpu;
static int mitigation_calls, tso_calls, advertised_tso;
static bool enable_tso = true;
struct mitigation { const char *name; bool vulnerable, mitigated; };
static struct mitigation mitigations[] = {{"test", true, false}};
static void apply_mitigations(void) {}
static void do_enable_tso(void) {}
static void cpufreq_init(void) {}
static bool smp_is_alive(int cpu) { return cpu > 0; }
static int smp_start_secondaries(void) { return start_error; }
static int smp_call4(int cpu, void *fn, u64 a, u64 b, u64 c, u64 d)
{
    if (fn == apply_mitigations) {
        mitigation_calls++;
        return cpu == fail_mitigation_cpu ? -1 : 0;
    }
    tso_calls++;
    return cpu == fail_tso_cpu ? -1 : 0;
}
static int smp_wait(int cpu, u64 *retval) { return 0; }
static void kboot_set_chosen(const char *key, const char *value) { advertised_tso++; }
''' + mitigations + '\nstatic int prepare_cpus(void)\n{\n' + setup + r'''
    return 0;
boot_failed:
    return -1;
}
static void reset(void)
{
    start_error = fail_mitigation_cpu = fail_tso_cpu = 0;
    mitigation_calls = tso_calls = advertised_tso = 0;
}
int main(void)
{
    int chips[] = {0x8103, 0x8112, 0x8122, T8140};
    for (unsigned i = 0; i < ARRAY_SIZE(chips); i++) {
        chip_id = chips[i];
        reset();
        assert(prepare_cpus() == 0 && advertised_tso == 1);
        reset(); fail_mitigation_cpu = 1;
        assert(prepare_cpus() == (chip_id == T8140 ? -1 : 0));
        assert(mitigation_calls == 3); /* Every live CPU is still attempted. */
        reset(); fail_tso_cpu = 1;
        assert(prepare_cpus() == (chip_id == T8140 ? -1 : 0));
        assert(!advertised_tso);
        assert(tso_calls == (chip_id == T8140 ? 1 : 3));
        reset(); start_error = -1;
        assert(prepare_cpus() == (chip_id == T8140 ? -1 : 0));
    }
    return 0;
}
'''
    compile_and_run(tmp_path, "payload-cpu-policy", harness)
