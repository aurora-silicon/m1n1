"""A rejected cold probe must not overwrite a live secondary's scratch code."""
from pathlib import Path
from types import SimpleNamespace
import textwrap

import pytest


def guard_context(live):
    source = (Path(__file__).resolve().parents[2] /
              'proxyclient/experiments/t6040_amp.py').read_text()
    prologue = textwrap.dedent(source.split('try:\n', 1)[1].split(
        '    p.smp_set_wfe_mode(True)', 1)[0])
    nodes = [SimpleNamespace(cpu_id=cpu, reg=reg)
             for cpu, reg in ((0, 0), (4, 0x100), (5, 0x101), (14, 0x204))]
    reads = []
    writes = []

    def is_alive(cpu):
        reads.append(cpu)
        return cpu in live

    def mrs(reg):
        writes.append(reg)  # ProxyUtils.mrs uses the shared scratch-code buffer.
        return 0x100

    context = {
        'p': SimpleNamespace(get_chipid=lambda: 0x6040, smp_is_alive=is_alive),
        'u': SimpleNamespace(adt={'/chosen': SimpleNamespace(board_id=6), '/cpus': nodes},
                             mrs=mrs),
    }
    return prologue, context, reads, writes


@pytest.mark.parametrize('cpu', [0, 5, 14])
def test_live_secondary_rejected_before_scratch_write(cpu):
    prologue, context, reads, writes = guard_context({cpu})
    with pytest.raises(ValueError, match='reboot first'):
        exec(prologue, context)
    assert cpu in reads
    assert not writes


def test_cold_guard_identifies_boot_cpu_after_checking_all_cpus():
    prologue, context, reads, writes = guard_context(set())
    exec(prologue, context)
    assert reads == [0, 4, 5, 14]
    assert writes == ['MPIDR_EL1']
    assert [node.cpu_id for node in context['nodes']] == [0, 5, 14]
