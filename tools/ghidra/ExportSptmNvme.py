# Decompile the stripped T8142 SPTM functions that enforce NVMe queue admission.
#@category AuroraSilicon

from ghidra.app.decompiler import DecompInterface
from ghidra.util.task import ConsoleTaskMonitor


OUTPUT = "/tmp/sptm-t8142-nvme-decomp.txt"

# References to SPTM's NVMe assertion/function-name strings, recovered from the
# matching 25G76 IPSW.  Function containment is more reliable than relying on
# stripped symbol names.
SEED_REFS = [
    0xfffffff0270c6aec,  # sptm_nvme_bar_iocq_reg
    0xfffffff0270c6b88,
    0xfffffff0270c6e0c,  # sptm_nvme_bar_iosq_reg
    0xfffffff0270c6ea8,
    0xfffffff0270c7010,  # sptm_nvme_bar_ioqa_reg
    0xfffffff0270c706c,
    0xfffffff0270c70d4,
    0xfffffff0270c73f8,  # sptm_nvme_bar_admin_queue_regs
    0xfffffff0270c749c,
    0xfffffff0270c7544,
]

NVME_STRINGS = [
    0xfffffff027008535,  # nvme_bootstrap
    0xfffffff027008597,  # sptm_nvme_map_pages
    0xfffffff027008675,  # sptm_nvme_unmap_pages
    0xfffffff027008703,  # sptm_nvme_bar_admin_queue_regs
    0xfffffff02700878b,  # sptm_nvme_bar_ioqa_reg
    0xfffffff0270087f4,  # sptm_nvme_bar_iosq_reg
    0xfffffff027008829,  # sptm_nvme_bar_iocq_reg
    0xfffffff0270088af,  # validate_nvme_call_allowed
]


def containing_function(address):
    return currentProgram.getFunctionManager().getFunctionContaining(toAddr(address))


def add_function(functions, function):
    if function is not None:
        functions[function.getEntryPoint().getOffset()] = function


def main():
    functions = {}

    for address in SEED_REFS:
        add_function(functions, containing_function(address))

    # Also consume any references Ghidra recovered to the named assertion
    # strings. This catches handler functions beyond the known seed range.
    for string_address in NVME_STRINGS:
        for reference in getReferencesTo(toAddr(string_address)):
            add_function(functions, containing_function(reference.getFromAddress().getOffset()))

    decompiler = DecompInterface()
    decompiler.openProgram(currentProgram)
    monitor = ConsoleTaskMonitor()

    output = []
    output.append("T8142 SPTM NVME HANDLERS\n")
    output.append("program=%s image_base=%s\n\n" %
                  (currentProgram.getName(), currentProgram.getImageBase()))

    for entry in sorted(functions.keys()):
        function = functions[entry]
        output.append("=" * 78 + "\n")
        output.append("FUNCTION %s entry=%s body=%s\n" %
                      (function.getName(), function.getEntryPoint(), function.getBody()))

        result = decompiler.decompileFunction(function, 120, monitor)
        if result.decompileCompleted() and result.getDecompiledFunction() is not None:
            output.append(result.getDecompiledFunction().getC())
        else:
            output.append("DECOMPILATION FAILED: %s\n" % result.getErrorMessage())
        output.append("\n")

    decompiler.dispose()

    handle = open(OUTPUT, "w")
    try:
        handle.write("".join(output))
    finally:
        handle.close()

    print("Aurora SPTM export: %d functions -> %s" % (len(functions), OUTPUT))


main()
