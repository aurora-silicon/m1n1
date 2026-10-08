import ast
import hashlib
import importlib.util
import json
import runpy
import struct
import textwrap
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest
from m1n1.hw.dart8110 import DART8110, DART8110Regs, PTE
from m1n1.fw.aop import j616s_capture as capture

REPO = Path(__file__).resolve().parents[2]
BASE = 0x513300000


def lp_payload(count=639, cursor=7680):
    data = bytearray(104)
    struct.pack_into("<II", data, 0, 0x6C706169, 0xC3000008)
    struct.pack_into("<Q", data, 0x40, count)
    struct.pack_into("<Q", data, 0x60, cursor)
    return bytes(data)


@pytest.mark.parametrize(
    "change", ["short", "extension", "header", "cursor", "overflow", "ring"]
)
def test_lp_raw_admission_rejects_unqualified_extent_header_and_cursor(change):
    data = lp_payload()
    ring = capture.LP_BYTES
    if change == "short":
        data = data[:-1]
    if change == "extension":
        data += b"x"
    if change == "header":
        data = b"xxxx" + data[4:]
    if change == "cursor":
        data = lp_payload(cursor=0)
    if change == "overflow":
        data = lp_payload(count=(1 << 64) - 1)
    if change == "ring":
        ring = True
    with pytest.raises(ValueError):
        capture.validate_lp_report(data, ring)


def test_lp_exact_counter_offsets_and_float_wave_layout_preview(tmp_path):
    assert capture.validate_lp_report(lp_payload()) == dict(
        frame_count=639, write_offset=7680, frame_bytes=12
    )
    raw = struct.pack("<6f", 0.01, 0.02, -0.03, 0.02, 0.04, -0.06) + b"12345678"
    output = tmp_path / "audio.wav"
    meta = capture.wave_files(raw, output, preview_channel=1)
    data = output.read_bytes()
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    assert struct.unpack_from("<HHIIHH", data, 20) == (3, 3, 48000, 576000, 12, 32)
    assert data[-24:] == raw[:24] and meta["tail_bytes"] == 8 and meta["frames"] == 2
    with wave.open(meta["preview"], "rb") as preview:
        assert (
            preview.getnchannels() == 1
            and preview.getsampwidth() == 2
            and preview.getnframes() == 2
        )
    assert meta["preview_gain"] > 1 and meta["preview_channel"] == 1
    with pytest.raises(ValueError):
        capture.wave_files(struct.pack("<3f", float("nan"), 0, 0), output)


