"""Exercise the production iBoot range comparator with boundary values."""

from pathlib import Path
import subprocess


def test_firmware_iboot_range_bounds(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/firmware.c").read_text()
    start = source.index("bool firmware_iboot_in_range(")
    end = source.index("// Note: semi-open range", start)
    comparator = source[start:end]
    header = (repo / "src/firmware.h").read_text()
    version_count = next(line for line in header.splitlines()
                         if line.startswith("#define IBOOT_VER_COMP "))

    harness = """
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
typedef uint32_t u32;
""" + version_count + "\n" + comparator + """
int main(void)
{
    u32 lo[IBOOT_VER_COMP] = {1, 2, 3, 4, 5};
    u32 hi[IBOOT_VER_COMP] = {1, 2, 3, 5, 0};
    u32 below[IBOOT_VER_COMP] = {1, 2, 3, 4, 4};
    u32 inside[IBOOT_VER_COMP] = {1, 2, 3, 4, 6};
    u32 above[IBOOT_VER_COMP] = {1, 2, 3, 5, 1};
    assert(!firmware_iboot_in_range(lo, hi, below));
    assert(firmware_iboot_in_range(lo, hi, lo));
    assert(firmware_iboot_in_range(lo, hi, inside));
    assert(!firmware_iboot_in_range(lo, hi, hi));
    assert(!firmware_iboot_in_range(lo, hi, above));
    return 0;
}
"""
    program = tmp_path / "firmware-range"
    subprocess.run(
        ["cc", "-std=c11", "-Wall", "-Wextra", "-fsanitize=address,undefined",
         "-I", str(repo / "src"), "-x", "c", "-", "-o", str(program)],
        input=harness,
        text=True,
        check=True,
    )
    subprocess.run([str(program)], check=True)
