// DumpVtableSlots.java - resolve selected C++ virtual table slots.
//@category AppleSilicon
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.Reference;

import java.io.PrintWriter;

public class DumpVtableSlots extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 3) {
            printerr("usage: DumpVtableSlots <output-file> <vtable-address> <slot-offset> [...]");
            return;
        }

        Memory memory = currentProgram.getMemory();
        Address vtable = toAddr(args[1]);

        try (PrintWriter out = new PrintWriter(args[0])) {
            out.println("program=" + currentProgram.getName());
            out.println("vtable=" + vtable);

            for (int i = 2; i < args.length; i++) {
                long offset = Long.decode(args[i]);

                // Apple C++ objects point 16 bytes past the Itanium ABI vtable
                // symbol (offset-to-top and RTTI occupy the first two entries).
                Address slot = vtable.add(16 + offset);
                long raw = memory.getLong(slot);
                Address target = toAddr(raw);
                Function function = getFunctionAt(target);
                if (function == null)
                    function = getFunctionContaining(target);
                Symbol symbol = getSymbolAt(target);

                out.printf("offset=%#x slot=%s raw=%#x target=%s function=%s symbol=%s%n",
                           offset, slot, raw, target,
                           function == null ? "-" : function.getName(true),
                           symbol == null ? "-" : symbol.getName(true));

                for (Reference reference : getReferencesFrom(slot)) {
                    Address referenceTarget = reference.getToAddress();
                    Function referenceFunction = getFunctionAt(referenceTarget);
                    if (referenceFunction == null)
                        referenceFunction = getFunctionContaining(referenceTarget);
                    Symbol referenceSymbol = getSymbolAt(referenceTarget);
                    out.printf("  ref=%s type=%s function=%s symbol=%s%n",
                               referenceTarget, reference.getReferenceType(),
                               referenceFunction == null ? "-" : referenceFunction.getName(true),
                               referenceSymbol == null ? "-" : referenceSymbol.getName(true));
                }
            }
        }

        println("DumpVtableSlots: wrote " + args[0]);
    }
}
