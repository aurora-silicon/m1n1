"""Recognize the boot firmware captured from J616s without prefix matching."""

from pathlib import Path
import subprocess


def test_j616s_boot_firmware_detection(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/firmware.c").read_text()
    start = source.index("const struct fw_version_info fw_versions[")
    end = source.index("\n};", start) + len("\n};")
    table = source[start:end]
    start = source.index("static void detect_firmware(")
    end = source.index("bool firmware_iboot_in_range(", start)
    detector = source[start:end]
    harness = """
#include <assert.h>
#include <string.h>
#include "firmware.h"
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
""" + table + "\n" + detector + """
int main(void)
{
    struct fw_version_info info;
    detect_firmware(&info, "mBoot-18000.161.9");
    assert(info.version == V26_6_1);
    assert(!strcmp(info.string, "26.6.1"));
    assert(info.num_length == 3);
    assert(info.num[0] == 26 && info.num[1] == 6 && info.num[2] == 1);
    assert(info.version > V15_0B1);

    detect_firmware(&info, "mBoot-18000.161.10");
    assert(info.version == V26_6_2);
    assert(V26_4 < V26_6_1 && V26_6_1 < V26_6_2);

    detect_firmware(&info, "mBoot-18000.161.91");
    assert(info.version == V_UNKNOWN);
    assert(!strcmp(info.iboot, "mBoot-18000.161.91"));
    return 0;
}
"""
    program = tmp_path / "firmware-detection"
    subprocess.run(
        ["cc", "-std=c11", "-Wall", "-Wextra", "-fsanitize=address,undefined",
         "-I", str(repo / "src"), "-x", "c", "-", "-o", str(program)],
        input=harness, text=True, check=True,
    )
    subprocess.run([str(program)], check=True)
