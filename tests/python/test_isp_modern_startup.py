from types import SimpleNamespace
import pytest
from m1n1.fw.isp.isp_startup import T6040ISPStartup
from m1n1.fw.isp import isp_startup as module

BASE=0x580000000
DARTS=(0x5828c0000,0x5828e0000,0x582900000)
DAPF=0x5828d0000


class Proxy:
    def __init__(self):
        self.words={BASE+0x1050000:(1<<40)|1}
        self.writes=[]
        self.magic=True
        for index,base in enumerate(DARTS):
            for sid in range(16):self.words[base+0x1000+4*sid]=1
            self.words[base+0x1000]=9
            for sid in (1,6,7,10,11):self.words[base+0x1000+sid*4]=0x80
            if index<2:self.words[base+0x1000+(15-index)*4]=2
            self.words[base+0xc00]=1
            self.words[base+0x1400]=0x1235+index*4

    def read32(self,address):return self.words.get(address,0)
    read64=read32
    def write32(self,address,value):
        self.words[address]=value;self.writes.append((address,value,32))
        if address==BASE+0x1600044 and value==0x10 and self.magic:
            self.words[BASE+0x29241d0+28]=0x8042006
    def write64(self,address,value):
        self.words[address]=value|3 if DAPF<=address<DAPF+11*0x40 and (address-DAPF)%0x40==16 else value
        self.writes.append((address,value,64))
    def get_exc_count(self):return 0


def model():
    p=Proxy();result={'dart_mappings':[{'instance':i,'base':hex(DARTS[i]),'ttbr':hex(0x1235+i*4)} for i in range(3)]}
    startup=T6040ISPStartup(proxy=p,guard=lambda:None,persist=lambda s:None,result=result,
        isp_base=BASE,plan=[{'name':'__TEXT','remap':1<<40}])
    records=[SimpleNamespace(start=i*0x1000,end=i*0x1000+0xfff,r0h=3,r0l=1,r4=0,r20=0) for i in range(11)]
    node=SimpleNamespace(get_reg=lambda i:(DAPF,0x4000),getprop=lambda n:records)
    return startup,p,result,node


def test_access_gpio_and_first_handshake():
    startup,p,result,node=model()
    startup.configure_firmware_access(node)
    assert len(result['dapf'])==11 and len(result['native_contexts'])==3
    assert [w[0]-DAPF for w in p.writes[:5]]==[4,8,16,0,32]
    assert [w[2] for w in p.writes[:5]]==[32,64,64,32,32]
    startup.initialize_gpio()
    assert p.words[startup.gpio+24]==1
    startup.start_firmware(armed_at=module.time.monotonic())
    assert result['first_handshake']=='passed'
    assert p.writes[-2:]==[(BASE+0x1600044,0,32),(BASE+0x1600044,0x10,32)]


@pytest.mark.parametrize('failure',['control','dapf','alias','root'])
def test_access_gate_refuses_without_start(failure):
    startup,p,result,node=model()
    if failure=='control':p.words[BASE+0x1600044]=0x10
    if failure=='dapf':node.getprop=lambda n:[]
    if failure=='alias':p.words[DARTS[0]+0x1004]=1
    if failure=='root':p.words[DARTS[0]+0x1400]=0
    with pytest.raises(ValueError):startup.configure_firmware_access(node)
    assert not any(a==BASE+0x1600044 for a,_,_ in p.writes)


def test_no_teardown_on_first_handshake_timeout(monkeypatch):
    startup,p,result,node=model();p.magic=False
    startup.configure_firmware_access(node)
    startup.initialize_gpio()
    p.writes.clear()
    ticks=iter(range(0,100,5))
    monkeypatch.setattr(module.time,'monotonic',lambda:next(ticks))
    monkeypatch.setattr(module.time,'sleep',lambda n:None)
    with pytest.raises(TimeoutError, match='first firmware handshake'):
        startup.start_firmware(armed_at=0)
    assert result['first_handshake']=='no magic within 10 seconds'
    assert p.words[BASE+0x1600044]==0x10
    assert p.writes==[(BASE+0x1600044,0,32),(BASE+0x1600044,0x10,32)]


@pytest.mark.parametrize('method', ['initialize_gpio', 'start_firmware'])
def test_public_entrypoints_refuse_active_control_without_writes(method):
    startup,p,result,node=model()
    startup.configure_firmware_access(node)
    if method=='start_firmware':startup.initialize_gpio()
    p.writes.clear()
    p.words[BASE+0x1600044]=0x10
    with pytest.raises(ValueError, match='running|active'):
        if method=='start_firmware':
            startup.start_firmware(armed_at=0)
        else:
            startup.initialize_gpio()
    assert p.writes==[]


