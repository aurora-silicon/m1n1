import json
from dataclasses import replace
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest
from m1n1.fw.isp import isp_mapping as module

BASES=(0x5828c0000,0x5828e0000,0x582900000)


class Backend:
    def __init__(self):
        self.words={}
        self.memory={}
        self.writes=[]
        self.failure=None
        for base in BASES:
            self.words[base+8]=(42<<24)|(42<<16)|(2<<8)
            for sid in (0,1,6,7,10,11): self.words[base+0x1000+sid*4]=1

    def read32(self,address): return self.words.get(address,0)
    def write32(self,address,value):
        self.writes.append((address,value))
        self.words[address]=value
    def dc_cvac(self,*args): pass
    def get_exc_count(self): return 0
    def readmem(self,address,size):
        data=self.memory[address]
        return bytes(size) if self.failure=='readback' else data


class Registers:
    def __init__(self,backend,base):
        self.backend=backend.proxy
        self.base=base
        self.TTBR=[SimpleNamespace(reg=SimpleNamespace(ADDR=0),val=0)]
        self.TCR=[SimpleNamespace(val=9)]


class FakeDART:
    def __init__(self,iface,regs,u):
        self.iface=iface;self.regs=regs;self.enabled_streams=0
        self.root=0x20000000+BASES.index(regs.base)*0x10000
        self.pt_cache={self.root:[0]*2048,self.root+0x4000:[0]*2048,self.root+0x8000:[0]*2048}
        self.mappings=[]
        self.cache_discarded=False
        self.regs.TTBR[0].reg.ADDR=self.root>>14
        self.regs.TTBR[0].val=((self.root>>14)<<2)|1
        self.iface.words[regs.base+0x1400]=self.regs.TTBR[0].val

    def iomap_at(self,sid,iova,phys,size,enable):
        assert sid==0 and not enable and 1<<40<=iova<iova+size<=1<<42
        self.mappings.append((iova,phys,size))
        self.pt_cache[self.root][(iova>>36)&0x7ff]=module.PTE(OFFSET=(self.root+0x4000)>>14,VALID=1).value
        self.pt_cache[self.root+0x4000][(iova>>25)&0x7ff]=module.PTE(OFFSET=(self.root+0x8000)>>14,VALID=1).value
        for offset in range(0,size,0x4000):
            self.pt_cache[self.root+0x8000][((iova+offset)>>14)&0x7ff]=module.PTE(OFFSET=(phys+offset)>>14,VALID=1).value

    def flush_pt(self,table): self.iface.memory[table]=struct.pack('<2048Q',*self.pt_cache[table])
    def invalidate_cache(self): self.cache_discarded=True
    def iotranslate(self,sid,iova,size):
        assert self.cache_discarded
        if self.iface.failure=='translation': return [(None,size)]
        for va,pa,span in self.mappings:
            if va<=iova<iova+size<=va+span:return [(pa+iova-va,size)]


def run(monkeypatch,failure=None,backend=None):
    monkeypatch.setattr(module,'DART8110',FakeDART)
    monkeypatch.setattr(module,'DART8110Regs',Registers)
    fixture=json.loads((Path(__file__).parent/'fixtures/isp_t6040_25g76.json').read_text())
    segments=module.T6040_25G76.validate_segments(firmware_sha256=fixture['firmware_sha256'],segment_names=fixture['segment_names'],segment_ranges=bytes.fromhex(fixture['segment_ranges_hex']))
    backend = Backend() if backend is None else backend
    backend.failure=failure
    u=SimpleNamespace(proxy=backend,heap_base=0x20000000,heap_top=0x21000000)
    result={};restore=[]
    if failure=='geometry':
        segments=(segments[0],replace(segments[1],size=0x4000))
    if failure=='width':backend.words[BASES[0]+8]=(40<<24)|(40<<16)|(2<<8)
    if failure=='alias':backend.words[BASES[0]+0x1000+4]=0
    if failure=='unowned':u.heap_top=0x20000000
    darts=module.map_firmware_darts(u=u,p=backend,iface=backend,dart_node=SimpleNamespace(get_reg=lambda i:(BASES[i],0x1800)),isp_base=0x580000000,segments=segments,guard=lambda:None,persist=lambda s:None,result=result,dart_restore=restore)
    return backend,darts,result,restore


def test_exact_mappings_and_text_protection(monkeypatch):
    backend,darts,result,restore=run(monkeypatch)
    assert len(darts)==len(restore)==3
    for base,dart in darts:
        assert dart.cache_discarded
        for va,pa,size in dart.mappings:
            assert va>=1<<40
            for offset in range(0,size,0x4000):
                pte=module.PTE(dart.pt_cache[dart.root+0x8000][((va+offset)>>14)&0x7ff])
                assert pte.VALID and pte.WRPROT==(va==1<<40)
        assert all(backend.words[base+0x1000+sid*4]==0x80 for sid in (1,6,7,10,11))
        assert backend.words[base+0xc00]==1
        enable=backend.writes.index((base+0xc00,1))
        assert all(backend.writes.index((base+0x1000+sid*4,0x80))<enable for sid in (1,6,7,10,11))


@pytest.mark.parametrize('failure',['width','readback','translation','alias','unowned','geometry'])
def test_mapping_failure_refused(monkeypatch,failure):
    with pytest.raises(ValueError):run(monkeypatch,failure)


def test_running_processor_rejected_before_writes(monkeypatch):
    backend = Backend()
    backend.words[0x580000000 + 0x1600044] = 0x10
    with pytest.raises(ValueError, match="processor already running"):
        run(monkeypatch, backend=backend)
    assert backend.writes == []
