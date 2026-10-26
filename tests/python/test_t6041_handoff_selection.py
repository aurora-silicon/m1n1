# SPDX-License-Identifier: MIT
"""Test the real handoff selector with real libfdt and synthetic DT data."""
from pathlib import Path
import os
import re
import subprocess


def block(source, marker):
    start = source.index(marker)
    opening = source.index("{", start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def test_t6041_handoff_selection(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/kboot_display_handoff.c").read_text()
    selector = block(source, "static bool dcp6_armed(void *dt)")
    profiles = []
    for name in ("t6041", "t6030"):
        profile = block(source, f"static const struct dcp_soc dcp_soc_{name} =")
        fields = []
        for field in ("optin", "os_fw", "dcp_compat"):
            value = re.search(r"\." + field + r'\s*=\s*("(?:\\.|[^"\\])*"|[A-Za-z_][A-Za-z_0-9]*)\s*,', profile)
            assert value is not None
            fields.append(f".{field} = {value[1]}")
        profiles.append(f"static const struct dcp_soc profile_{name} = {{" + ", ".join(fields) + "};")
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "libfdt.h"
enum fw_version { V_UNKNOWN, V14_7, V26_6_2 };
struct dcp_soc { const char *optin; enum fw_version os_fw; const char *dcp_compat; };
static struct { const struct dcp_soc *soc; } dcp6;
static struct { enum fw_version version; } os_firmware;
''' + "\n".join(profiles) + "\n" + selector + r'''
static unsigned char original[8192], dt[8192];
static unsigned int cases;
static void reset(void)
{
    assert(!fdt_open_into(original, dt, sizeof(dt)));
    dcp6.soc = &profile_t6041;
    os_firmware.version = V26_6_2;
}
static void expect(bool expected)
{
    unsigned char before[sizeof(dt)];
    memcpy(before, dt, sizeof(dt));
    assert(dcp6_armed(dt) == expected);
    assert(!memcmp(before, dt, sizeof(dt)));
    cases++;
}
int main(int argc, char **argv)
{
    assert(argc == 2);
    FILE *file = fopen(argv[1], "rb");
    assert(file);
    assert(fread(original, 1, sizeof(original), file) > 0);
    fclose(file);
    assert(!fdt_check_header(original));
    reset(); expect(true); /* The DT and qualified firmware suffice. */
    reset();
    assert(!fdt_setprop_string(dt, fdt_path_offset(dt, "/chosen"), "asahi,t6041-dcp", "0"));
    expect(true); /* The obsolete machine switch no longer dictates support. */
    reset(); os_firmware.version = V14_7; expect(false);
    reset(); os_firmware.version = V_UNKNOWN; expect(false);
    reset();
    assert(!fdt_delprop(dt, fdt_path_offset(dt, "/aliases"), "dcp"));
    expect(false);
    reset();
    assert(!fdt_setprop_string(dt, fdt_path_offset(dt, "/dcp"), "compatible", "apple,dcp"));
    expect(false);
    reset();
    assert(!fdt_del_node(dt, fdt_path_offset(dt, "/dcp")));
    assert(!fdt_setprop_string(dt, fdt_path_offset(dt, "/chosen"), "asahi,t6041-dcp", "1"));
    expect(false); /* A stale chosen line cannot manufacture the missing DT. */
    reset();
    struct dcp_soc unknown = profile_t6041;
    unknown.os_fw = V_UNKNOWN;
    dcp6.soc = &unknown; os_firmware.version = V_UNKNOWN; expect(false);
    reset(); dcp6.soc = &profile_t6030; expect(false);
    assert(!fdt_setprop_string(dt, fdt_path_offset(dt, "/chosen"), profile_t6030.optin, "1"));
    expect(true); /* Existing deliberate experiments keep their opt-in. */
    assert(!fdt_setprop_string(dt, fdt_path_offset(dt, "/chosen"), profile_t6030.optin, "0"));
    expect(false);
    printf("PASS %u selector cases; every selection leaves the DT unchanged\n", cases);
    return 0;
}
'''
    unit = tmp_path / "selector.c"
    unit.write_text(harness)
    binary, dtb = tmp_path / "selector", tmp_path / "fixture.dtb"
    libfdt = repo / "src/libfdt"
    files = ("fdt", "fdt_ro", "fdt_rw", "fdt_wip", "fdt_sw", "fdt_empty_tree", "fdt_strerror", "fdt_addresses")
    subprocess.run([os.environ.get("CC", "cc"), "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                    "-O1", "-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=undefined",
                    "-fno-omit-frame-pointer", "-I" + str(libfdt), str(unit),
                    *(str(libfdt / (name + ".c")) for name in files), "-o", str(binary)], check=True)
    subprocess.run([os.environ.get("DTC", "dtc"), "-q", "-I", "dts", "-O", "dtb", "-o", str(dtb),
                    str(repo / "tests/data/t6041-handoff-selection.dts")], check=True)
    result = subprocess.run([str(binary), str(dtb)], capture_output=True, text=True, check=True)
    assert "PASS 11 selector cases" in result.stdout
