#!/usr/bin/env python3
"""Host-test the production read-only DART walker, including last table entries."""
from pathlib import Path
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[2] / 'src/dart.c').read_text()
function = source[source.index('int dart_visit_locked_t8110('):source.index('u64 dart_search(')]
constants = '\n'.join(line for line in source.splitlines() if line.startswith('#define DART_') and '\\' not in line)
harness = r'''
#include <assert.h>
#include <stdint.h>
#include <stddef.h>
#include <sys/mman.h>
#include <stdio.h>
typedef uint64_t u64;
typedef uint32_t u32;
typedef uint8_t u8;
#define BIT(n) (1ULL << (n))
#define GENMASK(h,l) (((~0ULL) >> (63-(h))) & ((~0ULL) << (l)))
#define FIELD_GET(mask, value) (((value) & (mask)) >> __builtin_ctzll(mask))
#define SZ_16K 0x4000
static u32 regs[0x2000 / 4];
static u32 read32(uintptr_t addr) {
    assert(addr >= 0x28d304000ULL && addr < 0x30a302000ULL);
    return regs[(addr - 0x28d304000ULL) / 4];
}
static int visits, refuse;
static int visited(u64 iova, u64 physical, void *opaque) {
    assert(opaque == (void *)42);
    assert(iova == 0xfffffc000ULL && physical == 0x10008000);
    visits++;
    return refuse ? -7 : 0;
}
'''
harness += constants + '\n' + function
harness += r'''
int main(void) {
    u64 *root = mmap((void *)0x10000000, 0x10000, PROT_READ | PROT_WRITE,
                    MAP_ANONYMOUS | MAP_PRIVATE | MAP_FIXED_NOREPLACE, -1, 0);
    assert(root == (void *)0x10000000);
    u64 *leaf = (void *)0x10004000;
    regs[0x200/4] = 1;
    regs[(0x1000+16*4)/4] = 1;
    regs[(0x1400+16*4)/4] = (0x10000000 >> 12) | 1;
    root[2047] = (0x10004000 >> 4) | 1;
    leaf[2047] = (0x10008000 >> 4) | 1;
#define WALK(size) dart_visit_locked_t8110(0x28d304000ULL,16,0x10000000,size,visited,(void *)42)
    assert(WALK(0x10000) == 0 && visits == 1);
    refuse = 1; assert(WALK(0x10000) == -7); refuse = 0;
    visits = 0;
    assert(WALK(1) < 0 && visits == 0);
    root[2047] = (0x10010000 >> 4) | 1;
    assert(WALK(0x10000) < 0 && visits == 0);
    root[2047] = (0x10004000 >> 4) | 1;
    regs[(0x1400+16*4)/4] = (0x10010000 >> 12) | 1;
    assert(WALK(0x10000) < 0 && visits == 0);
    regs[(0x1400+16*4)/4] = (0x10000000 >> 12) | 1;
    regs[(0x1000+16*4)/4] = 0;
    assert(WALK(0x10000) < 0 && visits == 0);
    regs[(0x1000+16*4)/4] = 1;
    regs[0x200/4] = 0;
    assert(WALK(0x10000) < 0 && visits == 0);
    puts("locked DART walker bounds and last-entry checks passed");
}
'''
with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp)
    (p/'test.c').write_text(harness)
    subprocess.run(['cc','-std=gnu11','-Wall','-Werror',str(p/'test.c'),'-o',str(p/'test')],check=True)
    subprocess.run([str(p/'test')],check=True)
