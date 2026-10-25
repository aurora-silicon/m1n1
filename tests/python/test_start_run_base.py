# SPDX-License-Identifier: MIT
"""Run start.S's move to the run base stage 1 sets in the load header.

The production instructions are assembled into a small image, copied to an
executable staging buffer like the one stage 1 leaves the image in, and run
natively (arm64 Linux only: the copy and its cache maintenance run at EL0).
The image must stay put when run_base is zero, copy itself to run_base, record
where it was staged in relocated_from and continue in the copy when asked, and
never continue when run_base is unaligned, wraps, or does not leave the copy
below the staged image.
"""
import platform
import subprocess
import sys

import pytest

from test_smp_timeouts import REPO


def run_base_block():
    source = (REPO / "src/start.S").read_text()
    start = source.index("    adr x1, .Lrun_base\n")
    end = source.index(".Lrun_base_done:\n", start) + len(".Lrun_base_done:\n")
    return source[start:end]


@pytest.mark.skipif(sys.platform != "linux" or platform.machine() not in ("aarch64", "arm64"),
                    reason="runs arm64 code natively")
def test_start_moves_to_run_base(tmp_path):
    image = r'''
    .section .rodata
    .p2align 14
    .globl test_image_start
test_image_start:
_base:
    stp x29, x30, [sp, #-64]!
    stp x19, x20, [sp, #16]
    stp x21, x22, [sp, #32]
    str x23, [sp, #48]
''' + run_base_block() + r'''
    mov x0, #0
    b 4f
3:
    adr x0, 3b
4:
    ldr x23, [sp, #48]
    ldp x21, x22, [sp, #32]
    ldp x19, x20, [sp, #16]
    ldp x29, x30, [sp], #64
    ret
    .p2align 3
    .globl test_image_run_base
test_image_run_base:
.Lrun_base:
    .quad 0
    .globl test_image_relocated_from
test_image_relocated_from:
relocated_from:
    .quad 0
    .ascii "STACKBOT"
    .p2align 16
    .globl test_image_end
test_image_end:
_end:
'''
    harness = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

extern const unsigned char test_image_start[], test_image_end[];
extern const unsigned char test_image_run_base[], test_image_relocated_from[];

static size_t size;
static unsigned char *run_area, *staged;

static uint64_t field(unsigned char *base, const unsigned char *sym)
{
    uint64_t v;
    memcpy(&v, base + (sym - test_image_start), sizeof(v));
    return v;
}

static void stage(uint64_t run_base)
{
    memset(run_area, 0xa5, size);
    memcpy(staged, test_image_start, size);
    memcpy(staged + (test_image_run_base - test_image_start), &run_base, sizeof(run_base));
    __builtin___clear_cache((char *)staged, (char *)staged + size);
    __builtin___clear_cache((char *)run_area, (char *)run_area + size);
}

static uint64_t enter(void)
{
    return ((uint64_t (*)(void))staged)();
}

/* The image must never continue or touch memory: it parks the CPU */
static void parks(uint64_t run_base)
{
    stage(run_base);
    pid_t pid = fork();
    assert(pid >= 0);
    if (!pid) {
        enter();
        _exit(0);
    }
    usleep(300000);
    int status;
    assert(waitpid(pid, &status, WNOHANG) == 0);
    kill(pid, SIGKILL);
    assert(waitpid(pid, &status, 0) == pid);
    assert(WIFSIGNALED(status) && WTERMSIG(status) == SIGKILL);
    for (size_t i = 0; i < size; i++)
        assert(run_area[i] == 0xa5);
}

int main(void)
{
    size = test_image_end - test_image_start;
    assert(size && size % 0x10000 == 0);
    unsigned char *map = mmap(NULL, 3 * size + 0x10000, PROT_READ | PROT_WRITE | PROT_EXEC,
                              MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    assert(map != MAP_FAILED);
    /* The run base lies below the staged image, as stage 1's base lies below its heap */
    run_area = (unsigned char *)(((uintptr_t)map + 0xffff) & ~(uintptr_t)0xffff);
    staged = run_area + 2 * size;
    uint64_t run = (uint64_t)run_area;

    /* run_base zero: stays where it was staged and changes nothing */
    stage(0);
    assert(enter() == 0);
    assert(field(staged, test_image_relocated_from) == 0);
    for (size_t i = 0; i < size; i++)
        assert(run_area[i] == 0xa5);

    /* run_base set: copies [_base, _end), records the staged base, continues there */
    stage(run);
    uint64_t pc = enter();
    assert(pc >= run && pc < run + size);
    assert(field(run_area, test_image_relocated_from) == (uint64_t)staged);
    assert(field(run_area, test_image_run_base) == run);
    assert(field(staged, test_image_relocated_from) == 0);
    size_t rf = test_image_relocated_from - test_image_start;
    assert(!memcmp(run_area, staged, rf));
    assert(!memcmp(run_area + rf + 8, staged + rf + 8, size - rf - 8));
    /* A copy that ends exactly at the staged image is allowed */
    staged = run_area + size;
    stage(run);
    pc = enter();
    assert(pc >= run && pc < run + size);
    assert(field(run_area, test_image_relocated_from) == (uint64_t)staged);
    staged = run_area + 2 * size;

    /* Unaligned, overlapping, above the image, or wrapping: never continues */
    parks(run + 0x1000);
    parks((uint64_t)staged - size + 0x4000);
    parks((uint64_t)staged + 0x10000);
    parks(0xffffffffffffc000ULL);
    puts("start.S run base PASS");
    return 0;
}
'''
    asm = tmp_path / "image.S"
    asm.write_text(image)
    binary = tmp_path / "start-run-base"
    subprocess.run(
        ["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-x", "c", "-", "-x", "assembler-with-cpp",
         str(asm), "-o", str(binary)],
        input=harness, text=True, check=True,
    )
    subprocess.run([str(binary)], check=True, timeout=60)
