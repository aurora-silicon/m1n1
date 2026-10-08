# SPDX-License-Identifier: MIT
"""One cold J616s HP capture using the resident Apple program and NS RX0.

Invoke through t6040_iop's ownership handoff with an armed 45-second watchdog.
There is no restart/unmap path: buffers and AOP remain owned until watchdog
recovery, including after a successful input stop. No output codecs are touched.
"""

import hashlib
import math
import struct
import time
from array import array
from pathlib import Path
from construct import Container

from ...utils import FourCC

from ...hw.admac import ADMAC, ADMACDescriptor, E_BUSWIDTH, E_FRAME
from ...hw.dart8110 import DART8110
from .ipc import AudioAttachDevice, GetDeviceProp, SetDeviceProp, PowerSetting

PAGE = 0x4000
RATE = 48000
CHANNELS = 3
FRAME_BYTES = CHANNELS * 4
SIZES = (0x200000, 0x600000)
LP_BYTES = 768000
SERVICES = {
    "SPUApp",
    "wakehint",
    "aop-audio",
    "aop-voicetrigger",
    "accel",
    "gyro",
    "als",
    "las",
}
# Immutable native code and the complete default HP program, not mutable bootargs.
FINGERPRINTS = (
    (0x10405A0, 64, "40ea65315a4765e491503ca7911c320c4823c0b7532c84a4da89ec62d34e6203"),
    (0x10858E8, 64, "70185f567385437925aff3afc7d0584e7d30d634edebe551d8c0b9c840a25303"),
    (
        0x10CE344,
        0x3360,
        "baf5935d5ce5a6ca0fdf479de05df59914e7a2dd25f28e0395d8062fa44a9362",
    ),
)


def validate_lp_report(payload, ring_bytes=LP_BYTES):
    """Validate the observed report extent/header and cursor, not LP sample type."""
    if not isinstance(payload, bytes) or len(payload) != 0x68:
        raise ValueError("Expected the 104-byte LP producer report")
    if struct.unpack_from("<II", payload) != (0x6C706169, 0xC3000008):
        raise ValueError("Unexpected LP producer report header")
    if type(ring_bytes) is not int or ring_bytes <= 0 or ring_bytes % 12:
        raise ValueError("Invalid LP ring extent")
    count = struct.unpack_from("<Q", payload, 0x40)[0]
    cursor = struct.unpack_from("<Q", payload, 0x60)[0]
    total = (count + 1) * 12
    if total >= 1 << 64 or cursor != total % ring_bytes:
        raise ValueError("LP counter/cursor does not match the 12-byte frame")
    return dict(frame_count=count, write_offset=cursor, frame_bytes=12)


def wave_files(data, output, *, preview_channel=0):
    """Preserve Float32 frames and produce a separately normalized mono preview.

    The preview can amplify hiss. Its channel/gain metadata is a listening aid,
    not evidence of speech or independent physical microphone channels.
    """
    if type(preview_channel) is not int or not 0 <= preview_channel < CHANNELS:
        raise ValueError("Preview channel must be 0..2")
    complete = len(data) - len(data) % FRAME_BYTES
    if not complete:
        raise ValueError("No complete Float32 frame")
    values = array("f")
    values.frombytes(data[:complete])
    if struct.pack("=I", 1) != struct.pack("<I", 1):
        values.byteswap()
    if not all(math.isfinite(value) for value in values):
        raise ValueError(
            "Nonfinite capture; preserve raw evidence without WAV interpretation"
        )
    frames = complete // FRAME_BYTES
    fmt = struct.pack("<HHIIHH", 3, CHANNELS, RATE, RATE * FRAME_BYTES, FRAME_BYTES, 32)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"fact" + struct.pack("<II", 4, frames)
    chunks += b"data" + struct.pack("<I", complete) + data[:complete]
    Path(output).write_bytes(
        b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks
    )
    lane = values[preview_channel::CHANNELS]
    peak = max(abs(value) for value in lane)
    gain = 0.95 / peak if peak else 1.0
    pcm = array(
        "h", (max(-32768, min(32767, round(value * gain * 32767))) for value in lane)
    )
    if struct.pack("=I", 1) != struct.pack("<I", 1):
        pcm.byteswap()
    import wave

    preview = Path(output).with_name(Path(output).stem + "-preview.wav")
    with wave.open(str(preview), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm.tobytes())
    return dict(
        frames=frames,
        seconds=frames / RATE,
        channels=CHANNELS,
        rate=RATE,
        tail_bytes=len(data) - complete,
        preview_channel=preview_channel,
        preview_gain=gain,
        preview=str(preview),
    )


