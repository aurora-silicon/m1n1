"""Public M6 candidate E2E; native allocations, pinned image, explicit device selection."""
import argparse, hashlib, json, statistics, struct, subprocess, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from m1n1.proxy import UartInterface, M1N1Proxy
from m1n1.asm import ARMAsm
ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument('action', choices=['start', 'cores', 'atomic', 'amp', 'clocks', 'status'])
ap.add_argument('image', type=Path)
ap.add_argument('sha256')
ap.add_argument('--device', required=True)
ap.add_argument('--receipt', required=True, type=Path)
ap.add_argument('--nm', default='llvm-nm')
args = ap.parse_args()
IMAGE = args.image
SHA = args.sha256
STATE = args.receipt
assert args.action != 'start' or not STATE.exists(), 'Use a fresh receipt after resetting'
RAW = IMAGE.read_bytes()
assert hashlib.sha256(RAW).hexdigest() == SHA
SYMS = {}
for line in subprocess.check_output([args.nm, str(IMAGE.with_name('m1n1-raw.elf'))], text=True).splitlines():
    q = line.split()
    if len(q) == 3: SYMS[q[2]] = int(q[0], 16)
# A shared advisory lock prevents two copies of this harness owning one port.
import fcntl, tempfile
lock = open(Path(tempfile.gettempdir()) / ('m1n1-' + Path(args.device).name + '.lock'), 'a')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
f = UartInterface(args.device); f.dev.timeout = 8
s = json.loads(STATE.read_text()) if STATE.exists() else {'sha256': SHA, 'results': []}
assert s['sha256'] == SHA
def save(): STATE.write_text(json.dumps(s, indent=2))
try:
    f.nop(); p = M1N1Proxy(f); p.nop(); base = p.get_base()
    assert s.get('base', base) == base
    s['base'] = base
    for n in ['smp_start_secondaries', 'smp_secondary_entry', 'fb_set_active']:
        off = SYMS[n]; assert f.readmem(base+off, 64) == RAW[off:off+64], n
    assert p.read32(base+SYMS['chip_id']) == 0x8152 and p.read32(base+SYMS['board_id']) == 0x24
    def call(name, *a): return p.call(base+SYMS[name], *a)
    def status():
        d = {n: p.read64(base+SYMS[n]) for n in ['smp_started_mask', 'smp_start_fail_mask']}
        d['boot_cpu'] = p.read32(base+SYMS['boot_cpu_idx'])
        d['alive'] = [i for i in range(12) if p.smp_is_alive(i)]
        if 'cpufreq_get_cluster_hz' in SYMS:
            d['clock_hz'] = [call('cpufreq_get_cluster_hz', c) for c in range(2)]
        return d
    if args.action == 'start':
        print('BEFORE_NATIVE_START', status(), flush=True)
        # Keep the framebuffer console inactive during concurrent native work.
        call('fb_set_active', 0)
        s['smp_framebuffer_console_disabled'] = True
        save()
        print('NATIVE_SMP_FRAMEBUFFER_CONSOLE_DISABLED', flush=True)
        p.smp_start_secondaries()
        v = status(); s['startup'] = v; save()
        print('AFTER_NATIVE_START', v, flush=True)
        assert v['smp_started_mask'] == 0xfbf and v['smp_start_fail_mask'] == 0
        assert v['alive'] == list(range(12)) and v['boot_cpu'] == 6
        p.smp_start_secondaries()
        assert status() == v, 'Repeated startup must preserve live state'
        print('STARTUP_AND_IDEMPOTENCE_PASS', flush=True)
    elif args.action == 'status': print(status(), flush=True)
    else:
        v = status()
        assert v['smp_started_mask'] == 0xfbf and v['smp_start_fail_mask'] == 0 and v['alive'] == list(range(12))
        if 'code' not in s:
            s['code'] = p.memalign(0x4000, 0x4000); s['data'] = p.memalign(0x4000, 0x4000)
            assert s['code'] and s['data']
            asm = ARMAsm((HERE/'t8152-cpu.S').read_text(), s['code'])
            f.writemem(s['code'], asm.data); p.dc_cvac(s['code'], len(asm.data)); p.ic_ivau(s['code'], len(asm.data))
            assert f.readmem(s['code'], len(asm.data)) == asm.data
            s['functions'] = {n: getattr(asm,n) for n in ['identity', 'math', 'ticks', 'frequency', 'work', 'atomic']}; save()
        def execute(cpu, name, *a):
            addr = s['functions'][name]
            return p.call(addr, *a) if cpu == 6 else p.smp_call_sync(cpu, addr, *a)
        def complete(cpu):
            deadline = time.monotonic()+6
            while p.read64(base+SYMS['spin_table']+cpu*64+16):
                if time.monotonic()>deadline: raise TimeoutError(('native dispatch',cpu))
            return p.smp_wait(cpu)
        data = s['data']; hz = execute(6,'frequency')
        if args.action == 'cores':
            records = []
            for cpu in range(12):
                execute(cpu,'identity',data)
                r = dict(zip(['mpidr','midr','el','sctlr','tcr','tpidr'], struct.unpack('<6Q',f.readmem(data,48))))
                assert r['el'] == 8 and r['sctlr']&0x1005 == 0x1005 and r['tpidr'] == cpu, (cpu,r)
                assert r['mpidr']&0xffff == ((cpu//6)<<8)|(cpu%6)
                for i in range(32): assert execute(cpu,'math',i,7,13,11) == ((i*7+13)^11)
                records.append({'cpu':cpu,**r}); print('CORE_PASS',cpu,{k:hex(x) for k,x in r.items()},flush=True)
            s['cores'] = records
        elif args.action == 'clocks':
            measurements=[]
            for cluster,cpu,reg,trial in [(0,0,0x210e20020,3),(1,6,0x211e20020,3),(1,8,0x211e20020,3)]:
                original=p.read64(reg)&31
                assert 2 <= original < 32
                def measure():
                    times=[]
                    for _ in range(5):
                        assert execute(cpu,'work',2000000,data)==2000000
                        a,b=struct.unpack('<QQ',f.readmem(data,16));times.append((b-a)/hz)
                    return {'hz':call('cpufreq_get_cluster_hz',cluster),'seconds':times,'median':statistics.median(times),'raw':hex(p.read64(reg))}
                try:
                    assert call('cpufreq_set_cluster_pstate',cluster,2) == 0
                    before=measure()
                    ret=call('cpufreq_set_cluster_pstate',cluster,trial)
                    print('CLOCK_SET',cluster,original,trial,ret,flush=True);assert ret==0
                    during=measure();assert p.read64(reg)&31==trial
                    assert call('cpufreq_set_cluster_pstate',cluster,2) == 0
                    low_restored=measure()
                    ratio=before['median']/during['median']
                    expected=during['hz']/before['hz']
                    assert abs(ratio/expected-1) < 0.10, (ratio,expected)
                    assert abs(low_restored['median']/before['median']-1) < 0.10
                finally:
                    ret=call('cpufreq_set_cluster_pstate',cluster,original)
                    assert ret==0 and p.read64(reg)&31==original, 'Clock restoration failed'
                after=measure()
                r={'cluster':cluster,'cpu':cpu,'original':original,'trial':trial,'before':before,'during':during,'after':after,'low_restored':low_restored,'speed_ratio':ratio,'expected_ratio':expected}
                measurements.append(r);s['clocks']=measurements;save();print('CLOCK_ROUNDTRIP_PASS',r,flush=True)
        elif args.action == 'atomic':
            f.writemem(data,bytes(0x1000));count=1000000
            start=execute(6,'ticks')+hz*2
            for cpu in range(12):
                if cpu!=6:p.smp_call(cpu,s['functions']['atomic'],data,count,start,data+128+cpu*128)
            assert execute(6,'atomic',data,count,start,data+128+6*128)==count
            for cpu in range(12):
                if cpu!=6:assert complete(cpu)==count
            windows=[struct.unpack('<QQ',f.readmem(data+128+i*128,16)) for i in range(12)]
            actual=p.read64(data);overlap=min(b for a,b in windows)-max(a for a,b in windows)
            r={'actual':actual,'expected':12*count,'overlap_ticks':overlap,'counter_hz':hz,'windows':windows}
            s['atomic']=r;save();print('NATIVE_ATOMIC_RESULT',r,flush=True)
            assert actual==12*count and overlap>0
        elif args.action == 'amp':
            count=50000000;p.smp_call(0,s['functions']['work'],count,data)
            requests=[]
            for _ in range(100):
                before=execute(6,'ticks');p.nop();after=execute(6,'ticks');requests.append((before,after))
            assert complete(0)==count
            a,b=struct.unpack('<QQ',f.readmem(data,16))
            n=sum(a<=x<y<=b for x,y in requests)
            r={'count':count,'requests':100,'requests_during_worker':n,'worker_ticks':[a,b]}
            s['amp']=r;save();print('NATIVE_AMP_RESULT',r,flush=True);assert n>0
        save()
    p.nop();print('PROXY_VERIFIED',flush=True)
finally:f.dev.close()
