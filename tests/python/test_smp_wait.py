"""Legacy internal waits must observe completion; proxy waits remain bounded."""
import pathlib
import re
import subprocess


ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_legacy_wait_preserves_completion_and_return_value(tmp_path):
    source = (ROOT / 'src/smp.c').read_text()
    function = re.search(r'u64 smp_wait\(int cpu\)\n\{.*?\n\}', source, re.S).group()
    harness = r'''
#include <stdint.h>
#include <assert.h>
typedef uint64_t u64;
#define MAX_CPUS 2
struct spin_table { volatile u64 target, retval; } spin_table[MAX_CPUS];
static unsigned fences, timed_calls;
static void fence(void) {
    if (++fences == 4) spin_table[1].target = 0;
}
#define sysop(op) fence()
static int smp_wait_timed(int cpu, u64 *retval, unsigned timeout) {
    (void)cpu; (void)retval; (void)timeout;
    ++timed_calls;
    return -1;
}
'''
    harness += function + r'''
int main(void) {
    spin_table[1].target = 1;
    spin_table[1].retval = UINT64_C(0x123456789abcdef0);
    assert(smp_wait(1) == spin_table[1].retval);
    assert(fences == 4 && timed_calls == 0);
    assert(smp_wait(-1) == 0 && smp_wait(MAX_CPUS) == 0);
    assert(fences == 4 && timed_calls == 0);
    return 0;
}
'''
    file = tmp_path / 'wait.c'
    binary = tmp_path / 'wait'
    file.write_text(harness)
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', str(file), '-o', str(binary)],
                   check=True, capture_output=True)
    subprocess.run([str(binary)], check=True, timeout=5)
