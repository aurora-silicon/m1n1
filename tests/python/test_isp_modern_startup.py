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
    p=Proxy();result={'dart_mappings':[{'ttbr':hex(0x1235+i*4)} for i in range(3)]}
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
