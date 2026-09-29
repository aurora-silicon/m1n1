"""Check the production PCIe ADT geometry gate without hardware."""

from pathlib import Path
import subprocess


def test_pcie_port_geometry(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/pcie.c").read_text()
    start = source.index("static int pcie_port_reg_count(")
    end = source.index("/* Check every T8140 ADT window", start)
    helper = source[start:end]
    harness = """
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
typedef uint32_t u32;
""" + helper + """
int main(void)
{
    assert(pcie_port_reg_count(25 * 16, 3, 7, true) == 6);
    assert(pcie_port_reg_count(25 * 16, 3, 7, false) == 6);
    assert(pcie_port_reg_count(25 * 16, 0, 7, true) < 0);
    assert(pcie_port_reg_count(25 * 16, 9, 7, true) < 0);
    assert(pcie_port_reg_count(25 * 16 - 1, 3, 7, true) < 0);
    assert(pcie_port_reg_count(6 * 16, 3, 7, true) < 0);
    assert(pcie_port_reg_count(7 * 16, 3, 7, true) < 0);
    assert(pcie_port_reg_count(24 * 16, 3, 7, true) < 0);
    assert(pcie_port_reg_count(22 * 16, 3, 7, true) < 0);
    assert(pcie_port_reg_count(22 * 16, 3, 7, false) == 5);
    return 0;
}
"""
    binary = tmp_path / "pcie-geometry"
    subprocess.run(
        ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-x", "c", "-", "-o", str(binary)],
        input=harness,
        text=True,
        check=True,
    )
    subprocess.run([str(binary)], check=True)
