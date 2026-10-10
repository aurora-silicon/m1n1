# SPDX-License-Identifier: MIT
"""Read-only geometry for the qualified J616s 25G76 ISP image.

This profile does not boot the ISP. Physical allocation ownership, resident
image verification, DART parameters and fresh handshake values remain startup
prerequisites. Native iBoot segment exporter 0x1ad314..0x1ad47c
uses 32-byte <QQQII records; ISP boot reader 0x22f7c consumes the modern
header. The 0x290 container and gaps were also verified by live handshakes.
In particular, remap_base is not a general CPU-pointer decoder.
"""
from dataclasses import dataclass
import struct


@dataclass(frozen=True)
class ISPSegment:
    name: str
    phys: int
    virtual: int
    remap: int
    size: int
    flags: int


@dataclass(frozen=True)
class ISPBootLayout:
    ipc_iova: int
    ipc_size: int
    args_iova: int
    command_iova: int
    command_size: int
    extra_iova: int
    extra_size: int
    shared_base: int
    shared_size: int


@dataclass(frozen=True)
class ISPChannelLayout:
    name: str
    type: int
    source: int
    count: int
    iova: int


@dataclass(frozen=True)
class ISPModernProfile:
    firmware_sha256: str
    page_size: int = 0x4000
    remap_base: int = 1 << 40
    # Qualified DART8110 four-level implementation and live PARAMS_8 VA_WIDTH.
    dart_iova_limit: int = 1 << 42
    boot_block_size: int = 0x290
    ipc_size: int = 0x1c000
    slot_size: int = 0x40
    channel_descriptor_size: int = 0x100
    command_size: int = 0x400

    def validate_segments(self, *, firmware_sha256, segment_names,
                          segment_ranges):
        if firmware_sha256 != self.firmware_sha256:
            raise ValueError("ISP firmware does not match the 25G76 profile")
        if (segment_names != "__TEXT;__DATA" or
                not isinstance(segment_ranges, bytes) or len(segment_ranges) != 64):
            raise ValueError("Unexpected ISP segment metadata")
        segments = []
        for name, expected, values in zip(("__TEXT", "__DATA"),
                ((0, 0xca8000, 1), (0xca8000, 0x1344000, 0)),
                struct.iter_unpack("<QQQII", segment_ranges)):
            phys, virtual, remap, size, flags = values
            if (virtual, size, flags) != expected or remap != self.remap_base + virtual:
                raise ValueError("ISP segment differs from the captured 25G76 layout")
            if not phys or phys % self.page_size or phys + size > 1 << 64:
                raise ValueError("Invalid ISP physical segment extent")
            if any(phys < old.phys + old.size and old.phys < phys + size for old in segments):
                raise ValueError("ISP physical segments overlap")
            segments.append(ISPSegment(name, *values))
        return tuple(segments)

    def boot_layout(self, segments, *, args_offset, extra_size):
        if type(args_offset) is not int or args_offset < 0 or args_offset % self.slot_size:
            raise ValueError("Invalid negotiated ISP argument offset")
        if (type(extra_size) is not int or not 0 < extra_size <= 0x7000000 or
                extra_size % self.page_size):
            raise ValueError("Invalid negotiated ISP heap size")
        if len(segments) != 2 or segments[1].name != "__DATA":
            raise ValueError("Validated ISP segments required")
        self.validate_segments(firmware_sha256=self.firmware_sha256,
            segment_names=";".join(s.name for s in segments),
            segment_ranges=b"".join(struct.pack("<QQQII", s.phys, s.virtual,
                s.remap, s.size, s.flags) for s in segments))
        data = segments[1]
        ipc = data.remap + data.size + self.page_size
        args = ipc + args_offset + self.slot_size
        command = args + self.boot_block_size + self.slot_size
        extra = ipc + self.ipc_size + self.page_size
        if (command + self.command_size > ipc + self.ipc_size or
                extra + extra_size > self.dart_iova_limit):
            raise ValueError("ISP boot layout exceeds allocation bounds")
        shared = data.remap + data.size - self.remap_base
        return ISPBootLayout(ipc, self.ipc_size, args, command, self.command_size,
                             extra, extra_size, shared, 0x10000000 - shared)

    def prepare_bootargs(self, *, ipc_iova, ipc_size, args_offset, extra_iova, extra_size,
                         shared_base, shared_size, platform_id, camera_scheme,
                         command_size=0x400):
        for name, value in locals().copy().items():
            if name == "self":
                continue
            if type(value) is not int or value < 0:
                raise ValueError(f"Invalid {name}")
        limit = self.dart_iova_limit
        if not ipc_iova or ipc_iova % 0x4000 or not ipc_size or ipc_size % 0x4000:
            raise ValueError("IPC allocation must be page aligned and nonempty")
        if ipc_iova + ipc_size > limit:
            raise ValueError("IPC allocation exceeds the DART address range")
        if extra_size:
            if not extra_iova or extra_iova % 0x4000 or extra_iova + extra_size > limit:
                raise ValueError("Invalid extra allocation")
            extra_end = extra_iova + ((extra_size + 0x3fff) & ~0x3fff)
            if extra_end > limit:
                raise ValueError("Extra allocation exceeds the DART address range")
            if ipc_iova < extra_end and extra_iova < ipc_iova + ipc_size:
                raise ValueError("IPC and extra allocations overlap")
        elif extra_iova:
            raise ValueError("Empty extra allocation has an address")
        if not shared_base or not shared_size or shared_base + shared_size > 1 << 64:
            raise ValueError("Invalid shared range")
        if platform_id >= 1 << 32 or camera_scheme >= 1 << 64:
            raise ValueError("Platform or camera scheme exceeds its field width")
        if args_offset % 0x40 or not command_size:
            raise ValueError("Invalid argument offset or command size")
        args_iova = ipc_iova + args_offset + 0x40
        command_iova = args_iova + self.boot_block_size + self.slot_size
        if command_iova + command_size > ipc_iova + ipc_size:
            raise ValueError("Boot arguments and command exceed the IPC allocation")

        block = bytearray(self.boot_block_size)
        struct.pack_into("<QQQQQ", block, 8, ipc_iova, shared_base, shared_size,
                         extra_iova, extra_size)
        struct.pack_into("<I", block, 0x30, platform_id)
        struct.pack_into("<Q", block, 0x50, args_offset + 1)
        struct.pack_into("<I", block, 0x68, 0x40)
        struct.pack_into("<Q", block, 0x70 + 0x60, camera_scheme)
        struct.pack_into("<Q", block, 0x70 + 0xb0, 1)
        return args_iova, command_iova, bytes(block)

    def parse_channels(self, raw, *, table_iova, count, boot):
        if (type(count) is not int or
                not 0 < count <= boot.ipc_size // self.channel_descriptor_size):
            raise ValueError("Invalid negotiated ISP channel count")
        extent = count * self.channel_descriptor_size
        if (len(raw) != extent or
                not boot.ipc_iova <= table_iova <= boot.ipc_iova + boot.ipc_size - extent):
            raise ValueError("ISP channel table outside IPC allocation")
        for start, size in ((boot.args_iova, self.boot_block_size),
                            (boot.command_iova, boot.command_size)):
            if table_iova < start + size and start < table_iova + extent:
                raise ValueError("ISP channel table overlaps boot storage")
        reserved = ((table_iova, extent),
                    (boot.args_iova, self.boot_block_size),
                    (boot.command_iova, boot.command_size))
        channels = []
        for offset in range(0, extent, self.channel_descriptor_size):
            name_bytes = raw[offset:offset + 0x40].split(b"\0", 1)[0]
            if not name_bytes or any(c < 32 or c >= 127 for c in name_bytes):
                raise ValueError("Invalid ISP channel name")
            name = name_bytes.decode("ascii")
            kind, source, num, _, iova = struct.unpack_from("<IIIIQ", raw, offset + 0x40)
            size = num * self.slot_size
            if kind not in (0, 1, 2) or not 0 < num <= 2048 or iova % self.slot_size:
                raise ValueError("Invalid ISP channel geometry")
            if not boot.ipc_iova <= iova <= boot.ipc_iova + boot.ipc_size - size:
                raise ValueError("ISP channel ring outside IPC allocation")
            if any(iova < start + span and start < iova + size
                   for start, span in reserved):
                raise ValueError("ISP channel ring overlaps IPC control storage")
            if any(name == old.name or
                   (iova < old.iova + old.count * self.slot_size and old.iova < iova + size)
                   for old in channels):
                raise ValueError("Duplicate or overlapping ISP channels")
            channels.append(ISPChannelLayout(name, kind, source, num, iova))
        return tuple(channels)


# Own isp.bin SHA256; segment/boot reader source and live second handshake
# retained in isp-native-adt-segment-export.json and the qualified capture.
T6040_25G76 = ISPModernProfile(
    "4990fa8e00c31be970166238d7953246ee5b9db3ab0085aca578c4198dbad433")