def fixture(monkeypatch, *, failure=None, size=0x200000):
    cls = runpy.run_path(str(REPO / "tests/python/test_dart8110_mapping.py"))[
        "Hardware"
    ]
    reports = []
    descriptors = []
    allocations = []
    states = {"hpai": "idle", "lpai": "idle", "lai ": "idle"}
    published = [(1 << 64) - 1, 0]
    clock = [0.0]
    events = []
    records = []
    hp_pa = [None]

    class Hardware(cls):
        def read32(self, address):
            events.append(("read", address))
            if address == BASE + 0x14100:
                word = reports.pop(0)
                if not reports:
                    self.values[BASE + 0xC074] = 0x111
                return word
            if address == 0x50836C01C:
                return 4
            if address == 0x50836C014:
                return 45 * 24000000
            if address == 0x50836C010:
                return int(clock[0] * 24000000)
            return super().read32(address)

        def write32(self, address, value):
            events.append(("write", address, value))
            super().write32(address, value)
            if address == BASE + 0x14000:
                descriptors.append(value)
            if address == BASE + 0xC:
                self.values[BASE + 8] &= ~value

        def get_chipid(self):
            return 0x6040

        def get_exc_count(self):
            return 1 if failure == "descriptor_cpu" and descriptors else 0

        def memset32(self, address, value, length):
            self.pages[address] = bytes(length)
            events.append(("zero", address, length))

        def dc_cvac(self, address, length):
            events.append(("clean", address, length))

        def dc_ivac(self, address, length):
            events.append(("invalidate", address, length))

        def memalign(self, align, length):
            address = super().memalign(align, length)
            allocations.append((address, length))
            if length == size:
                hp_pa[0] = address
            return address

        def readmem(self, address, length):
            if hp_pa[0] is not None and hp_pa[0] <= address < hp_pa[0] + size:
                events.append(("download", address, length))
                if failure == "download":
                    raise TimeoutError("Read transport failed")
                data = self.pages[hp_pa[0]][
                    address - hp_pa[0] : address - hp_pa[0] + length
                ]
                return data[:-1] if failure == "short_copy" else data
            return super().readmem(address, length)

    hw = Hardware(True)
    hw.iface = hw
    hw.values.update(
        {
            8: (42 << 24) | (42 << 16) | (2 << 8),
            BASE + 0xC070: 0x100,
            BASE + 0xC074: 0x100,
            BASE + 0x98: 0x2000,
        }
    )
    dart = DART8110(hw, DART8110Regs(hw, 0), hw)
    dart.iomap_at(0, 0x10002000000, 0x10003000000, 0x4000)
    old_root = hw.values[0x1400]
    old_pages = hw.pages.copy()
    hw.next_page = 0x10008000000
    wdt = SimpleNamespace(get_reg=lambda index: (0x50836C000, 0x4000))
    mapper = SimpleNamespace(reg=10, getprop=lambda key: 263)
    node = SimpleNamespace(
        compatible=["admac,t604x"],
        _properties={"#dma-channels": 9},
        get_reg=lambda index: (BASE, 0x34000),
        getprop={
            "AAPL,phandle": 456,
            "iommu-parent": 263,
            "#dma-channels": 9,
        }.__getitem__,
    )
    adt = {
        "/chosen": SimpleNamespace(board_id=6),
        "/arm-io/wdt": wdt,
        "/arm-io/admac-leap-ns": node,
        "/arm-io/dart-aop/mapper-admac-leap-ns": mapper,
        "/arm-io/dart-aop": SimpleNamespace(get_reg=lambda index: (0, 0xC000)),
    }
    service = SimpleNamespace(
        last_producer_report=None, last_producer_report_payload=None
    )

    def barrier(command):
        if failure == "tail" and hp_pa[0] is not None:
            tail = hp_pa[0] + size - 0x4000
            matches = [
                (table, index)
                for table, entries in dart.pt_cache.items()
                for index, word in enumerate(entries)
                if PTE(word).VALID and PTE(word).OFFSET << 14 == tail
            ]
            assert len(matches) == 1
            table, index = matches[0]
            data = bytearray(hw.pages[table])
            data[index * 8 : index * 8 + 8] = bytes(8)
            hw.pages[table] = bytes(data)

    u = SimpleNamespace(
        proxy=hw,
        iface=hw,
        adt=adt,
        read=hw.read,
        write=hw.write,
        memalign=hw.memalign,
        exec=barrier,
    )
    obj = capture.J616sHPCapture(
        u,
        size=size,
        clock=lambda: clock[0],
        announce=lambda *args, **kw: events.append(("announce",)),
        persist=lambda state: records.append(dict(state)),
    )
    obj.state.update(profile_verified=True, rtkit_version=12)

    def send(call, version):
        assert version == 2
        name = type(call).__name__
        identifier = call.args.devid
        events.append((name, identifier, call.args.get("modifier")))
        if name == "AudioAttachDevice":
            return SimpleNamespace(retcode=0)
        mod = call.args.modifier
        if name == "SetDeviceProp":
            if mod == 300:
                published[:] = struct.unpack("<QQ", call.args.data)
            else:
                payload = call.args.data
                if identifier == "lai ":
                    payload = capture.PowerSetting.parse(payload)
                target = payload.target_pstate
                states[identifier] = target
                events.append(("power", identifier, target))
                if identifier == "lpai" and target == "runn":
                    service.last_producer_report_payload = lp_payload()
                if identifier == "hpai" and target == "pwrd":
                    assert len(descriptors) == 4 and hw.values[BASE + 8] == 1
                    if failure != "completion_timeout":
                        hw.values[BASE + 0xC070] = 0x111
                        hw.values[BASE + 0xC074] = 1
                        reports.extend([123, 0, 0, 1])
                        hw.pages[hp_pa[0]] = struct.pack(
                            "<3f", 0.001, -0.002, 0.003
                        ) * (size // 12) + bytes(size % 12)
            return SimpleNamespace(retcode=0)
        if mod == 300:
            return SimpleNamespace(
                retcode=0, len=16, data=struct.pack("<QQ", *published)
            )
        return SimpleNamespace(
            retcode=0,
            len=4,
            data=(
                capture.FourCC.build(states[identifier])
                if identifier == "lai "
                else states[identifier]
            ),
        )

    endpoint = SimpleNamespace(serv_map={"aop-audio": service}, send_roundtrip=send)

    def work():
        clock[0] += 0.01

    asc = SimpleNamespace(
        work=work,
        epmap={
            0x22: endpoint,
            0x20: SimpleNamespace(
                serv_map={name: object() for name in capture.SERVICES - {"aop-audio"}}
            ),
        },
    )
    wrapper = SimpleNamespace(dart=dart)
    return obj, u, asc, wrapper, hw, dart, events, records, hp_pa, old_root, old_pages


@pytest.mark.parametrize("size", [0x200000, 0x600000])
def test_complete_warm_report_owned_map_DMA_before_power_full_copies_and_input_stop(
    monkeypatch, size
):
    obj, u, asc, wrapper, hw, dart, events, records, pa, root, pages = fixture(
        monkeypatch, size=size
    )
    startup = {}
    obj(asc, wrapper, startup)
    assert (
        obj.state["stable"] and obj.state["hp_restored_idle"] and len(obj.data) == size
    )
    assert hw.values[0x1400] == root and all(
        hw.pages[address] == data for address, data in pages.items()
    )
    assert hw.values[0xC00] == 0x501
    assert events.index(("write", BASE + 8, 1)) < events.index(
        ("power", "hpai", "pwrd")
    )
    assert [e[1:] for e in events if e[0] == "download"] == [
        (pa[0] + offset, min(0x10000, size - offset))
        for _ in range(2)
        for offset in range(0, size, 0x10000)
    ]
    assert (
        obj.state["lp_report"]["write_offset"] == 7680
        and startup["hp_capture"] is obj.state
    )
    assert obj.state["retained"] and not obj.state["uncertain"]
    dart.invalidate_cache()
    for offset in range(0, size, 0x4000):
        assert dart.iotranslate(10, pa[0] + offset, 0x4000) == [
            (pa[0] + offset, 0x4000)
        ]


@pytest.mark.parametrize(
    "failure",
    ["tail", "descriptor_cpu", "download", "short_copy", "completion_timeout"],
)
def test_capture_failure_has_no_receiver_or_power_cleanup_and_is_terminal(
    monkeypatch, failure
):
    obj, u, asc, wrapper, hw, dart, events, records, *_ = fixture(
        monkeypatch, failure=failure
    )
    with pytest.raises((RuntimeError, TimeoutError)):
        obj(asc, wrapper, {})
    assert obj.state["uncertain"] and obj.state["retained"]
    assert ("write", BASE + 0xC, 1) not in events and (
        "power",
        "hpai",
        "idle",
    ) not in events
    if failure == "tail":
        assert not hw.values[0xC00] & 0x400
    count = len(events)
    with pytest.raises(RuntimeError):
        obj(asc, wrapper, {})
    assert len(events) == count


def test_cold_probe_import_safe_and_actual_handoff_retention_finally(tmp_path):
    path = REPO / "proxyclient/experiments/t6040_iop.py"
    spec = importlib.util.spec_from_file_location("cold_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert not hasattr(module, "p") and not hasattr(module, "u")
    source = path.read_text()
    start = source.index("    finally:\n        if result['resources_retained']")
    stop = source.index("    # Preserve both failures", start)
    final = source[start:stop].split("    finally:\n", 1)[1]

    class NoRPC:
        def __getattr__(self, name):
            raise AssertionError("Unexpected cleanup RPC " + name)

    result = {"resources_retained": True}
    exec(
        compile(textwrap.dedent(final), "actual retained cold finally", "exec"),
        dict(
            result=result,
            json=json,
            print=lambda *a: None,
            p=NoRPC(),
            asc=NoRPC(),
            dart=NoRPC(),
        ),
    )
    assert result["reboot_required"] and "Caller owns AOP" in result["cleanup_skipped"]
    assert (
        "on_aop_ready(asc, wrapper, result)" in source
        and "on_aop_ready.prepare_asc(asc)" in source
    )
    assert "proxy=p, utils=u" in source


@pytest.mark.parametrize("changed", [False, True])
def test_profile_fingerprint_uses_advertised_VA_to_PA_and_rejects_resident_change(
    monkeypatch, changed
):
    obj, u, _, _, hw, _, events, *_ = fixture(monkeypatch)
    virtual = 0x1000000
    physical = 0x510C10000
    window = b"bound native code"
    monkeypatch.setattr(
        capture,
        "FINGERPRINTS",
        ((virtual + 0x400, len(window), hashlib.sha256(window).hexdigest()),),
    )
    u.adt["/arm-io/aop/iop-aop-nub"] = SimpleNamespace(
        getprop=lambda name: (
            "__TEXT;__DATA;__ETEXT;__OS_LOG"
            if name == "segment-names"
            else b"".join(
                struct.pack("<QQQII", *row)
                for row in (
                    (physical, virtual, physical, 0xD9000, 3),
                    (0x510CE9000, 0x10D9000, 0x510CE9000, 0x10F000, 6),
                    (0x100000000, 0x11E8000, 1 << 40, 0x14000, 1),
                    (0x100100000, 0xFD000000, 0x100100000, 0x2A000, 10),
                )
            )
        )
    )
    u.adt["/arm-io/aop"] = SimpleNamespace(
        get_reg=lambda index: (0x510C00000, 0x2F0000)
    )
    readmem = u.iface.readmem

    def read(address, size):
        if address == physical + 0x400:
            events.append(("firmware", address, size))
            return window[:-1] + b"x" if changed else window
        return readmem(address, size)

    u.iface.readmem = read
    if changed:
        with pytest.raises(RuntimeError, match="firmware/profile differs"):
            obj.validate_profile()
        assert obj.state["uncertain"]
    else:
        obj.validate_profile()
        assert obj.state["profile_verified"]
    assert ("firmware", physical + 0x400, len(window)) in events


def test_management_version_gate_preserves_real_handler_only_for_RTKit12(monkeypatch):
    obj, *_ = fixture(monkeypatch)
    calls = []
    asc = SimpleNamespace(
        mgmt=SimpleNamespace(
            msghandler={1: lambda msg: calls.append(msg.MAX_VER) or True}
        )
    )
    obj.prepare_asc(asc)
    assert asc.mgmt.msghandler[1](SimpleNamespace(MIN_VER=12, MAX_VER=12))
    assert calls == [12]
    with pytest.raises(RuntimeError, match="RTKit version 12"):
        asc.mgmt.msghandler[1](SimpleNamespace(MIN_VER=12, MAX_VER=13))
    assert calls == [12] and obj.state["uncertain"]


def test_wrong_board_rejected_before_watchdog_or_firmware_access(monkeypatch):
    obj, u, _, _, _, _, events, *_ = fixture(monkeypatch)
    u.adt["/chosen"].board_id = 5
    count = len(events)
    with pytest.raises(ValueError):
        capture.J616sHPCapture(u)
    assert len(events) == count


def test_profile_short_and_extended_raw_reports_do_not_satisfy_readiness(monkeypatch):
    obj, u, asc, wrapper, _, _, events, *_ = fixture(monkeypatch)
    send = asc.epmap[0x22].send_roundtrip

    def changed(call, version):
        result = send(call, version)
        service = asc.epmap[0x22].serv_map["aop-audio"]
        if service.last_producer_report_payload is not None:
            service.last_producer_report_payload += b"extension"
        return result

    asc.epmap[0x22].send_roundtrip = changed
    with pytest.raises(ValueError, match="104-byte"):
        obj(asc, wrapper, {})
    assert obj.state["uncertain"] and ("write", BASE + 8, 1) not in events


@pytest.mark.parametrize("failed", ["hello", "callback", None])
def test_actual_cold_probe_handoff_keeps_resources_on_success_and_failure(
    monkeypatch, failed
):
    path = REPO / "proxyclient/experiments/t6040_iop.py"
    spec = importlib.util.spec_from_file_location("cold_capture_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls = runpy.run_path(str(REPO / "tests/python/test_dart8110_mapping.py"))[
        "Hardware"
    ]
    hw = cls(False)
    hw.values[8] = (42 << 24) | (42 << 16) | (2 << 8)
    hw.get_chipid = lambda: 0x6040
    hw.dc_cvac = lambda *a: None
    hw.nop = lambda: pytest.fail("Unexpected post-handoff RPC")
    base = 0x510D00000
    hw.values[base + 0x8114] = 1 << 17
    d = DART8110(hw, DART8110Regs(hw, 0), hw)
    wrapper = SimpleNamespace(dart=d)
    monkeypatch.setattr(module.DART, "from_adt", lambda *a, **kw: wrapper)
    config = SimpleNamespace(r0h=3, r0l=1, r4=0, start=0, end=0xFFF, r20=0)
    dapf_base = 0x100000

    def dapf(path, index):
        hw.values.update(
            {
                dapf_base: 0x31,
                dapf_base + 4: 0,
                dapf_base + 8: 0,
                dapf_base + 12: 0,
                dapf_base + 16: 0xFFF,
                dapf_base + 20: 0,
                dapf_base + 32: 0,
            }
        )
        return 0

    hw.dapf_init = dapf
    hw.read64 = lambda address: hw.values.get(address, 0)
    adt = {
        "/chosen": SimpleNamespace(board_id=6),
        "/arm-io/aop": SimpleNamespace(get_reg=lambda index: (base, 0x88000)),
        "/arm-io/dart-aop": SimpleNamespace(
            get_reg=lambda index: (0, 0xC000) if index == 0 else (dapf_base, 0x4000),
            getprop=lambda name: [config],
            __getitem__=None,
        ),
        "/arm-io/aop/iop-aop-nub": SimpleNamespace(
            getprop=lambda name: struct.pack(
                "<QQQII", 0x10002000000, 0x1000000, 1 << 40, 0x4000, 1
            )
        ),
    }

    class DartNode:
        def get_reg(self, index):
            return (0, 0xC000) if index == 0 else (dapf_base, 0x4000)

        def getprop(self, name):
            return [config]

        def __getitem__(self, name):
            return SimpleNamespace(reg=0)

    adt["/arm-io/dart-aop"] = DartNode()
    u = SimpleNamespace(adt=adt, proxy=hw, exec=lambda value: None, iface=hw)
    import m1n1.fw.aop.base as aop_base
    import m1n1.fw.aop.client as aop_client

    class BootArgs:
        def to_bytes(self):
            return b"bootargs"

        def update(self, values):
            assert values == {"p0CE": 1 << 40}

    class Base:
        _bootargs_span = (0x200000, 8)

        def __init__(self, u):
            pass

        def read_bootargs(self):
            return BootArgs()

        def write_bootargs(self, args):
            pass

    monkeypatch.setattr(aop_base, "AOPBase", Base)
    names = {
        0x20: "SPUApp",
        0x21: "wakehint",
        0x22: "aop-audio",
        0x23: "aop-voicetrigger",
        0x24: "accel",
        0x25: "gyro",
        0x26: "las",
        0x27: "als",
    }
    monkeypatch.setattr(aop_client.AOPClient, "ENDPOINTS", {ep: object for ep in names})
    events = []

    class ASC:
        def __init__(self, *a, **kw):
            self.eps = list(names)
            self.epmap = {}
            self.epcls = {}
            self.mgmt = SimpleNamespace(
                msghandler={1: lambda msg: True},
                iop_power_state=0x20,
                ap_power_state=0x20,
                ping=lambda: self.mgmt.msghandler[4](None),
            )

        def start(self):
            self.mgmt.msghandler[1](
                SimpleNamespace(MIN_VER=12, MAX_VER=13 if failed == "hello" else 12)
            )

        def start_ep(self, ep):
            service = SimpleNamespace(last_report=SimpleNamespace(angle=99))
            self.epmap[ep] = SimpleNamespace(
                started=True, serv_map={names[ep]: service}
            )

        def work(self):
            pass

    monkeypatch.setattr(module, "StandardASC", ASC)

    class Owner:
        def prepare_asc(self, asc):
            events.append("prepare")

            def hello(message):
                if message.MAX_VER != 12:
                    raise RuntimeError("Bad management version")
                return True

            asc.mgmt.msghandler[1] = hello

        def __call__(self, asc, actual_wrapper, result):
            events.append("callback")
            assert actual_wrapper is wrapper
            if failed == "callback":
                raise RuntimeError("Capture ambiguity")
            result["owned_input"] = True

    if failed:
        with pytest.raises(RuntimeError):
            module.probe("aop", proxy=hw, utils=u, on_aop_ready=Owner())
    else:
        result = module.probe("aop", proxy=hw, utils=u, on_aop_ready=Owner())
        assert (
            result["owned_input"]
            and result["resources_retained"]
            and result["reboot_required"]
        )
    assert events == (["prepare"] if failed == "hello" else ["prepare", "callback"])


@pytest.mark.parametrize("failed", ["profile", "capture", None])
def test_public_CLI_one_negotiated_connection_local_outputs_and_host_close(
    tmp_path, monkeypatch, failed
):
    path = REPO / "proxyclient/experiments/aop_capture.py"
    spec = importlib.util.spec_from_file_location("public_aop_capture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import m1n1.proxy as proxy
    import m1n1.proxyutils as utils

    events = []
    output = tmp_path / "voice.wav"
    iface = SimpleNamespace(
        dev=SimpleNamespace(close=lambda: events.append("close")),
        nop=lambda: events.append("iface_nop"),
    )
    p = SimpleNamespace(
        nop=lambda: events.append("proxy_nop"),
        get_chipid=lambda: 0x6040,
        wdt_arm=lambda value: events.append(("wdt", value)) or 0,
    )
    u = SimpleNamespace(adt={"/chosen": SimpleNamespace(board_id=6)})
    monkeypatch.setattr(
        proxy, "UartInterface", lambda device: events.append(("open", device)) or iface
    )
    monkeypatch.setattr(proxy, "M1N1Proxy", lambda actual: events.append("proxy") or p)
    monkeypatch.setattr(
        utils, "bootstrap_port", lambda *args: events.append("bootstrap")
    )
    monkeypatch.setattr(utils, "ProxyUtils", lambda actual: u)

    class Capture:
        def __init__(self, u, **kwargs):
            self.persist = kwargs["persist"]
            self.state = {"retained": True}
            self.data = None

        def validate_profile(self):
            events.append("profile")
            if failed == "profile":
                raise RuntimeError("Unsupported profile")

    def cold_probe(device, **kwargs):
        assert device == "aop" and kwargs["proxy"] is p and kwargs["utils"] is u
        owner = kwargs["on_aop_ready"]
        events.append("cold_capture")
        if failed == "capture":
            raise RuntimeError("Uncertain capture")
        owner.data = struct.pack("<6f", 0.01, 0.02, 0.03, 0.02, 0.04, 0.06)
        owner.state["stable"] = True

    monkeypatch.setattr(module, "J616sHPCapture", Capture)
    monkeypatch.setattr(module, "probe", cold_probe)
    monkeypatch.setattr(
        __import__("sys"), "argv", ["aop_capture", str(output), "--device", "fake"]
    )
    if failed:
        with pytest.raises(RuntimeError):
            module.main()
        assert not output.exists()
    else:
        module.main()
        assert output.exists() and output.with_suffix(".bin").exists()
        assert output.with_name("voice-preview.wav").exists()
    assert events[0] == ("open", "fake") and events[1:3] == ["iface_nop", "proxy"]
    assert events[-1] == "close" and events.count(("open", "fake")) == 1
    assert output.with_suffix(".json").exists()


def test_RAW_report_service_retains_full_known_payload_without_changing_fallback():
    from io import BytesIO
    from m1n1.fw.aop.aopep import AOPAudioService

    service = AOPAudioService.__new__(AOPAudioService)
    data = lp_payload()
    assert service.handle_report(0, 0x20, 1, BytesIO(data))
    assert (
        service.last_producer_report_payload == data
        and service.last_producer_report.frame_count == 639
    )


@pytest.mark.parametrize(
    "changed", ["pa", "sram", "flags", "name", "extent", "channels"]
)
def test_profile_bounds_reject_bad_ADT_before_any_firmware_download(
    monkeypatch, changed
):
    obj, u, _, _, hw, _, events, *_ = fixture(monkeypatch)
    rows = [
        (0x510C10000, 0x1000000, 0x510C10000, 0xD9000, 3),
        (0x510CE9000, 0x10D9000, 0x510CE9000, 0x10F000, 6),
        (0x100000000, 0x11E8000, 1 << 40, 0x14000, 1),
        (0x100100000, 0xFD000000, 0x100100000, 0x2A000, 10),
    ]
    if changed == "pa":
        rows[0] = (0x700000000, *rows[0][1:])
    if changed == "flags":
        rows[0] = (*rows[0][:4], 6)
    names = "changed" if changed == "name" else "__TEXT;__DATA;__ETEXT;__OS_LOG"
    u.adt["/arm-io/aop/iop-aop-nub"] = SimpleNamespace(
        getprop=lambda name: (
            names
            if name == "segment-names"
            else b"".join(struct.pack("<QQQII", *r) for r in rows)
        )
    )
    u.adt["/arm-io/aop"] = SimpleNamespace(
        get_reg=lambda index: (
            (0x700000000, 0x2F0000) if changed == "sram" else (0x510C00000, 0x2F0000)
        )
    )
    if changed == "extent":
        monkeypatch.setattr(capture, "FINGERPRINTS", ((0x10D8FFF, 64, "0" * 64),))
    if changed == "channels":
        node = u.adt["/arm-io/admac-leap-ns"]
        getprop = node.getprop
        node.getprop = lambda name: 13 if name == "#dma-channels" else getprop(name)
    calls = []
    u.iface.readmem = lambda *args: calls.append(args) or b""
    with pytest.raises(RuntimeError):
        obj.validate_profile()
    assert calls == [] and obj.state["uncertain"]


@pytest.mark.parametrize("failure", ["short_lp", "attach_release"])
def test_exact_published_LP_extent_and_post_attach_ownership_before_power(
    monkeypatch, failure
):
    obj, u, asc, wrapper, hw, _, events, *_ = fixture(monkeypatch)
    endpoint = asc.epmap[0x22]
    send = endpoint.send_roundtrip
    attached = [False]

    def changed(call, version):
        reply = send(call, version)
        if type(call).__name__ == "AudioAttachDevice" and call.args.devid == "adpx":
            attached[0] = True
        if type(call).__name__ == "GetDeviceProp" and call.args.modifier == 300:
            address, size = struct.unpack("<QQ", reply.data)
            if size and (failure == "short_lp" or attached[0]):
                reply.data = struct.pack(
                    "<QQ", address, size - 12 if failure == "short_lp" else 0
                )
        return reply

    endpoint.send_roundtrip = changed
    with pytest.raises(RuntimeError):
        obj(asc, wrapper, {})
    assert not any(e[0] == "power" for e in events) and obj.state["uncertain"]


def test_typed_power_reply_with_extended_declared_extent_is_rejected(monkeypatch):
    from m1n1.fw.aop.ipc import DevicePropertyReply

    obj, *_ = fixture(monkeypatch)
    raw = struct.pack("<II", 0, 8) + b"eldi" + b"extra"[:4]
    reply = DevicePropertyReply.parse(raw, devid="hpai", modifier=200)
    assert reply.data == "idle" and reply.len == 8
    obj.query = lambda call: reply
    with pytest.raises(RuntimeError, match="power property extent"):
        obj.get_state("hpai")
    assert obj.state["uncertain"]


@pytest.mark.parametrize("suffix", [".wav", ".json", ".bin", "-preview.wav"])
@pytest.mark.parametrize("symlink", [False, True])
def test_CLI_existing_or_dangling_output_rejected_before_UART(
    tmp_path, monkeypatch, suffix, symlink
):
    path = REPO / "proxyclient/experiments/aop_capture.py"
    spec = importlib.util.spec_from_file_location("safe_public_output", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "voice.wav"
    occupied = tmp_path / ("voice" + suffix)
    if symlink:
        occupied.symlink_to(tmp_path / "missing")
    else:
        occupied.write_bytes(b"existing evidence")
    import m1n1.proxy as proxy

    monkeypatch.setattr(
        proxy, "UartInterface", lambda *args: pytest.fail("UART before output gate")
    )
    monkeypatch.setattr(__import__("sys"), "argv", ["aop_capture", str(output)])
    with pytest.raises(SystemExit):
        module.main()
    if not symlink:
        assert occupied.read_bytes() == b"existing evidence"


@pytest.mark.parametrize("state", ["idle", "runn"])
def test_actual_generic_LAI_state_reply_decodes_exact_FourCC(monkeypatch, state):
    from m1n1.fw.aop.ipc import DevicePropertyReply

    obj, *_ = fixture(monkeypatch)
    raw = struct.pack("<II", 0, 4) + capture.FourCC.build(state)
    reply = DevicePropertyReply.parse(raw, devid="lai ", modifier=200)
    assert isinstance(reply.data, bytes) and len(reply.data) == 4
    obj.query = lambda call: reply
    assert obj.get_state("lai ") == state and not obj.state["uncertain"]


@pytest.mark.parametrize(
    "identifier, length, payload",
    [
        ("lai ", 8, b"nurrxxxx"),
        ("lai ", 4, b"\xff\xff\xff\xff"),
        ("hpai", 8, b"drwpxxxx"),
        ("lpai", 8, b"nurrxxxx"),
    ],
)
def test_actual_state_reply_extent_and_invalid_LAI_ascii_fail_closed(
    monkeypatch, identifier, length, payload
):
    from m1n1.fw.aop.ipc import DevicePropertyReply

    obj, *_ = fixture(monkeypatch)
    reply = DevicePropertyReply.parse(
        struct.pack("<II", 0, length) + payload, devid=identifier, modifier=200
    )
    obj.query = lambda call: reply
    with pytest.raises((RuntimeError, UnicodeDecodeError)):
        obj.get_state(identifier)
    assert obj.state["uncertain"]


def test_LAI_raw_support_does_not_loosen_typed_or_other_devices(monkeypatch):
    obj, *_ = fixture(monkeypatch)
    obj.query = lambda call: SimpleNamespace(len=4, data=b"drwp")
    with pytest.raises(RuntimeError, match="typed power-state"):
        obj.get_state("hpai")
    obj, *_ = fixture(monkeypatch)
    obj.query = lambda call: pytest.fail("Unknown device queried")
    with pytest.raises(RuntimeError, match="Unknown power-state"):
        obj.get_state("pdm0")
