"""Keep native SMP reset-vector ownership out of host chainloading."""

import ast
from pathlib import Path
from types import SimpleNamespace


def test_cold_secondary_reset_vector_ownership():
    path = Path(__file__).resolve().parents[2] / "proxyclient/tools/chainload.py"
    module = ast.parse(path.read_text())
    block = next(node for node in module.body if isinstance(node, ast.If)
                 and isinstance(node.test, ast.Compare)
                 and isinstance(node.test.left, ast.Name)
                 and node.test.left.id == "rvbar")
    code = compile(ast.Module(body=[block], type_ignores=[]), str(path), "exec")
    for chip in (0x8142, 0x8152, 0x8103):
        reads, writes = [], []

        def read64(address):
            reads.append(address)
            return int(address == 0x3000)

        def write64(address, value):
            writes.append((address, value))

        cpus = [SimpleNamespace(name=f"cpu{i}", state="running" if i == 0 else "stopped",
                                cpu_impl_reg=(0x1000 * (i + 1), 8)) for i in range(3)]
        proxy = SimpleNamespace(read64=read64, write64=write64)
        utils = SimpleNamespace(base=0, adt={"cpus": cpus, "/chosen": SimpleNamespace(chip_id=chip)})
        exec(code, {"u": utils, "p": proxy, "rvbar": 0x10000})
        if chip in (0x8142, 0x8152):
            assert reads == [] and writes == []
        else:
            assert reads == [0x2000, 0x3000]
            assert writes == [(0x2000, 0x10000)]
