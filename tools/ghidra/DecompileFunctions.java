// DecompileFunctions.java - decompile selected functions by address.
//@category AppleSilicon
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileOptions;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionManager;

import java.io.PrintWriter;

public class DecompileFunctions extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 2) {
            printerr("usage: DecompileFunctions <output-file> <address> [address ...]");
            return;
        }

        DecompInterface decompiler = new DecompInterface();
        decompiler.setOptions(new DecompileOptions());
        decompiler.openProgram(currentProgram);
        FunctionManager functions = currentProgram.getFunctionManager();

        try (PrintWriter out = new PrintWriter(args[0])) {
            out.println("// program: " + currentProgram.getName());
            for (int i = 1; i < args.length; i++) {
                Address address = toAddr(args[i]);
                Function function = functions.getFunctionAt(address);
                if (function == null)
                    function = functions.getFunctionContaining(address);

                out.println("\n// ============================================================");
                out.println("// requested: " + address);
                if (function == null) {
                    out.println("// no function found");
                    continue;
                }

                out.println("// function: " + function.getName(true));
                out.println("// entry: " + function.getEntryPoint());
                DecompileResults result = decompiler.decompileFunction(function, 180, monitor);
                if (result != null && result.decompileCompleted())
                    out.println(result.getDecompiledFunction().getC());
                else
                    out.println("// decompile failed: " +
                                (result == null ? "no result" : result.getErrorMessage()));
            }
        }

        decompiler.dispose();
        println("DecompileFunctions: wrote " + args[0]);
    }
}
