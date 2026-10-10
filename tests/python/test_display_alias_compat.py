"""The shared display reservation keeps its older-SoC alias fallback."""

from pathlib import Path
import subprocess


def test_missing_display_alias_is_fatal_only_on_t8140(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/kboot.c").read_text()
    helper = source[source.index("struct disp_mapping {"):source.index("static int dt_carveout_reserved_regions(")]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef unsigned long u64;
typedef uint32_t u32;
typedef int dart_dev_t;
#define T8140 0x8140
#define bail_cleanup(...) do { ret = -1; goto err; } while (0)
static void *dt;
static u32 chip_id;
static const char *missing;
static int fdt_path_offset(const void *tree, const char *path)
{ (void)tree; return !strcmp(path, missing) ? -1 : 1; }
static dart_dev_t *dt_init_dart_by_node(int node, int index)
{ static dart_dev_t dart; (void)node; (void)index; return &dart; }
static uint32_t fdt_get_phandle(const void *tree, int node)
{ (void)tree; (void)node; return 1; }
static int dt_get_or_add_reserved_mem(const char *a, const char *b, bool c, u64 d, u64 e)
{ (void)a; (void)b; (void)c; (void)d; (void)e; return 1; }
static int dt_device_set_reserved_mem_from_dart(int node, ...)
{ (void)node; return 0; }
static int dt_device_add_mem_region(const char *path, u32 phandle, const char *name)
{ (void)path; (void)phandle; (void)name; return 0; }
static int dt_get_iommu_node(int node, u32 index)
{ (void)node; (void)index; return 1; }
static int fdt_setprop_string(void *tree, int node, const char *name, const char *value)
{ (void)tree; (void)node; (void)name; (void)value; return 0; }
static void dart_shutdown(dart_dev_t *dart) { (void)dart; }
''' + helper + r'''
int main(void)
{
    const char *aliases[] = {"dcp", "disp", "piodma"};
    for (int i = 0; i < 3; i++) {
        missing = aliases[i];
        chip_id = 0x8103;
        assert(dt_add_reserved_regions("dcp", "disp", "piodma", NULL,
                                       NULL, NULL, 0) == 0);
        chip_id = T8140;
        assert(dt_add_reserved_regions("dcp", "disp", "piodma", NULL,
                                       NULL, NULL, 0) == -1);
    }
    return 0;
}
'''
    binary = tmp_path / "display-alias-compat"
    subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-x", "c", "-", "-o", str(binary)],
                   input=harness, text=True, check=True)
    subprocess.run([str(binary)], check=True)
