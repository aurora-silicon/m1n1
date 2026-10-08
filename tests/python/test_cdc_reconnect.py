from types import SimpleNamespace
import struct

import pytest

from proxyclient.m1n1 import cdc_reconnect as cdc


def port(path, vid, pid, interface=None):
    return SimpleNamespace(device=path, vid=vid, pid=pid, interface=interface)


def test_discovery_uses_usb_metadata_then_primary_nop():
    ports = [port("/dev/old-kis", None, None),
             port("/dev/second-acm", cdc.CDC_VID, cdc.CDC_PID, "secondary"),
             port("/dev/primary-acm", cdc.CDC_VID, cdc.CDC_PID, "primary"),
             port("/dev/other", 0x1234, 0x5678)]
    seen = []

    def probe(candidate, identity):
        seen.append(candidate.device)
        return "verified" if candidate.device == "/dev/primary-acm" else None

    result = cdc.discover_cdc(object(), timeout=1, ports=lambda: ports, probe=probe,
                              now=lambda: 0, sleep=lambda _: None)
    assert result == "verified"
    assert seen == ["/dev/primary-acm"]


def test_missing_primary_is_bounded_and_does_not_use_old_pty():
    times = iter([0, 0, 0.5, 1.1])
    with pytest.raises(cdc.CdcDiscoveryTimeout, match="acknowledged KIS"):
        cdc.discover_cdc(object(), timeout=1, ports=lambda: [port("/dev/old-kis", None, None)],
                         now=lambda: next(times), sleep=lambda _: None)


def test_identity_mismatch_is_distinct_from_discovery_timeout():
    candidate = port("/dev/acm", cdc.CDC_VID, cdc.CDC_PID)

    def wrong_identity(candidate, identity):
        raise cdc.CdcIdentityMismatch("wrong ADT")

    with pytest.raises(cdc.CdcIdentityMismatch, match="wrong ADT"):
        cdc.discover_cdc(object(), timeout=1, ports=lambda: [candidate],
                         probe=wrong_identity, now=lambda: 0, sleep=lambda _: None)