@pytest.mark.parametrize('method',['initialize_gpio','start_firmware'])
def test_skipped_phase_refuses_without_writes(method):
    startup,p,result,node=model()
    with pytest.raises(ValueError):
        if method=='initialize_gpio':startup.initialize_gpio()
        else:startup.start_firmware(armed_at=0)
    assert p.writes==[]


def test_mutated_gpio_and_repeated_start():
    startup,p,result,node=model()
    startup.configure_firmware_access(node);startup.initialize_gpio()
    p.writes.clear();p.words[startup.gpio]=1
    with pytest.raises(ValueError,match='GPIO changed'):startup.start_firmware(armed_at=0)
    assert p.writes==[]
    p.words[startup.gpio]=0
    startup.start_firmware(armed_at=0)
    p.writes.clear()
    with pytest.raises(ValueError,match='already attempted'):startup.start_firmware(armed_at=0)
    assert p.writes==[]


def test_poll_fault_cannot_accept_magic():
    startup,p,result,node=model()
    startup.configure_firmware_access(node);startup.initialize_gpio()
    fault=[False]
    original=p.read32
    def read32(address):
        value=original(address)
        if address==startup.gpio+28 and p.words.get(BASE+0x1600044)==0x10:
            fault[0]=True
        return value
    p.read32=read32
    p.get_exc_count=lambda:int(fault[0])
    with pytest.raises(ValueError):startup.start_firmware(armed_at=0)
    assert result.get('first_handshake')!='passed'
    assert startup.start_attempted


def test_failed_access_not_marked_ready():
    startup,p,result,node=model()
    node.getprop=lambda n:[]
    with pytest.raises(ValueError):startup.configure_firmware_access(node)
    assert not startup.access_ready and not startup.gpio_ready


def test_failed_gpio_not_marked_ready():
    startup,p,result,node=model()
    startup.configure_firmware_access(node)
    p.get_exc_count=lambda:1
    with pytest.raises(ValueError):startup.initialize_gpio()
    assert startup.access_ready and not startup.gpio_ready


def boot_model(failure=None):
    import json, struct
    from pathlib import Path
    fixture=json.loads((Path(__file__).parent/'fixtures/isp_t6040_25g76.json').read_text())
    profile=module.T6040_25G76
    segments=profile.validate_segments(firmware_sha256=fixture['firmware_sha256'],
        segment_names=fixture['segment_names'],segment_ranges=bytes.fromhex(fixture['segment_ranges_hex']))
    startup,p,result,_=model()
    startup.plan=[dict(name=s.name,phys=s.phys,iova=s.virtual,remap=s.remap,size=s.size,flags=s.flags) for s in segments]
    startup.start_attempted=True
    result.update(first_handshake='passed',boot_block_reader_source_match=True)
    layout=profile.boot_layout(segments,args_offset=fixture['args_offset'],extra_size=fixture['extra_size'])
    memory={};cursor=[0x30000000]
    def alloc(page,size):
        address=cursor[0];cursor[0]+=size
        return 0 if failure=='allocation' else address
    node=SimpleNamespace(get_reg=lambda i:(BASE,0x2924474),
        getprop=lambda key: {'sensor-type':5,'cam-connections-scheme':16}[key])
    u=SimpleNamespace(adt={'/arm-io/isp0':node},heap=SimpleNamespace(memalign=alloc),
        heap_base=0x20000000,heap_top=0x50000000)
    p.words.update({startup.gpio:7,startup.gpio+4:fixture['args_offset'],startup.gpio+8:2,
        startup.gpio+12:fixture['extra_size'],startup.gpio+28:0x8042006,BASE+0x1600044:0x10})
    p.memset32=lambda a,v,s:None
    p.dc_cvac=lambda a,s:None
    p.dc_ivac=lambda a,s:None
    original=p.write32
    def write32(address,value):
        original(address,value)
        if address==startup.gpio+28 and value==0xf7fbdff9:
            p.words[startup.gpio]=layout.ipc_iova&0xffffffff
            p.words[startup.gpio+4]=layout.ipc_iova>>32
            p.words[startup.gpio+28]=0x8042006
    p.write32=write32
    table=bytes.fromhex(fixture['channel_table_hex'])
    def readmem(address,size):
        if address in memory:
            return bytes(size) if failure=='boot_readback' else memory[address][:size]
        if address==0x30000000:
            return bytes(size) if failure=='channels' else table
        return b'\xff'*size if failure=='table_readback' else bytes(size)
    iface=SimpleNamespace(readmem=readmem,writemem=lambda a,b:memory.update({a:b}))
    class Dart:
        def __init__(self,index):
            self.regs=SimpleNamespace(TTBR=[SimpleNamespace(val=0x1235+index*4)])
            self.pt_cache={0x20000000+index*0x4000:[0]*2048}
            self.mappings=[]
        def iomap_at(self,sid,va,pa,size,enable):
            assert not enable
            self.mappings.append((va,pa,size))
        def iotranslate(self,sid,va,size):
            for v,p,s in self.mappings:
                if v<=va<va+size<=v+s:return [(p+va-v,size)]
        def invalidate_cache(self):pass
    darts=[(base,Dart(i)) for i,base in enumerate(DARTS)]
    if failure=='root':p.words[DARTS[0]+0x1400]=0
    return startup,p,result,u,iface,darts,segments


