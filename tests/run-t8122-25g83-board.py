#!/usr/bin/env python3
"""Check native25 Air board selection and explicit J615 intent with real FDTs."""
import argparse
from pathlib import Path
import resource
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(directory, ref=None, mutant=False):
    for name in ['payload_profile.h', 't8122_25g83.h']:
        if ref:
            text = subprocess.check_output(['git', 'show', ref + ':src/' + name], cwd=ROOT, text=True)
        else:
            text = (ROOT / 'src' / name).read_text()
        if mutant and name == 't8122_25g83.h':
            text = text.replace('j613 == j615', '(j613 == j615 && false)')
        (directory / name).write_text(text)
    binary = directory / 'control'
    subprocess.run(['clang', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-g', '-I' + str(directory), '-I' + str(ROOT / 'src'),
                    str(ROOT / 'tests/payload_profile_host.c'),
                    *[str(ROOT / 'src/libfdt' / (name + '.c')) for name in
                      ['fdt', 'fdt_ro', 'fdt_rw', 'fdt_wip', 'fdt_sw', 'fdt_empty_tree', 'fdt_addresses']],
                    '-o', str(binary)], check=True)
    return subprocess.run([str(binary)], capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-ref')
    args = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with tempfile.TemporaryDirectory(prefix='t8122-25g83-board-') as name:
        directory = Path(name)
        result = run(directory)
        if result.returncode:
            raise SystemExit(result.stderr)
        print('PASS actual board/rank helpers with real FDT: J613, J615 intent, wrong board/firmware, ambiguous identity and malformed switch')
        if args.old_ref:
            result = run(directory, ref=args.old_ref)
            if not result.returncode or 't8122_25g83_board' not in result.stderr:
                raise SystemExit('old board helper did not fail ambiguous identity control')
            print('PASS old board helper fails the ambiguous-identity control')
        result = run(directory, mutant=True)
        if not result.returncode or 't8122_25g83_board' not in result.stderr:
            raise SystemExit('disabled uniqueness guard did not fail ambiguous identity control')
        print('PASS disabled uniqueness guard fails ambiguous-identity control')


if __name__ == '__main__':
    main()
