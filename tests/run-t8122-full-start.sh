#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
test_dir=$(mktemp -d)
trap 'rm -rf "$test_dir"' EXIT HUP INT TERM
# Compile the actual full-start callers; only hardware and unchanged ADT/calibration helpers
# are supplied by the host fixture. Copy complete definitions without changing their bodies.
python3 - "$test_dir/production.inc" <<'PY'
import re, sys
from pathlib import Path
source = Path('src/kboot_gpu.c').read_text()
def definition(pattern):
    match = re.search(pattern, source, re.M)
    assert match, pattern
    start = match.start()
    brace = source.index('{', match.end() - 1)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    if source[end:end+1] == ';':
        end += 1
    return source[start:end] + '\n'
structs = ['perf_state', 'g15_soc', 'g15_region', 'g15_fw_seg', 'g15_aux']
prelude = ''.join(definition(r'^struct ' + name + r' \{') for name in structs)
prelude += definition(r'^static const struct g15_region g15_region_names\[')
functions = ['g15_dt_reg_by_name', 't8122_identity_read', 'g15_read_hw',
             'g15_optin', 'dt_set_gpu_g15_inner', 'g15_disable', 'dt_set_gpu_g15']
bodies = ''.join(definition(r'^static (?:int|bool|void) ' + name + r'\([^;]+?\)\n\{')
                 for name in functions)
Path(sys.argv[1]).write_text(prelude + '\n/* FIXTURE HELPERS */\n' + bodies)
PY
# Place mocks after the source's struct definitions and before its unchanged functions.
python3 - "$test_dir/production.inc" "$test_dir/full-start.c" <<'PY'
from pathlib import Path
import sys
production = Path(sys.argv[1]).read_text()
fixture = Path('tests/t8122_full_start.c').read_text()
prelude, bodies = production.split('/* FIXTURE HELPERS */')
Path(sys.argv[2]).write_text(fixture.replace('/* SOURCE STRUCTS */', prelude)
                          .replace('/* SOURCE FUNCTIONS */', bodies))
PY
${CC:-cc} -std=gnu11 -D_GNU_SOURCE -O1 -g -Wall -Wextra -Werror \
    -Wno-unused-parameter -Wno-sign-compare -Wno-format \
    -fsanitize=address,undefined -fno-omit-frame-pointer \
    -Isrc "$test_dir/full-start.c" src/kboot_gpu_t8122_power.c \
    src/libfdt/fdt.c src/libfdt/fdt_ro.c src/libfdt/fdt_rw.c \
    src/libfdt/fdt_wip.c src/libfdt/fdt_sw.c src/libfdt/fdt_empty_tree.c \
    src/libfdt/fdt_addresses.c -o "$test_dir/full-start"
"$test_dir/full-start"
