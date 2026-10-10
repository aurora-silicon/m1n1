"""An FDT reservation with no space fails both ISP handoff paths."""

from pathlib import Path
import subprocess


def test_isp_reservation_no_space(tmp_path):
    source = (Path(__file__).resolve().parents[2] / "src/kboot.c").read_text()
    firmware = source[source.index("static int dt_reserve_asc_firmware("):
                      source.index("static int dt_set_ave(")]
    heap = source[source.index("static int dt_set_isp_fwdata(void)"):
                  source.index("static int dt_disable_missing_devs(")]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef unsigned long u64;
typedef uint32_t u32;
#define SZ_16K 0x4000
#define ALIGN_UP(x, a) (((x) + (a) - 1) & ~((a) - 1))
#define V13_5 0
#define V13_6_1 1
#define bail(...) do { return -1; } while (0)
struct fw_version_info { int version; };
static struct fw_version_info os_firmware = {0}, fw_versions[2];
struct adt_segment_ranges { u64 phys, iova, remap; u32 size, flags; };
static struct adt_segment_ranges segment = {0x100000, 0x200000, 0, 0x4000, 0};
static void *dt, *adt;
static int fdt_path_offset(const void *tree, const char *path)
{ (void)tree; (void)path; return 1; }
static int adt_path_offset(const void *tree, const char *path)
{ (void)tree; (void)path; return 1; }
static uint32_t fdt_get_phandle(const void *tree, int node)
{ (void)tree; (void)node; return 1; }
static int fdt_generate_phandle(const void *tree, u32 *value)
{ (void)tree; *value = 1; return 0; }
static int fdt_setprop_u32(void *tree, int node, const char *name, u32 value)
{ (void)tree; (void)node; (void)name; (void)value; return 0; }
static const void *adt_getprop(const void *tree, int node, const char *name, u32 *len)
{ (void)tree; (void)node; (void)name; *len = sizeof(segment); return &segment; }
static int dt_get_or_add_reserved_mem(const char *name, const char *compat,
                                      bool no_map, u64 phys, u64 size)
{ (void)name; (void)compat; (void)no_map; (void)phys; (void)size; return -3; }
static int dt_device_set_reserved_mem(int node, const char *name, u32 handle,
                                      u64 iova, u64 size)
{ (void)node; (void)name; (void)handle; (void)iova; (void)size; return 0; }
static int dt_device_add_mem_region(const char *path, u32 handle, const char *name)
{ (void)path; (void)handle; (void)name; return 0; }
static int firmware_set_fdt(void *tree, int node, const char *name,
                            const struct fw_version_info *version)
{ (void)tree; (void)node; (void)name; (void)version; return 0; }
static int isp_get_heap(u64 *phys, u64 *iova, u64 *size)
{ *phys = 0x300000; *iova = 0x400000; *size = 0x4000; return 0; }
static const void *fdt_getprop(const void *tree, int node, const char *name, int *len)
{ (void)tree; (void)node; (void)name; (void)len; return "okay"; }
static int fdt_setprop_string(void *tree, int node, const char *name, const char *value)
{ (void)tree; (void)node; (void)name; (void)value; return 0; }
''' + firmware + heap + r'''
int main(void)
{
    assert(dt_reserve_asc_firmware("/arm-io/isp", NULL, "isp", false, 0) == -1);
    assert(dt_set_isp_fwdata() == -1);
    return 0;
}
'''
    binary = tmp_path / "isp-reservation-full"
    subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-x", "c", "-",
                    "-o", str(binary)], input=harness, text=True, check=True)
    subprocess.run([str(binary)], check=True)