def candidate_artifact(folder, image):
    elf = bytearray(0x100 + len(image))
    elf[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<Q", elf, 32, 64)
    struct.pack_into("<HH", elf, 54, 56, 1)
    struct.pack_into("<IIQQQQQQ", elf, 64, 1, 4, 0x100, 0, 0,
                     len(image), len(image), 0x100)
    elf[0x100:] = image
    (folder / "m1n1-raw.elf").write_bytes(elf)
    (folder / "m1n1.bin").write_bytes(image)


def test_capture_and_verify_full_adt_and_immutable_image(tmp_path):
    tag = b"##m1n1_ver##candidate-v1"
    image = tag + b"\0" * 17
    adt = b"full ADT bytes with private IDs"
    interface = SimpleNamespace(readmem=lambda addr, size: image[:size])
    utils = SimpleNamespace(iface=interface, base=0x1000, get_adt=lambda: adt)
    candidate_artifact(tmp_path, image)
    identity = cdc.capture_identity(utils, "candidate-v1", [(0, len(image))], tmp_path)
    cdc.verify_identity(utils, identity)
    interface.readmem = lambda addr, size: image[:size] + b"!"
    with pytest.raises(cdc.CdcIdentityMismatch, match="image"):
        cdc.verify_identity(utils, identity)
    interface.readmem = lambda addr, size: image[:size]
    utils.get_adt = lambda: adt + b"!"
    with pytest.raises(cdc.CdcIdentityMismatch, match="ADT"):
        cdc.verify_identity(utils, identity)


def test_matching_tag_with_altered_kis_image_fails_before_schedule(tmp_path):
    image = b"##m1n1_ver##candidate-v1" + b"stable bytes"
    candidate_artifact(tmp_path, image)
    altered = image[:-1] + b"!"
    utils = SimpleNamespace(
        iface=SimpleNamespace(readmem=lambda addr, size: altered[:size]),
        base=0x1000, get_adt=lambda: b"adt")
    with pytest.raises(cdc.CdcIdentityMismatch, match="candidate artifact"):
        cdc.capture_identity(utils, "candidate-v1", [(0, len(image))], tmp_path)


def test_kis_ack_precedes_close_and_rebootstrap(monkeypatch):
    actions = []
    old_device = SimpleNamespace(close=lambda: actions.append("close-kis"))
    proxy = SimpleNamespace(
        iface=SimpleNamespace(dev=old_device),
        cdc_schedule=lambda delay, flags: actions.append(("schedule", delay, flags)),
    )
    identity = cdc.CdcIdentity("candidate", "a" * 64, (cdc.ImmutableRegion(0, 10, "b" * 64),))
    monkeypatch.setattr(cdc, "capture_identity", lambda utils, tag, ranges, artifact: identity)
    monkeypatch.setattr(cdc, "discover_cdc", lambda identity, **kwargs: actions.append("discover"))
    cdc.transition_to_cdc(proxy, object(), build_tag="candidate",
                          immutable_ranges=[(0, 10)], artifact_dir="/candidate")
    assert actions == [("schedule", 1000, 6), "close-kis", "discover"]


def test_old_kis_timeout_is_not_reported_as_target_failure(monkeypatch):
    old_device = SimpleNamespace(close=lambda: pytest.fail("KIS closed without ACK"))
    proxy = SimpleNamespace(
        iface=SimpleNamespace(dev=old_device),
        cdc_schedule=lambda delay, flags: (_ for _ in ()).throw(cdc.UartTimeout()),
    )
    identity = cdc.CdcIdentity("candidate", "a" * 64, (cdc.ImmutableRegion(0, 10, "b" * 64),))
    monkeypatch.setattr(cdc, "capture_identity", lambda utils, tag, ranges, artifact: identity)
    with pytest.raises(cdc.CdcScheduleUncertain, match="state unknown"):
        cdc.transition_to_cdc(proxy, object(), build_tag="candidate",
                              immutable_ranges=[(0, 10)], artifact_dir="/candidate")


def test_reopen_discards_old_port_and_revalidates_identity(monkeypatch):
    actions = []
    identity = cdc.CdcIdentity("candidate", "a" * 64, (cdc.ImmutableRegion(0, 10, "b" * 64),))
    old = cdc.CdcSession("/dev/old", "primary", SimpleNamespace(
        dev=SimpleNamespace(close=lambda: actions.append("close-old"))),
        object(), object(), identity)
    fresh = cdc.CdcSession("/dev/new", "primary", object(), object(), object(), identity)
    monkeypatch.setattr(cdc, "discover_cdc", lambda identity, **kwargs: fresh)
    result = old.reconnect(log=actions.append)
    assert result is fresh and result.reconnect_count == 1
    assert actions[0] == "close-old"
    assert actions[1]["event"] == "cdc_reconnected"


def live_utils(monkeypatch):
    """Actual ProxyUtils and Heap, with only target RPC/serial I/O replaced."""
    from proxyclient.m1n1.proxy import M1N1Proxy, Feature
    from proxyclient.m1n1.tgtypes import BootArgs_r3
    from proxyclient.m1n1.malloc import Heap
    image = b'##m1n1_ver##live-test' + bytes(16)
    adt = b'fresh full ADT identity'
    ba_bytes = BootArgs_r3.build(dict(revision=3, version=2, virt_base=0, phys_base=34359738368, mem_size=17179869184, top_of_kernel_data=34359803904, video=dict(base=0, display=0, stride=0, width=0, height=0, depth=0), machine_type=0, devtree=8192, devtree_size=len(adt), cmdline='', boot_flags=0, mem_size_actual=17179869184))
    events = []
    opened = []

    class Interface:

        def __init__(self, path):
            self.path = path
            self.base = 1099511627776
            self.ba_bytes = ba_bytes
            self.image = image
            self.adt = adt
            self.closed = False
            self.dev = SimpleNamespace(timeout=1, close=self.close)
            self.devpath = path
            self.baudrate = 115200
            self.is_kis = 'kis' in path
            self.pted = False
            self.enabled_features = Feature(0)
            self.handlers = {1: object()}
            self.evt_handlers = {2: object()}
            self.tty_log = object()
            opened.append(self)

        def close(self):
            self.closed = True
            events.append(('close', self.path))

        def nop(self):
            events.append(('nop', self.path))

        def readstruct(self, address, schema):
            return schema.parse(self.ba_bytes)

        def readmem(self, address, size):
            events.append(('readmem', self.path, address, size))
            if address == self.base:
                return self.image[:size]
            if address == 34359746560:
                return self.adt[:size]
            raise AssertionError(hex(address))

    class Proxy(M1N1Proxy):

        def get_base(self):
            return self.iface.base

        def get_cpu_features(self):
            return None

        def get_bootargs_rev(self):
            return (34359742464, 3)

        def heapblock_alloc(self, size):
            events.append(('heapblock_alloc', self.iface.path))
            return 34628173824

        def heapblock_set_limit(self, limit):
            events.append(('heap_limit', self.iface.path, limit))

        def iodev_whoami(self):
            return cdc.IODEV.USB0

        def cdc_schedule(self, delay, flags):
            events.append(('schedule', delay, flags))
    old_iface = Interface('/dev/kis-original')
    old_proxy = Proxy(old_iface)
    utils = cdc.ProxyUtils(old_proxy)
    assert isinstance(utils.heap, Heap)
    identity = cdc.CdcIdentity('live-test', cdc._digest(adt), (cdc.ImmutableRegion(0, len(image), cdc._digest(image)),))
    monkeypatch.setattr(cdc, 'UartInterface', Interface)
    monkeypatch.setattr(cdc, 'M1N1Proxy', Proxy)
    monkeypatch.setattr(cdc, 'bootstrap_port', lambda *args: None)
    real_discover = cdc.discover_cdc

    def discover(identity, **kwargs):
        return real_discover(identity, ports=lambda: [port('/dev/cdc-new', cdc.CDC_VID, cdc.CDC_PID, 'primary')], now=lambda: 0, sleep=lambda _: None, **kwargs)
    monkeypatch.setattr(cdc, 'discover_cdc', discover)
    return (utils, identity, image, events, opened, real_discover)

@pytest.mark.parametrize('operation', ['transition', 'reconnect'])
def test_carrier_change_keeps_real_heap_proxy_utils_interface_and_aliases(monkeypatch, tmp_path, operation):
    utils, identity, image, events, opened, _ = live_utils(monkeypatch)
    old_proxy = utils.proxy
    old_iface = utils.iface
    heap = utils.heap
    retained = utils.memalign(16384, 16384)
    assert retained == 34762473472
    buffers = (utils.code_buffer, utils.simd_buf, utils.glk_arg_buf)
    methods = (utils.malloc, utils.memalign, utils.free, dict(utils.exec_modes))
    handlers = (old_iface.handlers, old_iface.evt_handlers, old_iface.tty_log)
    ledger = list(heap.blocks)
    if operation == 'transition':
        candidate_artifact(tmp_path, image)
        result = cdc.transition_to_cdc(old_proxy, utils, build_tag='live-test', immutable_ranges=[(0, len(image))], artifact_dir=tmp_path)
    else:
        result = cdc.CdcSession('/dev/kis-original', 'primary', old_iface, old_proxy, utils, identity).reconnect()
        assert result.reconnect_count == 1
    assert result.utils is utils and result.proxy is old_proxy and (result.iface is old_iface)
    assert utils.proxy.heap is heap and utils.heap is heap and (heap.blocks == ledger)
    assert buffers == (utils.code_buffer, utils.simd_buf, utils.glk_arg_buf)
    assert methods == (utils.malloc, utils.memalign, utils.free, utils.exec_modes)
    assert handlers == (old_iface.handlers, old_iface.evt_handlers, old_iface.tty_log)
    assert old_iface.dev is opened[-1].dev and (not old_iface.is_kis)
    new_address = utils.memalign(16384, 16384)
    assert new_address >= retained + 16384
    assert [e for e in events if e[0] == 'heapblock_alloc'] == [('heapblock_alloc', '/dev/kis-original')]
    assert len([e for e in events if e[0] == 'heap_limit']) == 1

@pytest.mark.parametrize('mismatch', ['image', 'adt', 'base', 'bootargs'])
def test_failed_identity_keeps_old_transport_and_real_allocation_ledger(monkeypatch, mismatch):
    utils, identity, _, events, opened, _ = live_utils(monkeypatch)
    utils.memalign(16384, 16384)
    old_iface = utils.iface
    old_dev = old_iface.dev
    old_proxy = utils.proxy
    ledger = list(utils.heap.blocks)
    ctor = cdc.UartInterface

    def wrong(path):
        iface = ctor(path)
        if mismatch == 'image':
            iface.image = iface.image[:-1] + b'!'
        if mismatch == 'adt':
            iface.adt = iface.adt[:-1] + b'!'
        if mismatch == 'base':
            iface.base += 16384
        if mismatch == 'bootargs':
            data = bytearray(iface.ba_bytes)
            data[32] ^= 1
            iface.ba_bytes = bytes(data)
        return iface
    monkeypatch.setattr(cdc, 'UartInterface', wrong)
    session = cdc.CdcSession('/dev/kis-original', 'primary', old_iface, old_proxy, utils, identity)
    with pytest.raises(cdc.CdcIdentityMismatch):
        session.reconnect()
    assert utils.iface is old_iface and old_proxy.iface is old_iface and (old_iface.dev is old_dev)
    assert utils.heap.blocks == ledger and old_proxy.heap is utils.heap
    assert opened[-1].closed
    assert len([e for e in events if e[0] == 'heap_limit']) == 1

def test_metadata_failure_closes_verified_candidate_before_retry_and_no_adoption(monkeypatch):
    utils, identity, _, _, opened, original = live_utils(monkeypatch)
    old_iface = utils.iface
    old_dev = old_iface.dev
    times = iter([0, 0, 1.1, 1.1])
    with pytest.raises(cdc.CdcDiscoveryTimeout):
        original(identity, utils=utils, timeout=1, ports=lambda: [port('/dev/cdc-new', cdc.CDC_VID, cdc.CDC_PID)], now=lambda: next(times), sleep=lambda _: None, speed_probe=lambda _: (_ for _ in ()).throw(OSError('speed unavailable')))
    assert opened[-1].closed and old_iface.dev is old_dev and (utils.proxy.iface is old_iface)

def test_unexpected_metadata_failure_also_closes_candidate_without_adoption(monkeypatch):
    utils, identity, _, _, opened, original = live_utils(monkeypatch)
    old_dev = utils.iface.dev
    with pytest.raises(ValueError, match='logger failed'):
        original(identity, utils=utils, timeout=1, now=lambda: 0, ports=lambda: [port('/dev/cdc-new', cdc.CDC_VID, cdc.CDC_PID)], log=lambda _: (_ for _ in ()).throw(ValueError('logger failed')))
    assert opened[-1].closed and utils.iface.dev is old_dev
