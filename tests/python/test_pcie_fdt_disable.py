"""Exercise the production T8140 PCIe FDT skip and its IOMMU references."""

from pathlib import Path
import subprocess


def test_t8140_pcie_nodes_disabled(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/kboot.c").read_text()
    start = source.index("static int dt_add_pcie_iommu(")
    end = source.index("static int dt_get_iommu_node(", start)
    helper = source[start:end]
    radio = (repo / "src/pcie_t8140.c").read_text()
    piodma = radio[radio.index("int pcie_t8140_disable_piodma("):
                   radio.index("static int radio_delprop(")]
    harness = r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "libfdt.h"
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
static void *dt;
''' + piodma + helper + r'''
static void add_node(int parent, const char *name)
{
    assert(fdt_add_subnode(dt, parent, name) >= 0);
}

static void set_node(const char *path, const char *type)
{
    int node = fdt_path_offset(dt, path);
    assert(node >= 0);
    assert(fdt_setprop_string(dt, node, "device_type", type) == 0);
    node = fdt_path_offset(dt, path);
    assert(fdt_setprop_string(dt, node, "status", "okay") == 0);
}

static void expect_status(const char *path, const char *status)
{
    int node = fdt_path_offset(dt, path);
    assert(node >= 0);
    const char *value = fdt_getprop(dt, node, "status", NULL);
    assert(value && !strcmp(value, status));
}

static void set_iommu(const char *path, unsigned int phandle)
{
    int node = fdt_path_offset(dt, path);
    assert(node >= 0);
    assert(fdt_setprop_u32(dt, node, "phandle", phandle) == 0);
    node = fdt_path_offset(dt, path);
    assert(fdt_setprop_u32(dt, node, "#iommu-cells", 1) == 0);
    node = fdt_path_offset(dt, path);
    assert(fdt_setprop_string(dt, node, "status", "okay") == 0);
}

int main(void)
{
    unsigned char blob[8192];
    dt = blob;
    assert(fdt_create_empty_tree(dt, sizeof(blob)) == 0);
    add_node(0, "serial@0");
    add_node(0, "pcie@1");
    add_node(fdt_path_offset(dt, "/pcie@1"), "port@0");
    add_node(0, "pcie@2");
    add_node(fdt_path_offset(dt, "/pcie@2"), "port@1");
    add_node(fdt_path_offset(dt, "/pcie@1"), "piodma@0");
    add_node(0, "iommu@390000000");
    add_node(fdt_path_offset(dt, "/iommu@390000000"), "iommu-mapper@0");
    add_node(fdt_path_offset(dt, "/iommu@390000000"), "piodma@1");
    add_node(0, "iommu@391000000");
    add_node(0, "iommu@other");
    add_node(0, "dma-controller@390030000");
    add_node(0, "dma-controller@other");
    assert(!fdt_setprop_string(dt, fdt_path_offset(dt, "/dma-controller@390030000"),
                              "compatible", "apple,t8140-piodma-diagnostic"));
    assert(!fdt_setprop_string(dt, fdt_path_offset(dt, "/dma-controller@other"),
                              "compatible", "other,dma"));
    set_node("/serial@0", "serial");
    set_node("/pcie@1", "pci");
    set_node("/pcie@1/port@0", "pci");
    set_node("/pcie@2", "pci");
    set_node("/pcie@2/port@1", "pci");
    set_iommu("/iommu@390000000", 10);
    set_iommu("/iommu@391000000", 11);
    set_iommu("/iommu@other", 12);
    assert(fdt_setprop_string(dt, fdt_path_offset(dt, "/iommu@390000000/iommu-mapper@0"),
                              "status", "okay") == 0);
    assert(fdt_setprop_string(dt, fdt_path_offset(dt, "/iommu@390000000/piodma@1"),
                              "status", "okay") == 0);
    assert(fdt_setprop_string(dt, fdt_path_offset(dt, "/pcie@1/piodma@0"),
                              "status", "okay") == 0);
    fdt32_t map[] = {cpu_to_fdt32(0x100), cpu_to_fdt32(10),
                     cpu_to_fdt32(1), cpu_to_fdt32(2),
                     cpu_to_fdt32(0x200), cpu_to_fdt32(10),
                     cpu_to_fdt32(3), cpu_to_fdt32(1)};
    assert(fdt_setprop(dt, fdt_path_offset(dt, "/pcie@1"), "iommu-map",
                       map, sizeof(map)) == 0);
    fdt32_t iommus[] = {cpu_to_fdt32(11), cpu_to_fdt32(17)};
    assert(fdt_setprop(dt, fdt_path_offset(dt, "/pcie@2"), "iommus",
                       iommus, sizeof(iommus)) == 0);
    assert(dt_disable_t8140_pcie() == 0);
    expect_status("/pcie@1", "disabled");
    expect_status("/pcie@1/port@0", "disabled");
    expect_status("/pcie@2", "disabled");
    expect_status("/pcie@2/port@1", "disabled");
    expect_status("/pcie@1/piodma@0", "disabled");
    expect_status("/iommu@390000000", "disabled");
    expect_status("/iommu@390000000/iommu-mapper@0", "disabled");
    expect_status("/iommu@390000000/piodma@1", "disabled");
    expect_status("/iommu@391000000", "disabled");
    expect_status("/iommu@other", "okay");
    expect_status("/serial@0", "okay");
    expect_status("/dma-controller@390030000", "disabled");
    assert(!fdt_getprop(dt, fdt_path_offset(dt, "/dma-controller@other"), "status", NULL));
    assert(fdt_pack(dt) == 0);
    assert(fdt_check_header(dt) == 0);

    assert(fdt_create_empty_tree(dt, sizeof(blob)) == 0);
    add_node(0, "pcie@bad");
    set_node("/pcie@bad", "pci");
    fdt32_t bad_map[] = {cpu_to_fdt32(0), cpu_to_fdt32(99),
                         cpu_to_fdt32(1), cpu_to_fdt32(1)};
    assert(fdt_setprop(dt, fdt_path_offset(dt, "/pcie@bad"), "iommu-map",
                       bad_map, sizeof(bad_map)) == 0);
    assert(dt_disable_t8140_pcie() == -1);
    expect_status("/pcie@bad", "okay");
    return 0;
}
'''
    binary = tmp_path / "pcie-fdt-disable"
    libfdt = repo / "src/libfdt"
    sources = [libfdt / name for name in (
        "fdt.c", "fdt_addresses.c", "fdt_check.c", "fdt_empty_tree.c",
        "fdt_ro.c", "fdt_rw.c", "fdt_strerror.c", "fdt_sw.c", "fdt_wip.c",
    )]
    subprocess.run(
        ["cc", "-D_GNU_SOURCE", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(libfdt),
         "-x", "c", "-", *map(str, sources), "-o", str(binary)],
        input=harness,
        text=True,
        check=True,
    )
    subprocess.run([str(binary)], check=True)
