import pytest

from proxyclient.m1n1.hw.dart8110 import DART8110, DART8110Regs


class Hardware:
    def __init__(self, four_levels=False):
        self.values = {0x1000: 1 | (8 if four_levels else 0)}
        self.pages = {}
        self.events = []
        self.next_page = 0x100000000

    def read(self, addr, *, width):
        assert width == 32
        return self.read32(addr)

    def write(self, addr, value, *, width):
        assert width == 32
        self.write32(addr, value)

    def read32(self, addr):
        return self.values.get(addr, 0)

    def write32(self, addr, value):
        self.events.append(('register', addr, value))
        self.values[addr] = value

    def memalign(self, align, size):
        addr = self.next_page
        self.next_page += size
        return addr

    def readmem(self, addr, size):
        return self.pages.get(addr, bytes(size))

    def writemem(self, addr, data):
        self.events.append(('table', addr, len(data)))
        self.pages[addr] = data


def controller(four_levels=False):
    hw = Hardware(four_levels)
    dart = DART8110(hw, DART8110Regs(hw, 0), hw)
    return hw, dart


@pytest.mark.parametrize('args', [
    (0, 1, 0x200000000, 0x4000),
    (0, 0, 0x200000001, 0x4000),
    (0, 0, 0x200000000, -1),
    (-1, 0, 0x200000000, 0x4000),
    (256, 0, 0x200000000, 0x4000),
])
def test_invalid_mapping_has_no_side_effects(args):
    hw, dart = controller()
    with pytest.raises(Exception):
        dart.iomap_at(*args)
    assert hw.events == []
    assert dart.enabled_streams == 0


def test_bypassed_stream_rejection_does_not_enable_it():
    hw, dart = controller()
    hw.values[0x1000] = 2
    with pytest.raises(Exception, match='bypassed'):
        dart.iomap_at(0, 0, 0x200000000, 0x4000)
    assert hw.events == []
    assert dart.enabled_streams == 0


@pytest.mark.parametrize('four_levels', [False, True])
def test_tables_precede_stream_enable_and_reused_root_translates(four_levels):
    hw, dart = controller(four_levels)
    iova = 0x10000000000 if four_levels else 0
    dart.iomap_at(0, iova, 0x200000000, 0x4000)
    assert hw.events[-1] == ('register', 0xc00, 1)
    assert any(event[0] == 'table' for event in hw.events[:-1])
    # Drop software cache so translation checks the published tables.
    dart.invalidate_cache()
    assert dart.iotranslate(0, iova, 16) == [(0x200000000, 16)]
    root = hw.values[0x1400]
    before = hw.next_page
    dart.iomap_at(0, iova + 0x4000, 0x200004000, 0x4000)
    assert hw.next_page == before
    assert hw.values[0x1400] == root
    dart.invalidate_cache()
    assert dart.iotranslate(0, iova + 0x4000, 16) == [(0x200004000, 16)]
    # A different leaf under the existing L0 root must allocate one table.
    dart.iomap_at(0, iova + (1 << 25), 0x200008000, 0x4000)
    assert hw.next_page == before + 0x4000
    dart.invalidate_cache()
    assert dart.iotranslate(0, iova + (1 << 25), 16) == [(0x200008000, 16)]


@pytest.mark.parametrize('four_levels', [False, True])
def test_deferred_firmware_mappings_leave_stream_disabled(four_levels):
    hw, dart = controller(four_levels)
    iova = 0x10000000000 if four_levels else 0
    dart.iomap_at(0, iova, 0x200000000, 0x4000, enable=False)
    dart.iomap_at(0, iova + 0x4000, 0x200004000, 0x4000, enable=False)
    assert hw.read32(0xc00) == 0
    assert dart.enabled_streams == 0
    assert not any(event[:2] == ('register', 0xc00) for event in hw.events)
    dart.invalidate_cache()
    assert dart.iotranslate(0, iova, 0x8000) == [(0x200000000, 0x8000)]
    # Ordinary mapping can enable the stream once its firmware map is ready.
    dart.iomap_at(0, iova + 0x8000, 0x200008000, 0x4000)
    assert hw.read32(0xc00) == 1
    assert dart.enabled_streams == 1