class J616sHPCapture:
    def __init__(
        self,
        u,
        *,
        size=0x600000,
        persist=lambda state: None,
        announce=print,
        clock=time.monotonic
    ):
        if type(size) is not int or size not in SIZES:
            raise ValueError("Only the qualified 2 MiB and 6 MiB windows are supported")
        self.u, self.p = u, u.proxy
        self.size, self.persist, self.announce, self.clock = (
            size,
            persist,
            announce,
            clock,
        )
        self.state = dict(operations=[], uncertain=False, retained=True, size=size)
        self.wdt = u.adt["/arm-io/wdt"].get_reg(0)[0]
        if u.adt["/chosen"].board_id != 6 or self.wdt != 0x50836C000:
            raise ValueError("Requires J616s board 6 and its advertised watchdog")
        self.started = False
        self.data = None

    def require(self, condition, message):
        if not condition:
            self.state["uncertain"] = True
            raise RuntimeError(message)

    def guard(self, minimum=3):
        p = self.p
        if p.get_exc_count():
            raise RuntimeError("CPU exception; retain resources")
        words = []
        for offset in (0x1C, 0x14, 0x10):
            words.append(p.read32(self.wdt + offset))
            if p.get_exc_count():
                raise RuntimeError("Watchdog read CPU exception")
        if not words[0] & 4 or (words[1] - words[2]) / 24000000 < minimum:
            raise RuntimeError("Insufficient armed watchdog headroom")

    def rpc(self, operation, function, *, minimum=3):
        if self.state["uncertain"]:
            raise RuntimeError("Terminal capture uncertainty; no further RPC")
        record = dict(operation=operation, pending=True)
        self.state["operations"].append(record)
        self.state["uncertain"] = True
        try:
            self.persist(self.state)
            self.guard(minimum)
            value = function()
            if self.p.get_exc_count():
                raise RuntimeError("CPU exception during " + operation)
            record["pending"] = False
            if isinstance(value, bytes):
                record["bytes"] = len(value)
            elif value is None or isinstance(value, (int, bool, str)):
                record["value"] = value
            self.state["uncertain"] = False
            self.persist(self.state)
            return value
        except Exception as error:
            self.state.update(
                uncertain=True,
                error=str(error),
                recovery="Retain resources until watchdog recovery",
            )
            try:
                self.persist(self.state)
            except Exception:
                pass
            raise

    def validate_profile(self):
        self.require(self.rpc("chip", self.p.get_chipid) == 0x6040, "Requires T6040")
        adt = self.u.adt
        self.require(
            adt["/chosen"].board_id == 6 and self.wdt == 0x50836C000,
            "Requires the J616s watchdog and board",
        )
        node = adt["/arm-io/admac-leap-ns"]
        self.require(
            node.get_reg(0) == (0x513300000, 0x34000)
            and node.getprop("AAPL,phandle") == 456
            and "admac,t604x" in node.compatible
            and node.getprop("#dma-channels") == 9,
            "NS LEAP aperture/parent changed",
        )
        mapper = adt["/arm-io/dart-aop/mapper-admac-leap-ns"]
        self.require(
            mapper.reg == 10
            and node.getprop("iommu-parent") == mapper.getprop("AAPL,phandle"),
            "Requires the advertised stream 10 NS RX0",
        )
        segments = list(
            struct.iter_unpack(
                "<QQQII", adt["/arm-io/aop/iop-aop-nub"].getprop("segment-ranges")
            )
        )
        nub = adt["/arm-io/aop/iop-aop-nub"]
        self.require(
            nub.getprop("segment-names") == "__TEXT;__DATA;__ETEXT;__OS_LOG"
            and len(segments) == 4,
            "Firmware segment profile changed",
        )
        text = (0x510C10000, 0x1000000, 0x510C10000, 0xD9000, 3)
        self.require(
            segments[0] == text
            and adt["/arm-io/aop"].get_reg(2) == (0x510C00000, 0x2F0000),
            "Local firmware SRAM profile changed",
        )
        reads = []
        for va, length, expected in FINGERPRINTS:
            physical = text[0] + va - text[1]
            self.require(
                text[1] <= va
                and va + length <= text[1] + text[3]
                and 0x510C00000 <= physical
                and physical + length <= 0x510EF0000,
                "Immutable fingerprint is outside the TEXT/SRAM extent",
            )
            reads.append((va, physical, length, expected))
        self.state["firmware_fingerprints"] = []
        for va, physical, length, expected in reads:
            data = self.rpc(
                "firmware fingerprint", lambda: self.u.iface.readmem(physical, length)
            )
            actual = hashlib.sha256(data).hexdigest()
            self.state["firmware_fingerprints"].append(
                dict(va=hex(va), bytes=length, sha256=actual)
            )
            self.require(
                len(data) == length and actual == expected,
                "Resident default HP firmware/profile differs",
            )
        self.require(PowerSetting.sizeof() == 48, "Unexpected power property ABI")
        self.state["profile_verified"] = True

    def prepare_asc(self, asc):
        self.require(
            self.state.get("profile_verified"), "Profile verification precedes boot"
        )
        original = asc.mgmt.msghandler[1]

        def hello(message):
            if (message.MIN_VER, message.MAX_VER) != (12, 12):
                self.state["uncertain"] = True
                raise RuntimeError("Only RTKit version 12 is qualified")
            self.state["rtkit_version"] = 12
            return original(message)

        asc.mgmt.msghandler[1] = hello

    def __call__(self, asc, wrapper, startup):
        try:
            self.require(
                not self.started
                and self.state.get("profile_verified")
                and self.state.get("rtkit_version") == 12,
                "Validated one-shot cold startup required",
            )
            self.started = True
            self.asc, self.wrapper, self.dart = asc, wrapper, wrapper.dart
            self.require(
                isinstance(self.dart, DART8110)
                and self.dart.regs._base
                == self.u.adt["/arm-io/dart-aop"].get_reg(0)[0],
                "Requires the advertised DART8110 backend",
            )
            params = self.rpc(
                "DART address profile", lambda: self.dart.regs.PARAMS_8.reg
            )
            self.require(
                (params.PA_WIDTH, params.VA_WIDTH, params.VERS_MAJ) == (42, 42, 2),
                "Requires the 42-bit revision 2 DART profile",
            )
            services = {
                name
                for endpoint in asc.epmap.values()
                for name in getattr(endpoint, "serv_map", {})
            }
            self.require(services == SERVICES, "AOP service profile changed")
            self.endpoint = asc.epmap[0x22]
            self.service = self.endpoint.serv_map["aop-audio"]
            self.warm_lp()
            self.capture_hp()
            startup["hp_capture"] = self.state
        except Exception:
            self.state.update(
                uncertain=True, recovery="Retain resources until watchdog recovery"
            )
            startup["hp_capture"] = self.state
            try:
                self.persist(self.state)
            except Exception:
                pass
            raise

    def query(self, call, *, minimum=3):
        def send():
            work = self.asc.work
            deadline = self.clock() + 4

            def bounded_work():
                if self.clock() >= deadline:
                    raise TimeoutError("AOP audio reply")
                self.guard()
                return work()

            self.asc.work = bounded_work
            try:
                reply = self.endpoint.send_roundtrip(call, version=2)
                if reply.retcode:
                    raise RuntimeError("AOP audio request rejected")
                return reply
            finally:
                self.asc.work = work

        return self.rpc("audio property", send, minimum=minimum)

    def get_state(self, identifier):
        self.require(
            identifier in ("hpai", "lpai", "lai "), "Unknown power-state device"
        )
        reply = self.query(GetDeviceProp(devid=identifier, modifier=200))
        self.require(reply.len == 4, "Malformed power property extent")
        if identifier == "lai ":
            # This device uses the generic byte reply; HP/LP have typed FourCC replies.
            self.require(
                isinstance(reply.data, bytes) and len(reply.data) == 4,
                "Malformed LAI power-state payload",
            )
            try:
                state = FourCC.parse(reply.data)
                self.require(state.isascii(), "Malformed LAI power-state encoding")
                return state
            except Exception:
                self.state["uncertain"] = True
                raise
        self.require(isinstance(reply.data, str), "Malformed typed power-state payload")
        return reply.data

    def power(self, identifier, target, cookie, *, minimum=3):
        payload = Container(
            devid=identifier,
            cookie=cookie,
            target_pstate=target,
            unk2=1 if target in ("pwrd", "runn") else 0,
        )
        if identifier == "lai ":
            payload = PowerSetting.build(payload)
        self.query(
            SetDeviceProp(devid=identifier, modifier=202, data=payload), minimum=minimum
        )
        self.require(
            self.get_state(identifier) == target, "Fresh power state does not match"
        )

    def map_buffer(self, stream, size):
        d, p = self.dart, self.p
        base = d.regs._base
        enabled = self.rpc("DART enable mask", lambda: p.read32(base + 0xC00))
        tcr, valid, roots = self.rpc(
            "input stream ownership",
            lambda: (
                d.regs.TCR[stream].val,
                bool(d.regs.TTBR[stream].reg.VALID),
                tuple(
                    (d.regs.TCR[i].val, d.regs.TTBR[i].val)
                    for i in (0, 8)
                    if i != stream
                ),
            ),
        )
        self.require(
            not enabled & (1 << stream)
            and not valid
            and tcr in ((0, 1) if stream == 10 else (0, 1, 2)),
            "Input stream already owned",
        )
        pa = self.rpc("allocate owned buffer", lambda: self.u.memalign(PAGE, size))
        self.require(
            type(pa) is int and pa > 0 and pa % PAGE == 0 and pa + size <= 1 << 42,
            "Invalid owned allocation",
        )
        self.state.setdefault("buffers", []).append(
            dict(stream=stream, pa=hex(pa), bytes=size)
        )
        self.rpc("zero buffer", lambda: p.memset32(pa, 0, size))
        self.rpc("clean buffer", lambda: p.dc_cvac(pa, size))
        self.rpc("clear unused TTBR", lambda: setattr(d.regs.TTBR[stream], "val", 0))
        self.rpc("four-level TCR", lambda: setattr(d.regs.TCR[stream], "val", 9))
        self.rpc("deferred map", lambda: d.iomap_at(stream, pa, pa, size, enable=False))
        self.require(
            self.rpc("deferred enable readback", lambda: p.read32(base + 0xC00))
            == enabled,
            "Deferred mapping changed enable mask",
        )
        self.state["retained_page_tables"] = sorted(d.pt_cache)
        for table in self.state["retained_page_tables"]:
            self.rpc("clean page table", lambda table=table: p.dc_cvac(table, PAGE))
        self.rpc("publish barrier", lambda: self.u.exec("dsb sy"))
        self.rpc("invalidate stream", lambda: p.write32(base + 0x80, 0x100 | stream))
        deadline = self.clock() + 0.5
        while self.rpc("invalidation poll", lambda: p.read32(base + 0x80)) & (1 << 31):
            self.require(self.clock() < deadline, "DART invalidation timeout")
        d.invalidate_cache()
        self.require(
            self.rpc("fresh full translation", lambda: d.iotranslate(stream, pa, size))
            == [(pa, size)],
            "Published mapping does not cover full owned buffer",
        )
        self.require(
            self.rpc("enable publication guard", lambda: p.read32(base + 0xC00))
            == enabled,
            "Enable mask changed before publication",
        )
        self.rpc(
            "enable owned stream",
            lambda: p.write32(base + 0xC00, enabled | (1 << stream)),
        )
        d.enabled_streams = enabled | (1 << stream)
        self.require(
            self.rpc("enabled readback", lambda: p.read32(base + 0xC00))
            == d.enabled_streams
            and self.rpc(
                "existing roots readback",
                lambda: tuple(
                    (d.regs.TCR[i].val, d.regs.TTBR[i].val)
                    for i in (0, 8)
                    if i != stream
                ),
            )
            == roots,
            "Existing roots/stream mask changed",
        )
        self.require(
            not self.rpc(
                "DART fault after mapping", lambda: bool(d.regs.ERROR.reg.FLAG)
            ),
            "DART fault after mapping",
        )
        return pa

    def warm_lp(self):
        for identifier in ("hpai", "lpai"):
            self.require(
                self.get_state(identifier) == "idle", "Fresh cold frontend required"
            )
        reply = self.query(GetDeviceProp(devid="adpx", modifier=300))
        self.require(
            reply.len == 16
            and bytes(reply.data) == struct.pack("<QQ", (1 << 64) - 1, 0),
            "LP buffer already published",
        )
        backing = (LP_BYTES + PAGE - 1) & ~(PAGE - 1)
        pa = self.map_buffer(8, backing)
        self.query(
            SetDeviceProp(
                devid="adpx", modifier=300, data=struct.pack("<QQ", pa, LP_BYTES)
            ),
            minimum=15,
        )
        readback = self.query(GetDeviceProp(devid="adpx", modifier=300))
        self.require(readback.len == 16, "Malformed LP buffer property extent")
        address, capacity = struct.unpack("<QQ", readback.data)
        self.require(address == pa and capacity == LP_BYTES, "LP publication differs")
        for identifier in ("lpai", "pdm0", "adpx"):
            self.query(AudioAttachDevice(devid=identifier))
        checked = self.query(GetDeviceProp(devid="adpx", modifier=300))
        self.require(
            checked.len == 16 and struct.unpack("<QQ", checked.data) == (pa, LP_BYTES),
            "LP publication changed during attachment",
        )
        self.power("lpai", "pw1 ", 1, minimum=15)
        self.query(AudioAttachDevice(devid="lai "))
        self.service.last_producer_report = None
        self.service.last_producer_report_payload = None
        self.power("lai ", "runn", 2, minimum=15)
        self.power("lpai", "runn", 3, minimum=15)
        deadline = self.clock() + 2
        while self.service.last_producer_report_payload is None:
            self.require(self.clock() < deadline, "LP producer report timed out")
            self.rpc("LP report wait", self.asc.work)
        self.state["lp_report"] = validate_lp_report(
            self.service.last_producer_report_payload, capacity
        )

    def capture_hp(self):
        p, d, u = self.p, self.dart, self.u
        base = 0x513300000
        self.query(AudioAttachDevice(devid="hpai"))
        self.power("hpai", "pw1 ", 4)
        self.require(
            self.rpc("RX enable preflight", lambda: p.read32(base + 8)) == 0,
            "RX controller not exclusive",
        )
        for off in (0xC070, 0xC074):
            word = self.rpc("empty RX rings", lambda off=off: p.read32(base + off))
            self.require(
                word & 0x100 and not word & 0x600, "RX ring not empty or faulted"
            )
        self.require(
            self.rpc("RX SRAM size", lambda: p.read32(base + 0x98)) & 0xFFFF >= 0x1000,
            "RX SRAM differs",
        )
        for index in range(9):
            word = self.rpc(
                "RX carveout preflight",
                lambda index=index: p.read32(base + 0xC050 + index * 0x200),
            )
            self.require(
                word >> 16 == 0
                or (word & 0xFFFF) + (word >> 16) <= 0x800
                or word & 0xFFFF >= 0x1000,
                "RX SRAM overlaps",
            )
        pa = self.map_buffer(10, self.size)
        mac = ADMAC(u, "/arm-io/admac-leap-ns")
        channel = mac.chans[1]
        self.require(
            mac.split_channels and channel.rx and channel.reg_ch == 32,
            "Requires NS RX0/bank32",
        )
        self.rpc("reset own rings", lambda: setattr(mac.regs.CHAN_CTL[32], "val", 1))
        self.rpc("release own rings", lambda: setattr(mac.regs.CHAN_CTL[32], "val", 0))
        for attr, value in (
            ("buswidth", E_BUSWIDTH.W_32BIT),
            ("framesize", E_FRAME.F_1_WORD),
            ("sram_carveout", (0x800, 0x800)),
            ("burstsize", 0xC00060),
        ):
            self.rpc(
                "configure RX " + attr,
                lambda attr=attr, value=value: setattr(channel, attr, value),
            )
        desc = ADMACDescriptor(pa, self.size, DESC_ID=1, NOTIFY=1)
        self.rpc(
            "submit owned descriptor", lambda: channel.submit_desc(desc), minimum=15
        )
        self.state["descriptor_words"] = desc.ser()

        def start_rx():
            self.state["before_rx_enable"] = self.clock()
            self.announce("HQAI_CAPTURE_START", flush=True)
            p.write32(base + 8, 1)

        self.rpc("start RX before HP power", start_rx, minimum=15)
        self.power("hpai", "pwrd", 5)
        self.state["after_hp_pwrd"] = self.clock()
        deadline = self.clock() + (14 if self.size == 0x600000 else 8)
        while self.rpc("completion poll", lambda: p.read32(base + 0xC074)) & 0x100:
            self.require(
                self.clock() < deadline, "Owned descriptor completion timed out"
            )
            self.rpc("AOP work during capture", self.asc.work)
        self.state["report_ready"] = self.clock()
        self.rpc("consume owned report", channel.read_reports)
        report = channel._last_report
        self.require(
            report is not None
            and report.flags.DESC_ID == 1
            and set(channel._submitted) == {1},
            "Unexpected report ownership",
        )
        self.state["report_words"] = report.ser()
        self.require(
            self.rpc("completion residue", lambda: p.read32(base + 0xC064)) == 0,
            "Incomplete descriptor residue",
        )

        def check_owned():
            self.require(self.get_state("hpai") == "pwrd", "HP active window lost")
            self.require(
                self.rpc("snapshot RX enable", lambda: p.read32(base + 8)) == 1
                and channel._last_report is report
                and channel._submitted == {1: desc}
                and (desc.addr, desc.length, desc.flags.REPEAT) == (pa, self.size, 0),
                "RX/report/descriptor identity changed",
            )
            for off in (0xC070, 0xC074):
                word = self.rpc(
                    "completed empty rings", lambda off=off: p.read32(base + off)
                )
                self.require(
                    word & 0x100 and not word & 0x600,
                    "Queued/error/full completion ring",
                )
            d.invalidate_cache()
            self.require(
                self.rpc(
                    "owned full snapshot mapping",
                    lambda: d.iotranslate(10, pa, self.size),
                )
                == [(pa, self.size)],
                "Owned mapping changed",
            )
            self.require(
                self.rpc("snapshot residue", lambda: p.read32(base + 0xC064)) == 0
                and not self.rpc(
                    "snapshot DART fault", lambda: bool(d.regs.ERROR.reg.FLAG)
                ),
                "Residue/DART fault",
            )

        copies = []
        for index in range(2):
            check_owned()
            self.rpc(
                "invalidate active buffer", lambda: p.dc_ivac(pa, self.size), minimum=15
            )
            parts = []
            for offset in range(0, self.size, 0x10000):
                length = min(0x10000, self.size - offset)
                data = self.rpc(
                    "download owned chunk",
                    lambda offset=offset, length=length: u.iface.readmem(
                        pa + offset, length
                    ),
                )
                self.require(
                    isinstance(data, bytes) and len(data) == length,
                    "Short owned download",
                )
                parts.append(data)
            copies.append(b"".join(parts))
        check_owned()
        self.require(
            copies[0] == copies[1] and len(copies[0]) == self.size,
            "Owned active copies differ",
        )
        self.data = copies[0]
        self.state.update(
            stable=True,
            sha256=hashlib.sha256(self.data).hexdigest(),
            bytes=self.size,
            nominal_seconds=self.size / (RATE * FRAME_BYTES),
        )
        self.rpc("disable owned RX", channel.disable)
        for offset, value in ((0xC0A0, 0), (0xC000, 1), (0xC000, 0)):
            self.rpc(
                "clear/reset owned RX",
                lambda offset=offset, value=value: p.write32(base + offset, value),
            )
        self.require(
            self.rpc("stopped RX enable", lambda: p.read32(base + 8)) == 0,
            "RX stop not confirmed",
        )
        self.power("hpai", "pw1 ", 6)
        self.power("hpai", "idle", 7)
        self.state["hp_restored_idle"] = True
        self.persist(self.state)
