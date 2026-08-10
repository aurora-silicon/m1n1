// Decompile the stripped T8142 SPTM functions that enforce NVMe queue admission.
//@category AuroraSilicon

import java.io.FileWriter;
import java.util.Map;
import java.util.TreeMap;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionManager;
import ghidra.program.model.symbol.Reference;


public class ExportSptmNvme extends GhidraScript {
    private static final String OUTPUT = "/tmp/sptm-t8142-nvme-decomp.txt";

    private static final long[] SEED_REFS = {
        0xfffffff0270c6aecL, 0xfffffff0270c6b88L,
        0xfffffff0270c6e0cL, 0xfffffff0270c6ea8L,
        0xfffffff0270c7010L, 0xfffffff0270c706cL, 0xfffffff0270c70d4L,
        0xfffffff0270c73f8L, 0xfffffff0270c749cL, 0xfffffff0270c7544L,
    };

    private static final long[] NVME_STRINGS = {
        0xfffffff027008535L, 0xfffffff027008597L,
        0xfffffff027008675L, 0xfffffff027008703L,
        0xfffffff02700878bL, 0xfffffff0270087f4L,
        0xfffffff027008829L, 0xfffffff0270088afL,
    };

    private void addContainingFunction(Map<Long, Function> functions,
                                       FunctionManager manager, Address address) {
        Function function = manager.getFunctionContaining(address);
        if (function != null) {
            functions.put(function.getEntryPoint().getOffset(), function);
        }
    }

    @Override
    public void run() throws Exception {
        FunctionManager manager = currentProgram.getFunctionManager();
        Map<Long, Function> functions = new TreeMap<>();

        for (long seed : SEED_REFS) {
            addContainingFunction(functions, manager, toAddr(seed));
        }

        for (long stringAddress : NVME_STRINGS) {
            for (Reference reference : getReferencesTo(toAddr(stringAddress))) {
                addContainingFunction(functions, manager, reference.getFromAddress());
            }
        }

        DecompInterface decompiler = new DecompInterface();
        decompiler.openProgram(currentProgram);

        StringBuilder output = new StringBuilder();
        output.append("T8142 SPTM NVME HANDLERS\n");
        output.append("program=").append(currentProgram.getName())
              .append(" image_base=").append(currentProgram.getImageBase()).append("\n\n");

        for (Function function : functions.values()) {
            output.append("==============================================================================\n");
            output.append("FUNCTION ").append(function.getName())
                  .append(" entry=").append(function.getEntryPoint())
                  .append(" body=").append(function.getBody()).append("\n");

            DecompileResults result = decompiler.decompileFunction(function, 120, monitor);
            if (result.decompileCompleted() && result.getDecompiledFunction() != null) {
                output.append(result.getDecompiledFunction().getC());
            } else {
                output.append("DECOMPILATION FAILED: ").append(result.getErrorMessage()).append("\n");
            }
            output.append("\n");
        }

        decompiler.dispose();

        try (FileWriter writer = new FileWriter(OUTPUT)) {
            writer.write(output.toString());
        }

        println("Aurora SPTM export: " + functions.size() + " functions -> " + OUTPUT);
    }
}
