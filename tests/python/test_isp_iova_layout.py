import importlib
import struct
import sys
from types import ModuleType, SimpleNamespace

import pytest

from m1n1.hw.dart import DART
from m1n1.malloc import Heap


@pytest.fixture
def isp_module(monkeypatch):
    parent = importlib.import_module("m1n1.fw")
    prefix = "m1n1.fw.isp"
    saved = {name: module for name, module in sys.modules.items()
             if name == prefix or name.startswith(prefix + ".")}
    missing = object()
    attribute = getattr(parent, "isp", missing)
    for name in saved:
        del sys.modules[name]
    if attribute is not missing:
        del parent.isp
    # Image rendering is not exercised by the memory mapping model.
    for name in ("cv2", "numpy"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    color = ModuleType("termcolor")
    color.colored = lambda text, *args: text
    monkeypatch.setitem(sys.modules, "termcolor", color)
    try:
        module = importlib.import_module("m1n1.fw.isp.isp_base")
        monkeypatch.setattr(module, "ISPChannelTable",
                            lambda isp, desc: SimpleNamespace(channels=[]))
        yield module
    finally:
        for name in list(sys.modules):
            if name == prefix or name.startswith(prefix + "."):
                del sys.modules[name]
        sys.modules.update(saved)
        if attribute is missing:
            parent.__dict__.pop("isp", None)
        else:
            parent.isp = attribute


class Registers:
    def __init__(self, extra_size):
        self.extra_size = extra_size

    def __getattr__(self, name):
        values = {"ISP_GPIO_0": 7, "ISP_GPIO_3": self.extra_size,
                  "ISP_GPIO_7": 0x8042006}
        if name.startswith("ISP_GPIO_"):
            return SimpleNamespace(val=values.get(name, 0))
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name.startswith("ISP_GPIO_"):
            if name == "ISP_GPIO_3" and value == 0x8042006:
                self.extra_size = 0
        else:
            object.__setattr__(self, name, value)


def model(module, data_iova, data_size, extra_size, start=0x3a28000,
          end=0x20000000):
    isp = module.ISP.__new__(module.ISP)
    isp.PAGE_SIZE = isp.page_size = 0x4000
    isp.regs = Registers(extra_size)
    isp.p = SimpleNamespace(memset32=lambda *args: None)
    heap = Heap(0x100000000, 0x120000000, 0x4000)
    segments = struct.pack("<QQQI4xQQQI4x", 0x100000000, 0, 0, data_iova,
                           0x110000000, data_iova, 0, data_size)
    isp.u = SimpleNamespace(heap=heap, memalign=heap.memalign,
                            adt={"/arm-io/isp": SimpleNamespace(**{
                                "segment-ranges": segments})})
    isp.dart = DART.__new__(DART)
    isp.dart.iova_allocator = [Heap(start, end, 0x4000)]
    mappings = []
    isp.dart.dart = SimpleNamespace(iomap_at=lambda stream, iova, phys, size:
                                    mappings.append((iova, iova + ((size + 0x3fff) & ~0x3fff))))
    isp.asc = SimpleNamespace(boot=lambda: None)
    isp.sync_ttbr = lambda: None
    isp.log = lambda *args: None
    isp.ioread = lambda iova, size: bytes(size)
    isp.iowrite = lambda iova, data: None
    isp.mmger = module.ISPMemoryManager(isp)
    isp.mmger._stfu = True
    return isp, mappings


@pytest.mark.parametrize("data_iova,data_size,extra_size,expected", [
    (0x9b4000, 0x41c000, 0x2200000, 0x3a28000),
    # Captured J616s segments with a synthetic legacy extra-heap request.
    (0xca8000, 0x1344000, 0x2200000, 0x4c44000),
    (0xca8000, 0x1344000, 0x2200001, 0x4c48000),
])
def test_surface_does_not_replace_firmware_mapping(isp_module, data_iova,
                                                  data_size, extra_size, expected):
    isp, mappings = model(isp_module, data_iova, data_size, extra_size)
    isp.initialize_firmware()
    fixed = list(mappings)
    channel_module = importlib.import_module("m1n1.fw.isp.isp_chan")
    channel = channel_module.ISPSharedMallocChannel.__new__(
        channel_module.ISPSharedMallocChannel)
    channel.isp = isp
    channel.log = lambda *args: None
    reply = channel._handle(SimpleNamespace(arg0=0, arg1=0x4000, arg2=0x4c4f47))
    assert reply.arg0 == expected | 1
    assert reply.arg1 == 0
    assert all(expected >= end or expected + 0x4000 <= start
               for start, end in fixed)


def test_used_allocator_rejected_before_mapping(isp_module):
    isp, mappings = model(isp_module, 0xca8000, 0x1344000, 0x2200000)
    isp.dart.iova_allocator[0].malloc(0x4000)
    with pytest.raises(ValueError, match="already in use"):
        isp.initialize_firmware()
    assert mappings == []


def test_custom_allocator_bounds(isp_module):
    isp, _ = model(isp_module, 0xca8000, 0x1344000, 0x2200000,
                   start=0x6000000, end=0x6008000)
    isp.initialize_firmware()
    assert isp.mmger.alloc_size(0x4000).iova == 0x6000000
    assert isp.mmger.alloc_size(0x4000).iova == 0x6004000
    with pytest.raises(Exception, match="Out of memory"):
        isp.mmger.alloc_size(0x4000)


def test_exhausted_layout_rejected_before_ipc_mapping(isp_module):
    isp, mappings = model(isp_module, 0xca8000, 0x1344000, 0x2200000,
                           end=0x4c44000)
    with pytest.raises(ValueError, match="exceeds IOVA range"):
        isp.initialize_firmware()
    assert len(mappings) == 3


@pytest.mark.parametrize("base", [0, 1 << 40])
def test_frame_metadata_preserves_surface_addresses(isp_module, base):
    video = sys.modules["m1n1.fw.isp.isp_vid"]
    record = bytearray(0x280)
    addresses = [base + 0x400000, base + 0x500000, base + 0x600000]
    for offset, address in zip((0x10, 0x50, 0x58), addresses):
        struct.pack_into("<Q", record, offset, address)
    struct.pack_into("<I", record, 0x218, 7)
    parsed = video.ISPFrameMeta.parse(record)
    assert (parsed.meta_iova, parsed.luma_iova, parsed.cbcr_iova) == tuple(addresses)
    assert parsed.index == 7
    assert video.ISPFrameMeta.build(parsed) == record


@pytest.mark.parametrize("base", [0, 1 << 40])
def test_buffer_submission_encodes_full_addresses(isp_module, base):
    video = sys.modules["m1n1.fw.isp.isp_vid"]
    addresses = (base + 0x400000, base + 0x500000)
    record = video.BufH2TSendArgs.build(dict(
        iova0=addresses[0], iova1=addresses[1],
        flag0=0x40000000, flag1=0x40000000,
        unk_30=2, pool=1, tag=0x500))
    expected = bytearray(0x40)
    struct.pack_into("<4Q6IQ", expected, 0,
                     *addresses, 0, 0, 0x40000000, 0x40000000,
                     0, 0, 2, 1, 0x500)
    assert record == expected
    parsed = video.BufH2TSendArgs.parse(record)
    assert (parsed.iova0, parsed.iova1) == addresses


@pytest.mark.parametrize("idle", [1, 3, 0x10000400001, 0x10000400003])
def test_receive_ring_stops_at_ack_without_dispatch(isp_module, idle):
    channel_module = sys.modules["m1n1.fw.isp.isp_chan"]
    message = channel_module.ISPChannelMessage.build(arg0=idle)
    isp = SimpleNamespace(ioread=lambda address, size: message.encode())
    desc = SimpleNamespace(name="BUF_T2H", type=1, src=3, num=2, iova=0x8000)
    channel = channel_module.ISPBufT2HChannel(isp, desc)
    channel._stfu = True
    channel.handler()
    assert channel.cursor == 0


@pytest.mark.parametrize("request_flag,cursor", [(0, 0), (2, 0), (0, 1), (2, 1)])
def test_receive_ring_retires_request_before_terminal_ack(isp_module,
                                                         request_flag, cursor):
    channel_module = sys.modules["m1n1.fw.isp.isp_chan"]
    request = channel_module.ISPChannelMessage.build(
        arg0=0x10000400000 | request_flag, arg1=0x280, arg2=0x10000000)
    idle = channel_module.ISPChannelMessage.build(arg0=3)
    slots = {0x8000 + cursor * 64: request.encode(),
             0x8000 + (1 - cursor) * 64: idle.encode()}
    writes = []
    isp = SimpleNamespace(
        ioread=lambda address, size: slots[address],
        iowrite=lambda address, data: writes.append((address, data)),
        regs=SimpleNamespace(ISP_IRQ_DOORBELL=SimpleNamespace(val=0)))
    desc = SimpleNamespace(name="BUF_T2H", type=1, src=3, num=2, iova=0x8000)
    channel = channel_module.ISPBufT2HChannel(isp, desc)
    channel._stfu = True
    channel.cursor = cursor
    frames = []
    channel.push_frame = frames.append
    channel.handler()
    assert [frame.encode() for frame in frames] == [request.encode()]
    assert writes == [(0x8000 + cursor * 64,
                       channel_module.ISPChannelMessage.build(
                           arg0=request.arg0 | 1, arg2=0x80000000).encode())]
    assert channel.cursor == 1 - cursor
    assert isp.regs.ISP_IRQ_DOORBELL.val == 8
