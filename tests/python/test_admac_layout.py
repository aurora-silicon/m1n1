from types import SimpleNamespace

import pytest

from m1n1.hw.admac import ADMAC, ADMACDescriptor, E_BUSWIDTH, E_FRAME


class Backend:
    def __init__(self, compatible):
        self.values = {}
        self.writes = []
        self.proxy = SimpleNamespace(iface=None)
        node = SimpleNamespace(compatible=[compatible],
                               _properties={'#dma-channels': 9},
                               get_reg=lambda index: (0x100000, 0x34000))
        self.adt = {'/admac': node}

    def read(self, address, width):
        return self.values.get(address, 0)

    def write(self, address, value, width):
        self.writes.append((address, value))
        self.values[address] = value


@pytest.mark.parametrize('compatible,split', [('admac,t8103', False), ('admac,t604x', True)])
@pytest.mark.parametrize('channel', [0, 1, 16, 17])
def test_channel_accessors_follow_layout(compatible, split, channel):
    backend = Backend(compatible)
    dma = ADMAC(backend, '/admac')
    chan = dma.chans[channel]
    offset = 0x8000 + (channel // 2 * 0x200 + channel % 2 * 0x4000 if split else channel * 0x200)
    base = dma.base + offset
    for name, off in [('CTL', 0), ('BUSWIDTH', 0x40), ('SRAM_CARVEOUT', 0x50),
                      ('BURSTSIZE', 0x54), ('RESIDUE', 0x64),
                      ('DESC_RING', 0x70), ('REPORT_RING', 0x74)]:
        assert getattr(dma.regs, 'CHAN_' + name)[chan.reg_ch].addr == base + off
    for line in range(4):
        assert dma.regs.CHAN_STATUS[chan.reg_ch, line].addr == base + 0x10 + line * 4
        assert dma.regs.CHAN_INTMASK[chan.reg_ch, line].addr == base + 0x20 + line * 4
    fifo = dma.base + 0x10000 + channel // 2 * 4 + channel % 2 * 0x4000
    assert chan.DESC_WRITE.addr == fifo
    assert chan.REPORT_READ.addr == fifo + 0x100

    chan.reset()
    assert backend.writes[:2] == [(base, 3), (base, 0)]
    assert chan.buswidth == E_BUSWIDTH.W_32BIT
    assert chan.framesize == E_FRAME.F_1_WORD
    chan.sram_carveout = (0x800, 0x800)
    assert chan.sram_carveout == (0x800, 0x800)
    assert backend.values[base + 0x50] == 0x08000800
    chan.enable()
    assert backend.writes[-2][0] == base + 0x20
    assert backend.writes[-1] == (dma.base + (0 if chan.tx else 8), 1 << (channel // 2))
    chan.disable()
    assert backend.writes[-1] == (dma.base + (4 if chan.tx else 12), 1 << (channel // 2))
    descriptor = ADMACDescriptor(0x10000124000, 0x4000, DESC_ID=1, NOTIFY=1)
    chan.submit_desc(descriptor)
    assert backend.writes[-4:] == [(fifo, word) for word in descriptor.ser()]


def test_physical_address_keeps_legacy_layout():
    backend = Backend('admac,t604x')
    dma = ADMAC(backend, 0x100000)
    assert not dma.split_channels
    assert dma.regs.CHAN_CTL[dma.chans[1].reg_ch].addr == 0x108200
    assert backend.writes == []


@pytest.mark.parametrize('compatible', ['admac,t8103', 'admac,t604x'])
@pytest.mark.parametrize('channel', [1, 17])
def test_receive_report_after_ring_error(compatible, channel):
    backend = Backend(compatible)
    dma = ADMAC(backend, '/admac', debug=True)
    chan = dma.chans[channel]
    status = dma.regs.CHAN_STATUS[chan.reg_ch, 0].addr
    desc_ring = dma.regs.CHAN_DESC_RING[chan.reg_ch].addr
    report_ring = dma.regs.CHAN_REPORT_RING[chan.reg_ch].addr
    fifo = chan.REPORT_READ.addr
    words = [0x12345678, 0, 0x02000000, 1]
    pending = list(words)
    backend.values.update({status: 0x41, desc_ring: 0x500, report_ring: 0x400})
    read, write = backend.read, backend.write

    def hardware_read(address, width):
        if address == fifo:
            value = pending.pop(0)
            if not pending:
                backend.values[report_ring] = 0x100
            return value
        return read(address, width)

    def hardware_write(address, value, width):
        old = backend.values.get(address, 0)
        write(address, value, width)
        if address == status:
            backend.values[address] = old & ~value
        elif address in (desc_ring, report_ring):
            backend.values[address] = old & ~(value & 0x400)

    backend.read, backend.write = hardware_read, hardware_write
    # RegMap binds its backend methods when constructed.
    dma.regs = type(dma.regs)(backend, dma.base)
    chan.regs = dma.regs
    chan._submitted[1] = ADMACDescriptor(0x10000124000, 0x4000, DESC_ID=1, NOTIFY=1)
    assert chan.poll(wait=False) == bytearray()
    assert chan._last_report.ser() == words
    assert not pending
    assert not backend.values[status] & 1
    assert not backend.values[desc_ring] & 0x400
    assert not backend.values[report_ring] & 0x400
    assert {address for address, value in backend.writes} == {status, desc_ring, report_ring}
