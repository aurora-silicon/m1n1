"""Native DPTX activation and HPD trial on the qualified ATC3 route."""
import hashlib
import json
import subprocess
import struct
import sys
import time
from pathlib import Path
from elftools.elf.elffile import ELFFile

ROOT = Path(__file__).resolve().parents[2]
import argparse, fcntl, tempfile, os
ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument('image', type=Path)
ap.add_argument('sha256')
ap.add_argument('--device', required=True)
ap.add_argument('--output', type=Path, required=True)
ap.add_argument('--nm', default='llvm-nm')
args = ap.parse_args()
args.image = args.image.resolve()
args.output.mkdir(parents=True, exist_ok=False)
os.chdir(args.output)
lock = open(Path(tempfile.gettempdir()) / ('m1n1-' + Path(args.device).name + '.lock'), 'a')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
sys.path.insert(0, str(ROOT / 'proxyclient'))
from m1n1.proxy import UartInterface, M1N1Proxy, REGION_RX_EL1
from m1n1.asm import ARMAsm
from m1n1.proxyutils import ProxyUtils

image = args.image
raw = image.read_bytes()
assert hashlib.sha256(raw).hexdigest() == args.sha256
elf = image.parent / 'm1n1-raw.elf'
with elf.open('rb') as stream:
    parsed = ELFFile(stream)
    assert min(s['p_vaddr'] for s in parsed.iter_segments() if s['p_type'] == 'PT_LOAD') == 0
    assert parsed.header['e_entry'] == 0x800
symbols = {}
for line in subprocess.check_output([args.nm, str(elf)], text=True).splitlines():
    parts = line.split()
    if len(parts) == 3:
        symbols[parts[2]] = int(parts[0], 16)
