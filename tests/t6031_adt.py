#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build a binary ADT, in the layout m1n1's ADT reader (rust/src/adt.rs) walks, from the
allowlist an aurora-adt-extract report prints (adt-allowlist.txt), for tests/t6031_host.c.

    t6031_adt.py <adt-allowlist.txt> <out.adt>

Every node and property of the allowlist is kept as it is. The allowlist leaves out a few things
the T6031 handoff reads; those are added with synthetic values, each listed on stdout with
"synthetic:". This Mac's values stay in the allowlist file; nothing is written into the tree.
"""
import re
import struct
import sys

PROP = re.compile(r'^  (\S+) \[(\d+)\] = (.*)$')
STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')

# Synthetic values for what the allowlist does not carry
DRAM_BASE = 0x100_0000_0000
DRAM_SIZE = 0xC_0000_0000          # 48 GiB, as this Mac has
# One image for dcp and dcpext0-3 (the ADT gives them one text segment), one for gfx and pmp
SYNTH_UUID = {
    "gfx-asc/iop-gfx-nub0": "5E5E5E5E-0000-4000-8000-000000000001",
    "dcp/iop-dcp-nub": "5E5E5E5E-0000-4000-8000-000000000002",
    "dcpext0/iop-dcpext0-nub": "5E5E5E5E-0000-4000-8000-000000000002",
    "dcpext1/iop-dcpext1-nub": "5E5E5E5E-0000-4000-8000-000000000002",
    "dcpext2/iop-dcpext2-nub": "5E5E5E5E-0000-4000-8000-000000000002",
    "dcpext3/iop-dcpext3-nub": "5E5E5E5E-0000-4000-8000-000000000002",
    "pmp/iop-pmp-nub": "5E5E5E5E-0000-4000-8000-000000000003",
}
# carveout-memory-map region ids for the dcp segments other than __OS_LOG (T6030's numbering)
SYNTH_REGION_IDS = [49, 50, None, 57, 94, 95, 14, 157]


def value_bytes(size, text, where):
    text = text.strip()
    if text.startswith("<"):
        words = text.strip("<>").split()
        data = struct.pack("<%dI" % len(words), *(int(w, 16) for w in words))
    elif text.startswith('"'):
        parts = [s.encode() for s in STRING.findall(text)]
        data = b"\0".join(parts) + b"\0"
        if len(data) < size:
            data += b"\0" * (size - len(data))
    else:
        raise SystemExit("%s: cannot read value %r" % (where, text[:40]))
    if len(data) != size:
        raise SystemExit("%s: %d bytes, the allowlist says %d" % (where, len(data), size))
    return data


def parse(path):
    nodes, order, cur, pending = {}, [], None, None

    def flush():
        nonlocal pending
        if pending:
            name, size, text = pending
            nodes[cur].append((name, value_bytes(size, text, "%s %s" % (cur, name))))
        pending = None

    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if line.startswith("#") or not line.strip():
            continue
        if line.startswith("/"):
            flush()
            cur = line.strip()
            nodes.setdefault(cur, [])
            order.append(cur)
            continue
        m = PROP.match(line)
        if m:
            flush()
            pending = [m.group(1), int(m.group(2)), m.group(3)]
        elif pending and line.startswith("      "):
            pending[2] += " " + line.strip()
        else:
            raise SystemExit("cannot read line %r" % line[:60])
    flush()
    return nodes, order


def getprop(nodes, path, name):
    for n, v in nodes.get(path, []):
        if n == name:
            return v
    return None


def setprop(nodes, order, path, name, data, why):
    if path not in nodes:
        nodes[path] = []
        order.append(path)
    nodes[path] = [(n, v) for n, v in nodes[path] if n != name] + [(name, data)]
    print("synthetic: %s %s (%s)" % (path, name, why))


def synthesize(nodes, order):
    u64 = lambda v: struct.pack("<Q", v)
    u32 = lambda v: struct.pack("<I", v)
    setprop(nodes, order, "/chosen", "name", b"chosen\0", "not in the allowlist")
    setprop(nodes, order, "/chosen", "dram-base", u64(DRAM_BASE), "not in the allowlist")
    setprop(nodes, order, "/chosen", "dram-size", u64(DRAM_SIZE), "48 GiB, as this Mac has")
    setprop(nodes, order, "/chosen", "board-id", u32(0x5A), "placeholder")
    setprop(nodes, order, "/chosen", "dram-vendor-id", u32(0x5B), "placeholder")
    for nub, uuid in SYNTH_UUID.items():
        path = "/arm-io/" + nub
        if path in nodes:
            setprop(nodes, order, path, "uuid", uuid.encode() + b"\0",
                    "dropped by the collector; placeholder")

    # disp0's clock ids index /arm-io clock-frequencies from 256; 712 MHz is T6030's fixed clk_disp0
    freqs = [0] * 256
    for i, cid in enumerate(struct.unpack("<2I", getprop(nodes, "/arm-io/disp0", "clock-ids"))):
        freqs[cid - 256] = 712000000 if i == 0 else 0
    setprop(nodes, order, "/arm-io", "clock-frequencies", struct.pack("<256I", *freqs),
            "not in the allowlist; placeholder rates")
    setprop(nodes, order, "/arm-io/mcc", "name", b"mcc\0", "not in the allowlist")
    setprop(nodes, order, "/arm-io/mcc", "reg",
            u64(0x2_8000_0000 - 0x2_0000_0000) + u64(0x200000) +
            u64(0x2_8040_0000 - 0x2_0000_0000) + u64(0x200000),
            "not in the allowlist; placeholder windows")

    seg = getprop(nodes, "/arm-io/dcp/iop-dcp-nub", "segment-ranges")
    cmap = "/chosen/carveout-memory-map"
    setprop(nodes, order, cmap, "name", b"carveout-memory-map\0", "not in the allowlist")
    for i in range(len(seg) // 32):
        phys, iova, remap, size, flags = struct.unpack_from("<QQQII", seg, 32 * i)
        rid = SYNTH_REGION_IDS[i] if i < len(SYNTH_REGION_IDS) else 200 + i
        if flags == 0xA or rid is None:
            continue
        setprop(nodes, order, cmap, "region-id-%d" % rid,
                u64(phys) + u64((size + 0x3FFF) & ~0x3FFF),
                "dcp segment %d as one carveout, T6030's region number" % i)
        if i == 6:
            setprop(nodes, order, "/vram", "name", b"vram\0", "not in the allowlist")
            setprop(nodes, order, "/vram", "reg", u64(phys) + u64(size),
                    "dcp segment 6, which holds the boot framebuffer")


def build(nodes, order):
    children = {p: [] for p in order}
    for p in order:
        if p == "/":
            continue
        parent = p.rsplit("/", 1)[0] or "/"
        if parent not in children:
            raise SystemExit("%s has no parent node %s in the allowlist" % (p, parent))
        children[parent].append(p)

    def node(path):
        props = list(nodes[path])
        if path != "/" and not any(n == "name" for n, _ in props):
            props.append(("name", path.rsplit("/", 1)[1].encode() + b"\0"))
        out = struct.pack("<II", len(props), len(children[path]))
        for name, data in props:
            if len(name) >= 32:
                raise SystemExit("property name %s too long" % name)
            out += name.encode().ljust(32, b"\0") + struct.pack("<I", len(data))
            out += data + b"\0" * (-len(data) % 4)
        for child in children[path]:
            out += node(child)
        return out

    return node("/")


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    nodes, order = parse(sys.argv[1])
    if "/" not in nodes:
        raise SystemExit("no root node")
    synthesize(nodes, order)
    blob = build(nodes, order)
    with open(sys.argv[2], "wb") as f:
        f.write(blob)
    print("ADT: %d nodes, %d bytes" % (len(order), len(blob)))


if __name__ == "__main__":
    main()
