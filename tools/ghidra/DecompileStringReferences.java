// DecompileStringReferences.java - find strings and decompile direct referrers.
//@category AppleSilicon
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileOptions;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Listing;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.symbol.Reference;

import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.Set;

public class DecompileStringReferences extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 2) {
            printerr("usage: DecompileStringReferences <output-file> <string> [string ...]");
            return;
        }

        DecompInterface decompiler = new DecompInterface();
        decompiler.setOptions(new DecompileOptions());
        decompiler.openProgram(currentProgram);
        Memory memory = currentProgram.getMemory();
        Listing listing = currentProgram.getListing();

        try (PrintWriter out = new PrintWriter(args[0])) {
            out.println("// program: " + currentProgram.getName());
            for (int i = 1; i < args.length; i++) {
                byte[] needle = (args[i] + "\0").getBytes(StandardCharsets.US_ASCII);
                Address cursor = memory.getMinAddress();
                Set<Address> seenFunctions = new HashSet<>();
                out.println("\n// string: " + args[i]);

                while (cursor != null) {
                    Address hit = memory.findBytes(cursor, needle, null, true, monitor);
                    if (hit == null)
                        break;
                    out.println("// hit: " + hit);
                    Data data = listing.getDataAt(hit);
                    if (data != null)
                        out.println("// data: " + data);

                    for (Reference ref : getReferencesTo(hit)) {
                        Function function = getFunctionContaining(ref.getFromAddress());
                        out.println("// reference: " + ref.getFromAddress() +
                                    (function == null ? "" : " in " + function.getName(true)));
                        if (function == null || !seenFunctions.add(function.getEntryPoint()))
                            continue;
                        DecompileResults result = decompiler.decompileFunction(function, 180, monitor);
                        out.println("// function: " + function.getName(true));
                        out.println("// entry: " + function.getEntryPoint());
                        if (result != null && result.decompileCompleted())
                            out.println(result.getDecompiledFunction().getC());
                        else
                            out.println("// decompile failed: " +
                                        (result == null ? "no result" : result.getErrorMessage()));
                    }
                    cursor = hit.next();
                }
            }
        }

        decompiler.dispose();
        println("DecompileStringReferences: wrote " + args[0]);
    }
}