keep_active = False
f = UartInterface(args.device)
f.dev.timeout = 15
try:
    f.nop()
    p = M1N1Proxy(f)
    p.nop()
    base = p.get_base()
    assert p.read32(base+symbols['chip_id']) == 0x8152
    assert p.read32(base+symbols['board_id']) == 0x24
    assert not any(p.smp_is_alive(i) for i in range(12) if i != 6), 'Fresh boot required'
    u = ProxyUtils(p)
    assert u.adt['/chosen'].chip_id == 0x8152
    assert u.adt['/chosen'].board_id == 0x24
    assert u.adt['/'].model == 'Mac18,5'
    base = p.get_base()
    names = ['afk_epic_start_ep', 'afk_epic_start_interface', 'afk_epic_shutdown_ep', 'dcp_ib_set_power', 'display_start_dcp', 'display_shutdown', 'dcp_dpav_init', 'dcp_dptx_init',
             'dcp_dpav_shutdown', 'dcp_dptx_shutdown', 'dcp_work',
             'afk_epic_command', 'dcp_dptx_connect', 'dptx_phy_init', 'dptx_phy_deactivate', 'dcp_ib_get_hpd', 'dart_translate','display_configure','fb_set_active','fb_clear','fb_display_logo','fb_console_write']
    for name in names:
        off = symbols[name]
        assert f.readmem(base + off, 64) == raw[off:off + 64], name
    def call(name, *args):
        return p.call(base + symbols[name], *args)
    with elf.open('rb') as stream:
        dw = ELFFile(stream).get_dwarf_info()
        layouts = {}
        for cu in dw.iter_CUs():
            for d in cu.iter_DIEs():
                if d.tag == 'DW_TAG_structure_type' and d.attributes.get('DW_AT_name') and 'DW_AT_byte_size' in d.attributes:
                    name = d.attributes['DW_AT_name'].value.decode()
                    if name in ('dcp_dev','rtkit_dev','rtkit_buffer','afk_epic_service_ops','afk_epic_service'):
                        fields = {c.attributes['DW_AT_name'].value.decode(): c.attributes['DW_AT_data_member_location'].value
                                  for c in d.iter_children() if c.tag == 'DW_TAG_member'}
                        assert name not in layouts or layouts[name] == fields
                        layouts[name] = fields
    def snapshot(label):
        dcp = p.read64(base + symbols['dcp'])
        rtk = p.read64(dcp + layouts['dcp_dev']['rtkit'])
        for name in ('oslog_bfr', 'syslog_bfr'):
            obj = rtk + layouts['rtkit_dev'][name]
            addr = p.read64(obj + layouts['rtkit_buffer']['bfr'])
            size = p.read64(obj + layouts['rtkit_buffer']['sz'])
            if addr and size:
                assert 0 < size <= 0x40000
                # Firmware's carveout lies beyond the memory exposed to the OS.
                # Validate each page through DCP's live DART mapping instead.
                dva = p.read64(obj + layouts['rtkit_buffer']['dva'])
                dart = p.read64(dcp + layouts['dcp_dev']['dart_dcp'])
                for i in range(0,size,0x4000):
                    assert call('dart_translate',dart,(dva+i)&((1<<36)-1)) == addr+i
                data = b''.join(f.readmem(addr+i,min(0x4000,size-i)) for i in range(0,size,0x4000))
                Path(f'display-{label}-{name}.bin').write_bytes(data)
                print('LOG_CAPTURE',label,name,hex(addr),len(data),flush=True)
        print('ROUTE_STATUS',label,{hex(o):hex(p.read32(0x413074000+o))
              for o in (0,8,0xc,0x20,0x24,0x38,0x3c,0x44,0x48,0x4c,0x800,0x804,0x808,0x818,0x81c,0x82c)},flush=True)
    assert p.read64(base + symbols['iboot']) == 0
    ret = call('display_start_dcp')
    print('DCP_START', hex(ret), flush=True)
    assert ret == 0
    assert p.read64(base + symbols['iboot'])
    dpav = dptx = phy = device_ep = 0
    device_allocations = []
    device_channels = []
    try:
        dcp = p.read64(base + symbols['dcp'])
        dpav = call('dcp_dpav_init', dcp)
        print('DPAV_OPEN', bool(dpav), flush=True)
        assert dpav
        dptx = call('dcp_dptx_init', dcp, 2)
        print('DPTX_OPEN', bool(dptx), flush=True)
        assert dptx
        path = u.malloc(64)
        try:
            f.writemem(path,b'/arm-io/atc-phy3\0')
            phy = call('dptx_phy_init',path,0)
        finally:
            u.free(path)
        assert phy
        print('NATIVE_DPTX_PHY',hex(phy),flush=True)
        # Verified port[0] layout, unchanged from FULL-15.
        port = dptx + 24
        assert p.read8(port) == 1 and p.read32(port + 4) == 0
        service = p.read64(port + 8)
        assert service
        epic = p.read64(service + 16)
        channel = p.read32(service + 32)

        def command(label, number, body, status_offset, group=0):
            request = struct.pack('<HHIII48x', 0, group, number, len(body), 0x69706378) + body
            tx, rx, count = u.malloc(len(request)), u.malloc(len(request)), u.malloc(8)
            try:
                f.writemem(tx, request)
                f.writemem(rx, bytes(len(request)))
                f.writemem(count, struct.pack('<Q', len(request)))
                code = f'''mov x4, #{len(request)}
                           ldr x5, ={rx}
                           ldr x6, ={count}
                           ldr x16, ={base + symbols['afk_epic_command']}
                           br x16'''
                ret = u.exec(code, epic, channel, 0xc0, tx)
                size = p.read64(count)
                assert size <= len(request)
                response = f.readmem(rx, size)
                Path(f'display-{label}.bin').write_bytes(response)
                print(label, 'transport', hex(ret), 'bytes', size, 'response', response.hex(), flush=True)
                if ret:
                    raise RuntimeError(f'{label}: transport {ret:#x}')
                assert len(response) >= status_offset + 4
                assert struct.unpack_from('<HHIII', response) == (0, group, number, len(body), 0x69706378)
                status = struct.unpack_from('<I', response, status_offset)[0]
                print(label, 'firmware_status', hex(status), flush=True)
                return status
            finally:
                u.free(count)
                u.free(rx)
                u.free(tx)


        def device_command(devchannel, number, body, variable=False):
            request = struct.pack('<HHIII48x', 0, int(variable), number, len(body), 0x69706378) + body
            tx, rx, count = u.malloc(len(request)), u.malloc(len(request)), u.malloc(8)
            try:
                f.writemem(tx, request)
                f.writemem(rx, bytes(len(request)))
                f.writemem(count, struct.pack('<Q', len(request)))
                asm = f'''mov x4, #{len(request)}
                           ldr x5, ={rx}
                           ldr x6, ={count}
                           ldr x16, ={base + symbols['afk_epic_command']}
                           br x16'''
                ret = u.exec(asm, device_ep, devchannel, 0xc0, tx)
                size = p.read64(count)
                assert size <= len(request)
                response = f.readmem(rx, size)
                if ret:
                    return ret, b''
                assert size == len(request)
                assert response[:16] == request[:16]
                return 0, response[64:]
            finally:
                u.free(count)
                u.free(rx)
                u.free(tx)

        def open_device_diagnostics():
            global device_ep
            assert layouts['afk_epic_service_ops'] == {'name':0,'init':32,'call':40,'call_v2':48}
            records = u.malloc(16 + 8 * 128)
            callback = u.malloc(512)
            ops = u.malloc(3 * 56)
            device_allocations.extend([records, callback, ops])
            f.writemem(records, bytes(16+8*128))
            asm = f'''ldr x9, ={records}
                      ldr w10, [x9]
                      cmp w10, #8
                      b.hs 3f
                      add w11, w10, #1
                      str w11, [x9]
                      add x9, x9, #16
                      add x9, x9, x10, lsl #7
                      stp x0, x3, [x9]
                      add x9, x9, #16
                      mov x10, #0
                      cbz x1, 2f
                   1: ldrb w11, [x1, x10]
                      strb w11, [x9, x10]
                      add x10, x10, #1
                      cbz w11, 2f
                      cmp x10, #47
                      b.lo 1b
                   2: add x9, x9, #48
                      mov x10, #0
                      cbz x2, 3f
                   4: ldrb w11, [x2, x10]
                      strb w11, [x9, x10]
                      add x10, x10, #1
                      cbz w11, 3f
                      cmp x10, #47
                      b.lo 4b
                   3: ret'''
            code = ARMAsm(asm, callback).data
            assert len(code) <= 512
            f.writemem(callback,code)
            assert f.readmem(callback,len(code)) == code
            p.dc_cvau(callback,len(code))
            p.ic_ivau(callback,len(code))
            data=b''.join(struct.pack('<32sQQQ',name,callback | REGION_RX_EL1,0,0)
                          for name in (b'DCPDPDevice',b'DCPDPVirtualDevice')) + bytes(56)
            f.writemem(ops,data)
            afk=p.read64(dcp+layouts['dcp_dev']['afk'])
            device_ep=call('afk_epic_start_ep',afk,0x27,ops,1)
            print('DEVICE_EP',hex(device_ep),flush=True)
            assert device_ep
            asm=f'''mov x4, #0x4000
                    ldr x16, ={base+symbols['afk_epic_start_interface']}
                    br x16'''
            ret=u.exec(asm,device_ep,0,8,0x4000)
            count=p.read32(records)
            assert count <= 8
            print('DEVICE_INIT',hex(ret),'count',count,flush=True)
            for i in range(count):
                record=f.readmem(records+16+128*i,128)
                svc,unit=struct.unpack_from('<QQ',record)
                name=record[16:64].split(bytes(1))[0].decode()
                eclass=record[64:112].split(bytes(1))[0].decode()
                ch=p.read32(svc+layouts['afk_epic_service']['channel'])
                print('DEVICE_SERVICE',name,eclass,unit,ch,flush=True)
                if name == 'dcpdp-device-epic' and eclass == 'DCPDPDevice':
                    ret,response=device_command(ch,4,bytes(32))
                    status=struct.unpack_from('<I',response)[0] if response else None
                    print('DEVICE_OPEN',ch,hex(ret),status,flush=True)
                    assert ret==0 and status==0
                    device_channels.append(ch)
            assert len(device_channels)==1, 'Expected one physical DCP DP device'

        def read_dpcd(label):
            for ch in device_channels:
                for addr,size in ((0,16),(0x100,16),(0x200,16),(0x600,1)):
                    body=bytearray(64+size)
                    struct.pack_into('<I',body,0,addr)
                    struct.pack_into('<I',body,16,size)
                    struct.pack_into('<I',body,32,500)
                    ret,response=device_command(ch,6,bytes(body),True)
                    status=struct.unpack_from('<I',response,48)[0] if response else None
                    print('DPCD_READ',label,ch,hex(addr),'transport',hex(ret),
                          'status',hex(status) if status is not None else None,
                          'data',response[64:].hex(),flush=True)
                    Path(f'display-{label}-{addr:x}.bin').write_bytes(response)

        # connectTo and noconnect use the same command-11/body-32 message.
        # Native activation callback owns the scoped ATC3 hardware operations.
        try:
            status = call('dcp_dptx_connect', dptx, phy, 0, 0)
            print('NATIVE_CONNECT_AND_REQUEST_RESULT', hex(status), flush=True)
            try:
                assert status == 0, f'Connect/request failed {status:#x}'
                body = bytearray(32)
                struct.pack_into('<I',body,16,1)
                status = command('hpd-high',8,bytes(body),0x40,group=8)
                print('HPD_HIGH_RESULT',hex(status),flush=True)
                # Service firmware continuously on target; avoid host round-trip gaps.
                code = f'''stp x19, x20, [sp, #-32]!
                           stp x21, x30, [sp, #16]
                           mov x19, x0
                           ldr x0, =8000000
                           ldr x16, ={base + symbols['timeout_calculate']}
                           blr x16
                           mov x20, x0
                           ldr x21, ={base + symbols['dcp_work']}
                        1: mov x0, x19
                           blr x21
                           tbnz w0, #31, 2f
                           mrs x8, CNTPCT_EL0
                           cmp x8, x20
                           b.lo 1b
                           mov x0, #0
                        2: ldp x21, x30, [sp, #16]
                           ldp x19, x20, [sp], #32
                           ret'''
                ret = u.exec(code, dcp)
                print('NATIVE_WORK_8S', hex(ret), flush=True)
                snapshot('after-training')
                open_device_diagnostics()
                read_dpcd('after-training')
                counts=u.malloc(8)
                try:
                    f.writemem(counts,bytes(8))
                    ret=call('dcp_ib_get_hpd',p.read64(base+symbols['iboot']),counts,counts+4)
                    modes = struct.unpack('<II',f.readmem(counts,8))
                    print('IBOOT_HPD_MODES',hex(ret),modes,flush=True)
                    assert ret == 1 and all(modes), 'Display modes unavailable'
                    ret = call('dcp_ib_set_power',p.read64(base+symbols['iboot']),1)
                    print('PREPOWER',hex(ret),flush=True)
                    read_dpcd('after-power')
                    assert ret == 0
                    ret = u.exec(code, dcp)
                    print('POST_POWER_WORK_8S',hex(ret),flush=True)
                    config = u.malloc(32)
                    try:
                        f.writemem(config,b'1280x720@60\0')
                        ret = call('display_configure',config)
                    finally:
                        u.free(config)
                    print('DISPLAY_CONFIGURE',hex(ret),flush=True)
                    snapshot('after-configure')
                    read_dpcd('after-configure')
                    assert ret == 1
                    keep_active = True
                    state = {'base':hex(base),'dcp':hex(dcp),'dpav':hex(dpav),
                             'dptx':hex(dptx),'phy':hex(phy),'image_sha256':args.sha256,
                             'modes':modes,'visible_confirmed':False,
                             'host_heap_base':u.heap_base,'host_heap_top':u.heap_top}
                    Path('display-state.json').write_text(json.dumps(state,indent=2))
                    call('fb_set_active',1)
                    call('fb_clear',0)
                    call('fb_display_logo')
                    banner = b'\n\nJ873g M6 - public m1n1\nUSB-C DisplayPort framebuffer test\n'
                    msg = u.malloc(len(banner))
                    try:
                        f.writemem(msg,banner)
                        print('CONSOLE_WRITE',call('fb_console_write',msg,len(banner)),flush=True)
                    finally:
                        u.free(msg)
                    print('FRAMEBUFFER_GEOMETRY',struct.unpack('<5I',f.readmem(base+symbols['fb']+16,20)),flush=True)
                finally:
                    u.free(counts)
            finally:
                if not keep_active:
                    command('hpd-low',8,bytes(32),0x40,group=8)
                    status = command('release-display',7,bytes(16),0x40,group=8)
                    print('DISPLAY_RELEASE_RESULT',hex(status),flush=True)
        finally:
            if not keep_active:
                status = command('disconnect-unit0',11,bytes(32),0x50)
                print('ROUTE_DISCONNECT_RESULT',hex(status),flush=True)
        print('ROUTE_TRIAL_FINISHED', flush=True)
    finally:
        if not keep_active:
            if device_ep:
                for ch in device_channels:
                    ret,response=device_command(ch,5,bytes(16))
                    print('DEVICE_CLOSE',ch,hex(ret),response.hex(),flush=True)
                call('afk_epic_shutdown_ep',device_ep)
                device_ep=0
            for addr in reversed(device_allocations):
                u.free(addr)
            if dptx:
                call('dcp_dptx_shutdown', dptx)
            if dpav:
                call('dcp_dpav_shutdown', dpav)
            call('display_shutdown', 0)
            if phy:
                assert call('dptx_phy_deactivate',phy)==0
                # Read the exact current native struct layout from DWARF.
                with elf.open('rb') as stream:
                    dw=ELFFile(stream).get_dwarf_info()
                    ds=[d for cu in dw.iter_CUs() for d in cu.iter_DIEs() if d.tag=='DW_TAG_structure_type' and d.attributes.get('DW_AT_name') and d.attributes['DW_AT_name'].value==b'dptx_phy' and 'DW_AT_byte_size' in d.attributes]
                    assert len(ds)==1
                    off=next(d.attributes['DW_AT_data_member_location'].value for d in ds[0].iter_children() if d.tag=='DW_TAG_member' and d.attributes['DW_AT_name'].value==b'atc')
                p.free(p.read64(phy+off))
                p.free(phy)
            assert p.read32(0x3082901c0)==0x300
            assert p.read32(0x3082900c0)==0x244
            assert p.read32(0x3082900e0)==0x244
            assert p.read32(0x303a13a80)&7==6
    p.nop()
    print('PROXY_VERIFIED', flush=True)
    print('DISPLAY_LEFT_ACTIVE',keep_active,flush=True)
finally:
    f.dev.close()