def test_second_handshake_publication_follows_all_mapping_checks():
    startup,p,result,u,iface,darts,segments=boot_model()
    startup.complete_boot(u=u,iface=iface,darts=darts,segments=segments)
    assert result['second_handshake']=='passed'
    assert result['modern_profile_channels_validated']
    assert len(result['channels'])==7
    assert all(len(dart.mappings)==2 for _,dart in darts)
    args=int(result['boot_block']['args_iova'],16)
    assert p.writes[-3:]==[(startup.gpio,args&0xffffffff,32),
        (startup.gpio+4,args>>32,32),(startup.gpio+28,0xf7fbdff9,32)]
    assert result['boot_block']['readback_matches']
    p.writes.clear()
    with pytest.raises(ValueError,match='already attempted'):
        startup.complete_boot(u=u,iface=iface,darts=darts,segments=segments)
    assert not p.writes


@pytest.mark.parametrize('failure',['allocation','table_readback','boot_readback','root'])
def test_second_handshake_failure_prevents_gpio_publication(failure):
    startup,p,result,u,iface,darts,segments=boot_model(failure)
    with pytest.raises(ValueError):startup.complete_boot(u=u,iface=iface,darts=darts,segments=segments)
    assert not any(startup.gpio<=a<startup.gpio+32 for a,_,_ in p.writes)
    assert startup.boot_attempted


def test_second_handshake_rejects_invalid_channels_after_publication():
    startup,p,result,u,iface,darts,segments=boot_model('channels')
    with pytest.raises(ValueError):startup.complete_boot(u=u,iface=iface,darts=darts,segments=segments)
    assert result.get('modern_profile_channels_validated') is not True


@pytest.mark.parametrize('bad',['empty','missing','duplicate','wrongbase','wrongrecord'])
def test_second_handshake_requires_exact_owned_dart_set(bad):
    startup,p,result,u,iface,darts,segments=boot_model()
    allocations=[]
    u.heap.memalign=lambda page,size:allocations.append(size) or 0x30000000
    if bad=='empty':darts=[]
    if bad=='missing':darts=darts[:2]
    if bad=='duplicate':darts[1]=(DARTS[1],darts[0][1])
    if bad=='wrongbase':darts[0]=(0x5828c4000,darts[0][1])
    if bad=='wrongrecord':result['dart_mappings'][1]['base']=hex(DARTS[0])
    with pytest.raises(ValueError):startup.complete_boot(u=u,iface=iface,darts=darts,segments=segments)
    assert allocations==[] and p.writes==[]
    assert not getattr(startup,'boot_attempted',False)


@pytest.mark.parametrize('fault',['cpu','dart'])
def test_final_context_read_fault_prevents_publication(fault):
    startup,p,result,u,iface,darts,segments=boot_model()
    final=[False];cpu=[False]
    startup.persist=lambda stage:final.__setitem__(0,True) if stage=='before second handshake boot block submission' else None
    original=p.read32
    def read32(address):
        value=original(address)
        if final[0] and address==DARTS[2]+0x1000+4*11:
            if fault=='cpu':cpu[0]=True
            else:p.words[DARTS[2]+0x100]=1
        return value
    p.read32=read32
    p.get_exc_count=lambda:int(cpu[0])
    with pytest.raises(ValueError,match='fault|exception'):
        startup.complete_boot(u=u,iface=iface,darts=darts,segments=segments)
    assert not any(startup.gpio<=a<startup.gpio+32 for a,_,_ in p.writes)
