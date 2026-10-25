# SPDX-License-Identifier: MIT
"""Extract the J700 display dependency subgraph from a composed DTB into a DTS fixture.

usage: python3 extract_j700_display.py <full.dtb> <out.dts>
Kept: the internal and external DCP controllers (with children), the display
subsystem, the Type-C controller that links to the external route, and the
transitive closure of their iommus/mboxes/power-domains providers. Ancestors
keep only cell sizes, ranges and compatible. Aliases keep display entries.
"""
import sys, libfdt
blob = open(sys.argv[1], 'rb').read()
fdt = libfdt.Fdt(blob)
def path(off): return fdt.get_path(off)
def props(off):
    out = []; p = fdt.first_property_offset(off, libfdt.QUIET_NOTFOUND)
    while p >= 0:
        pr = fdt.get_property_by_offset(p); out.append((pr.name, bytes(pr))); p = fdt.next_property_offset(p, libfdt.QUIET_NOTFOUND)
    return out
def prop(off, name):
    try: return bytes(fdt.getprop(off, name))
    except libfdt.FdtException: return None
def u32s(b): return [int.from_bytes(b[i:i+4], 'big') for i in range(0, len(b), 4)]
def by_phandle(ph): return fdt.node_offset_by_phandle(ph)
SEEDS = ['/soc/dcp@412e00000', '/soc/dcp@416e00000', '/display-subsystem']
keep_subtree = set(); keep = set()
work = [fdt.path_offset(s) for s in SEEDS]
# the Type-C controller linked from the external route endpoint
ep = fdt.path_offset('/soc/dcp@416e00000/typec-routes/route@0/port/endpoint')
remote = by_phandle(u32s(prop(ep, 'remote-endpoint'))[0])
o = remote
while path(o).count('/') > 1 and not prop(o, 'compatible'):
    o = fdt.parent_offset(o)
work.append(o)
CELLS = {'iommus': '#iommu-cells', 'mboxes': '#mbox-cells', 'power-domains': '#power-domain-cells'}
while work:
    off = work.pop()
    if path(off) in keep_subtree: continue
    keep_subtree.add(path(off))
    stack = [off]
    while stack:
        n = stack.pop(); keep.add(path(n))
        for name, cells in CELLS.items():
            v = prop(n, name)
            if not v: continue
            vals = u32s(v); i = 0
            while i < len(vals):
                prov = by_phandle(vals[i]); w = u32s(prop(prov, cells))[0]
                work.append(prov); i += 1 + w
        c = fdt.first_subnode(n, libfdt.QUIET_NOTFOUND)
        while c >= 0:
            stack.append(c); c = fdt.next_subnode(c, libfdt.QUIET_NOTFOUND)
anc = set()
for p in list(keep):
    parts = p.split('/')
    for i in range(1, len(parts)):
        anc.add('/'.join(parts[:i]) or '/')
def fmt_val(name, b):
    if len(b) == 0: return None
    if b[-1:] == b'\0' and all(32 <= c < 127 or c == 0 for c in b) and b[:1] != b'\0' and b'\0\0' not in b:
        return ', '.join('"%s"' % s.decode() for s in b[:-1].split(b'\0'))
    if len(b) % 4 == 0: return '<' + ' '.join('0x%x' % x for x in u32s(b)) + '>'
    return '[' + ' '.join('%02x' % x for x in b) + ']'
ANC_PROPS = {'#address-cells', '#size-cells', 'ranges', 'compatible', 'model'}
lines = []
def emit(off, depth):
    p = path(off); ind = '\t' * depth
    name = '/' if p == '/' else p.rsplit('/', 1)[1]
    lines.append(f'{ind}{name} {{')
    full = p in keep
    for k, v in props(off):
        if p == '/aliases':
            if not (k.startswith('dcp') or k.startswith('disp')): continue
        elif not full and k not in ANC_PROPS: continue
        f = fmt_val(k, v)
        lines.append(f'{ind}\t{k};' if f is None else f'{ind}\t{k} = {f};')
    c = fdt.first_subnode(off, libfdt.QUIET_NOTFOUND)
    while c >= 0:
        cp = path(c)
        if cp in keep or cp in anc or cp in ('/aliases', '/chosen', '/reserved-memory'):
            emit(c, depth + 1)
        c = fdt.next_subnode(c, libfdt.QUIET_NOTFOUND)
    lines.append(f'{ind}}};')
emit(0, 0)
lines[0] = '/ {'
out = open(sys.argv[2], 'w')
out.write('/dts-v1/;\n\n' + '\n'.join(lines) + '\n')
print('kept', len(keep), 'nodes;', 'seeds', SEEDS, 'typec', path(o))
