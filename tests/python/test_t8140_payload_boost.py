"""Check that payload loading restores T8140's inherited P-state."""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_t8140_payload_boost(tmp_path):
    source = (ROOT / 'src/cpufreq.c').read_text()
    start = source.index('#define T8140_PAYLOAD_BOOST_PSTATE')
    end = source.index('int cpufreq_init(void)', start)
    helper = source[start:end]
    program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;
#define T8140 0x8140
#define CLUSTER_PSTATE 0x20
struct cluster_t {const char *name; uint64_t base; bool performance; unsigned a,b,c;};
static unsigned chip_id, calls, last_state, inherited = 5, ladder = 17;
static bool fail;
static void *adt;
static u32 opps[64];
static int adt_path_offset(void *tree, const char *path)
{ (void)tree; return strcmp(path, "/arm-io/pmgr") ? -1 : 1; }
static const void *adt_getprop(void *tree, int node, const char *name, u32 *len)
{
 (void)tree; (void)node;
 if (strcmp(name, "voltage-states5-sram") || !ladder) return NULL;
 *len = ladder * 8;
 return opps;
}
static uint64_t read64(uint64_t addr) {assert(addr == 0x211e00020ULL);return inherited;}
static unsigned pstate_reg_to_pstate(uint64_t value) {return value;}
static int set_pstate(const struct cluster_t *c, unsigned state) {
 assert(c->base == 0x211e00000ULL);calls++;last_state=state;return fail ? -1 : 0;
}
''' + helper + r'''
int main(void) {
 chip_id=0x8122;cpufreq_payload_boost(true);assert(!calls);
 chip_id=T8140;cpufreq_payload_boost(true);assert(calls==1 && last_state==8);
 cpufreq_payload_boost(true);assert(calls==1);
 cpufreq_payload_boost(false);assert(calls==2 && last_state==5);
 cpufreq_payload_boost(false);assert(calls==2);
 inherited=3;fail=true;cpufreq_payload_boost(true);assert(calls==3 && last_state==8);
 fail=false;cpufreq_payload_boost(false);assert(calls==4 && last_state==3);

 /* A failed restore keeps iBoot's state: the next raise does not save P8 as inherited,
  * and the next restore tries the original state again. */
 inherited=4;cpufreq_payload_boost(true);assert(calls==5 && last_state==8);
 fail=true;cpufreq_payload_boost(false);assert(calls==6 && last_state==4);
 fail=false;inherited=8;cpufreq_payload_boost(true);assert(calls==6);
 cpufreq_payload_boost(false);assert(calls==7 && last_state==4);
 cpufreq_payload_boost(false);assert(calls==7);

 /* P8 must be on the ADT's P-cluster ladder, and so must the inherited state. */
 inherited=2;ladder=7;cpufreq_payload_boost(true);assert(calls==7);
 ladder=0;cpufreq_payload_boost(true);assert(calls==7);
 ladder=17;inherited=0;cpufreq_payload_boost(true);assert(calls==7);
 inherited=18;cpufreq_payload_boost(true);assert(calls==7);
 cpufreq_payload_boost(false);assert(calls==7);
 inherited=17;cpufreq_payload_boost(true);assert(calls==8 && last_state==8);
 cpufreq_payload_boost(false);assert(calls==9 && last_state==17);
 puts("T8140 payload boost and inherited-state restoration passed");
}
'''
    file = tmp_path / 'boost.c'
    file.write_text(program)
    binary = tmp_path / 'boost'
    subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', str(file), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)


def test_t8140_pstate_request_waits_and_checks(tmp_path):
    source = (ROOT / 'src/cpufreq.c').read_text()
    start = source.index('static u32 pstate_reg_to_pstate(u64 val)')
    end = source.index('int cpufreq_init_cluster(', start)
    functions = source[start:end]
    names = set(re.findall(r'\bCLUSTER_[A-Z0-9_]+\b', functions))
    macros = '\n'.join(re.search(r'^#define ' + name + r'\s+[^\n]+', source, re.M)[0]
                       for name in sorted(names))
    program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include "soc.h"
typedef uint32_t u32;
typedef uint64_t u64;
#define BIT(n) (UINT64_C(1) << (n))
#define GENMASK(h, l) ((UINT64_MAX << (l)) & (UINT64_MAX >> (63 - (h))))
#define FIELD_GET(mask, value) (((value) & (mask)) >> __builtin_ctzll(mask))
#define FIELD_PREP(mask, value) (((u64)(value) << __builtin_ctzll(mask)) & (mask))
struct cluster_t { const char *name; u64 base; bool pcluster; u32 apsc_pstate, default_pstate, voltage_ctl; };
''' + macros + r'''
static u32 chip_id = T8140;
static u64 reg, writes;
static bool busy_before, stuck, refuse;
static u64 read64(u64 addr) { (void)addr; return reg; }
static void write64(u64 addr, u64 value)
{
    (void)addr;
    writes++;
    /* A refused request leaves the desired state where it was. */
    reg = refuse ? (reg & ~CLUSTER_PSTATE_SET) : (value & ~CLUSTER_PSTATE_SET);
    if (stuck)
        reg |= CLUSTER_PSTATE_BUSY;
}
static int poll64(u64 addr, u64 mask, u64 target, u32 timeout)
{
    (void)addr; (void)target; (void)timeout;
    if (busy_before) return -1;
    return (reg & mask) ? -1 : 0;
}
''' + functions + r'''
int main(void)
{
    const struct cluster_t c = {"PCPU", 0x211e00000, true, 0, 0, 0};
    reg = 1;
    assert(!set_pstate(&c, 8) && writes == 1 && pstate_reg_to_pstate(reg) == 8);
    /* Busy before the request: nothing is written. */
    busy_before = true;
    assert(set_pstate(&c, 1) == -1 && writes == 1);
    busy_before = false;
    /* A request the cluster does not take is reported. */
    refuse = true;
    assert(set_pstate(&c, 2) == -1 && writes == 2 && pstate_reg_to_pstate(reg) == 8);
    refuse = false;
    stuck = true;
    assert(set_pstate(&c, 3) == -1);
    puts("T8140 P-state request protocol passed");
    return 0;
}
'''
    file = tmp_path / 'request.c'
    file.write_text(program)
    binary = tmp_path / 'request'
    subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-I',
                    str(ROOT / 'src'), str(file), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
