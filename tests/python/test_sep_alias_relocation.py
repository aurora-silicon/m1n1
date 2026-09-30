"""dt_set_sep() must survive the FDT moving under the sep alias string.

Adding /reserved-memory/sep-firmware shifts every property stored after
/reserved-memory in the blob. When /aliases follows it (as in the J700 base
DT), a pointer returned by fdt_get_alias() before the reservation reads
another string afterwards, and memory-region lands on the wrong node or none.
"""

from pathlib import Path
import subprocess


def extract(source, start, end):
    first = source.index(start)
    return source[first:source.index(end, first)]


def test_sep_alias_relocation(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/kboot.c").read_text()
    add_mem_region = extract(source, "static int dt_device_add_mem_region(",
                             "\nstatic ")
    set_sep = extract(source, "static int dt_set_sep(void)", "static int dt_set_sio_fwdata(")
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "libfdt.h"
typedef uint64_t u64;
typedef uint32_t u32;
#define bail(...) do { printf(__VA_ARGS__); return -1; } while (0)
#define ADT_GETPROP_ARRAY(a, n, p, v) adt_get((n), (p), (v), sizeof(v))
static unsigned char blob[16384];
static void *dt = blob;
static void *adt;
static unsigned char lpol[] = {1, 2, 3, 4}, ibot[] = {5, 6, 7};

static int adt_path_offset(void *a, const char *path)
{
    (void)a;
    if (!strcmp(path, "/chosen/memory-map")) return 1;
    if (!strcmp(path, "/chosen/boot-object-manifests")) return 2;
    return -1;
}

static int adt_get(int node, const char *prop, void *out, size_t len)
{
    u64 pair[2];
    if (node == 1 && !strcmp(prop, "SEPFW")) {
        pair[0] = 0x10000000000; pair[1] = 0x400000;
    } else if (node == 2 && !strcmp(prop, "lpol")) {
        pair[0] = (u64)(uintptr_t)lpol; pair[1] = sizeof(lpol);
    } else if (node == 2 && !strcmp(prop, "ibot")) {
        pair[0] = (u64)(uintptr_t)ibot; pair[1] = sizeof(ibot);
    } else {
        return -1;
    }
    memcpy(out, pair, len);
    return len;
}

/* In place, like kboot.c: the blob grows inside its buffer. */
static int dt_get_or_add_reserved_mem(const char *name, const char *compat, bool nomap,
                                      u64 addr, size_t size)
{
    (void)nomap;
    int node = fdt_add_subnode(dt, fdt_path_offset(dt, "/reserved-memory"), name);
    assert(node >= 0);
    assert(fdt_setprop_string(dt, node, "compatible", compat) == 0);
    u64 reg[] = {cpu_to_fdt64(addr), cpu_to_fdt64(size)};
    assert(fdt_setprop(dt, node, "reg", reg, sizeof(reg)) == 0);
    assert(fdt_setprop_u32(dt, node, "phandle", 0x42) == 0);
    return node;
}
''' + add_mem_region + "\n" + set_sep + r'''
int main(void)
{
    /* fdt_add_subnode() inserts before existing siblings: build back to front
     * so the blob holds /reserved-memory, /soc, /aliases in that order. */
    assert(fdt_create_empty_tree(dt, sizeof(blob)) == 0);
    int aliases = fdt_add_subnode(dt, 0, "aliases");
    assert(aliases >= 0);
    assert(fdt_setprop_string(dt, aliases, "serial0", "/soc/serial@30812c000") == 0);
    aliases = fdt_path_offset(dt, "/aliases");
    assert(fdt_setprop_string(dt, aliases, "sep", "/soc/sep@0") == 0);
    int soc = fdt_add_subnode(dt, 0, "soc");
    assert(soc >= 0);
    assert(fdt_add_subnode(dt, soc, "sep@0") >= 0);
    assert(fdt_add_subnode(dt, fdt_path_offset(dt, "/soc"), "serial@30812c000") >= 0);
    assert(fdt_add_subnode(dt, 0, "reserved-memory") >= 0);
    assert(fdt_path_offset(dt, "/reserved-memory") < fdt_path_offset(dt, "/soc"));
    assert(fdt_path_offset(dt, "/soc") < fdt_path_offset(dt, "/aliases"));

    assert(dt_set_sep() == 0);

    int len, sep = fdt_path_offset(dt, "/soc/sep@0");
    const fdt32_t *region = fdt_getprop(dt, sep, "memory-region", &len);
    assert(region && len == 4 && fdt32_to_cpu(*region) == 0x42);
    const char *name = fdt_getprop(dt, sep, "memory-region-names", &len);
    assert(name && !strcmp(name, "sepfw"));
    const unsigned char *prop = fdt_getprop(dt, sep, "local-policy-manifest", &len);
    assert(prop && len == sizeof(lpol) && !memcmp(prop, lpol, len));
    prop = fdt_getprop(dt, sep, "iboot-manifest", &len);
    assert(prop && len == sizeof(ibot) && !memcmp(prop, ibot, len));
    assert(!fdt_getprop(dt, fdt_path_offset(dt, "/soc/serial@30812c000"), "memory-region", NULL));
    assert(fdt_check_header(dt) == 0);
    return 0;
}
'''
    binary = tmp_path / "sep-alias-relocation"
    libfdt = repo / "src/libfdt"
    sources = [libfdt / name for name in (
        "fdt.c", "fdt_addresses.c", "fdt_empty_tree.c",
        "fdt_ro.c", "fdt_rw.c", "fdt_strerror.c", "fdt_sw.c", "fdt_wip.c",
    )]
    subprocess.run(
        ["cc", "-D_GNU_SOURCE", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(libfdt),
         "-x", "c", "-", *map(str, sources), "-o", str(binary)],
        input=harness, text=True, check=True,
    )
    subprocess.run([str(binary)], check=True)
