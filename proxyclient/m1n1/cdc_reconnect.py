# SPDX-License-Identifier: MIT
"""Bounded host discovery for m1n1's opt-in J700 CDC proxy carrier."""

from dataclasses import dataclass
import hashlib
from pathlib import Path
import struct
import time
from types import SimpleNamespace

from serial.tools import list_ports

from .proxy import IODEV, M1N1Proxy, ProxyError, UartError, UartInterface, UartTimeout
from .proxyutils import ProxyUtils, bootstrap_port
from .tgtypes import BootArgs_r1, BootArgs_r2, BootArgs_r3


CDC_VID = 0x1209
CDC_PID = 0x316D


class CdcScheduleUncertain(RuntimeError):
    """The KIS session timed out before acknowledging the transition."""


class CdcDiscoveryTimeout(RuntimeError):
    """The schedule was acknowledged, but no verified CDC primary appeared."""


class CdcIdentityMismatch(RuntimeError):
    """A responding CDC device did not match the pinned live state."""


@dataclass(frozen=True)
class ImmutableRegion:
    offset: int
    size: int
    sha256: str


@dataclass(frozen=True)
class CdcIdentity:
    build_tag: str
    adt_digest: str
    regions: tuple[ImmutableRegion, ...]


@dataclass
class CdcSession:
    path: str
    interface: str | None
    iface: UartInterface
    proxy: M1N1Proxy
    utils: ProxyUtils
    identity: CdcIdentity
    link_mbps: int | None = None
    reconnect_count: int = 0

    def reconnect(self, *, timeout=30, log=lambda event: None,
                  speed_probe=lambda port: None):
        """Re-prove the carrier while retaining this live image's allocation ledger."""
        self.iface.dev.close()
        fresh = discover_cdc(self.identity, timeout=timeout, log=log,
                             speed_probe=speed_probe, utils=self.utils)
        fresh.reconnect_count = self.reconnect_count + 1
        log({"event": "cdc_reconnected", "count": fresh.reconnect_count,
             "adt_digest": fresh.identity.adt_digest})
        return fresh


def eligible_cdc_ports(ports):
    """USB metadata narrows candidates; a fresh NOP identifies the primary."""
    return sorted(
        (port for port in ports if port.vid == CDC_VID and port.pid == CDC_PID),
        key=lambda port: (str(port.interface or ""), port.device),
    )


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _artifact_regions(artifact_dir, ranges):
    """Read file-backed load ranges from one matching ELF and raw image."""
    folder = Path(artifact_dir)
    elf = (folder / "m1n1-raw.elf").read_bytes()
    image = (folder / "m1n1.bin").read_bytes()
    if elf[:4] != b"\x7fELF" or elf[4:6] != b"\x02\x01":
        raise ValueError("candidate is not a little-endian ELF64 image")
    phoff = struct.unpack_from("<Q", elf, 32)[0]
    entsize, count = struct.unpack_from("<HH", elf, 54)
    if entsize < 56 or phoff + entsize * count > len(elf):
        raise ValueError("invalid candidate ELF program headers")
    loads = []
    for i in range(count):
        kind, _, file_offset, vaddr, _, filesz, _, _ = struct.unpack_from(
            "<IIQQQQQQ", elf, phoff + i * entsize)
        if kind == 1 and file_offset + filesz <= len(elf):
            loads.append((vaddr, vaddr + filesz, file_offset))
    expected = []
    for offset, size in ranges:
        if offset < 0 or size <= 0 or size > 16 * 1024 * 1024:
            raise ValueError("invalid image range")
        segment = next((load for load in loads
                        if load[0] <= offset and offset + size <= load[1]), None)
        if segment is None or offset + size > len(image):
            raise ValueError("image range is not file-backed by the candidate ELF")
        data = elf[segment[2] + offset - segment[0]:
                   segment[2] + offset - segment[0] + size]
        if data != image[offset:offset + size]:
            raise CdcIdentityMismatch("candidate ELF and image disagree")
        expected.append(data)
    return expected


def capture_identity(utils, build_tag, ranges, artifact_dir):
    if not build_tag or not ranges:
        raise ValueError("build tag and immutable image ranges are required")
    regions = []
    tag = ("##m1n1_ver##" + build_tag).encode("ascii")
    found_tag = False
    for (offset, size), expected in zip(ranges, _artifact_regions(artifact_dir, ranges)):
        found_tag |= tag in expected
        data = utils.iface.readmem(utils.base + offset, size)
        if data != expected:
            raise CdcIdentityMismatch("live KIS image differs from candidate artifact")
        regions.append(ImmutableRegion(offset, size, _digest(expected)))
    if not found_tag:
        raise CdcIdentityMismatch("expected build tag absent from immutable image ranges")
    return CdcIdentity(build_tag, _digest(utils.get_adt()), tuple(regions))


def verify_identity(utils, identity):
    tag = ("##m1n1_ver##" + identity.build_tag).encode("ascii")
    found_tag = False
    if _digest(utils.get_adt()) != identity.adt_digest:
        raise CdcIdentityMismatch("live ADT hash changed across carrier transition")
    for region in identity.regions:
        data = utils.iface.readmem(utils.base + region.offset, region.size)
        found_tag |= tag in data
        if _digest(data) != region.sha256:
            raise CdcIdentityMismatch("live image region hash changed across carrier transition")
    if not found_tag:
        raise CdcIdentityMismatch("live build tag changed across carrier transition")


def _identity_reader(proxy):
    """Read live identity without allocating or changing the target heap limit."""
    address, revision = proxy.get_bootargs_rev()
    schema = {0: BootArgs_r1, 1: BootArgs_r1, 2: BootArgs_r2, 3: BootArgs_r3}.get(revision)
    if schema is None:
        raise CdcIdentityMismatch("unsupported live bootargs revision")
    ba = proxy.iface.readstruct(address, schema)
    adt_base = (ba.devtree - ba.virt_base + ba.phys_base) & 0xffffffffffffffff
    return SimpleNamespace(iface=proxy.iface, base=proxy.get_base(),
                           ba_addr=address, ba_rev=revision, ba=ba,
                           get_adt=lambda: proxy.iface.readmem(adt_base, ba.devtree_size))


def _open_primary(port, identity, *, utils=None):
    iface = UartInterface(port.device)
    session = None
    try:
        iface.dev.timeout = 1
        iface.nop()
        proxy = M1N1Proxy(iface)
        if proxy.iodev_whoami() != IODEV.USB0:
            return None
        bootstrap_port(iface, proxy)
        reader = _identity_reader(proxy)
        verify_identity(reader, identity)
        if utils is not None:
            if (reader.base != utils.base or reader.ba_addr != utils.ba_addr
                    or reader.ba_rev != utils.ba_rev or reader.ba != utils.ba):
                raise CdcIdentityMismatch("live image location or bootargs changed; retain old allocations")
        else:
            utils = ProxyUtils(proxy)
        session = CdcSession(port.device, port.interface, iface, proxy, utils, identity)
        return session
    finally:
        if session is None:
            iface.dev.close()


def discover_cdc(identity, *, timeout=30, ports=list_ports.comports,
                 probe=None, now=time.monotonic, sleep=time.sleep,
                 log=lambda event: None, speed_probe=lambda port: None, utils=None):
    if timeout <= 0:
        raise ValueError("discovery timeout must be positive")
    if utils is not None and utils.iface is not utils.proxy.iface:
        raise ValueError("existing utilities and proxy must share one transport")
    deadline = now() + timeout
    last_error = None
    attempts = 0
    while now() < deadline:
        for port in eligible_cdc_ports(ports()):
            attempts += 1
            session = None
            try:
                session = (_open_primary(port, identity, utils=utils) if probe is None
                           else probe(port, identity))
                if session is not None:
                    try:
                        speed = speed_probe(port)
                        if isinstance(session, CdcSession):
                            session.link_mbps = speed
                        log({"event": "cdc_primary_verified", "attempts": attempts,
                             "interface": str(port.interface or ""), "link_mbps": speed,
                             "adt_digest": identity.adt_digest if isinstance(identity, CdcIdentity) else None})
                    except Exception:
                        session.iface.dev.close()
                        session = None
                        raise
                    if utils is not None:
                        # Keep all driver references, bound methods and live allocations.
                        old_iface = utils.iface
                        for field in ("dev", "devpath", "baudrate", "is_kis",
                                      "pted", "enabled_features"):
                            if hasattr(session.iface, field):
                                setattr(old_iface, field, getattr(session.iface, field))
                        session.iface = old_iface
                        session.proxy = utils.proxy
                        session.utils = utils
                    return session
            except CdcIdentityMismatch:
                if session is not None:
                    session.iface.dev.close()
                log({"event": "cdc_identity_mismatch", "attempts": attempts})
                raise
            except (OSError, UartError, ProxyError) as exc:
                if session is not None:
                    session.iface.dev.close()
                last_error = exc
                log({"event": "cdc_candidate_retry", "attempts": attempts,
                     "error_type": type(exc).__name__})
        sleep(min(0.2, max(0, deadline - now())))
    log({"event": "cdc_discovery_timeout", "attempts": attempts})
    raise CdcDiscoveryTimeout(
        "acknowledged KIS transition; verified CDC primary did not appear"
    ) from last_error


def transition_to_cdc(proxy, utils, *, build_tag, immutable_ranges, artifact_dir,
                      delay_ms=1000, timeout=30, flags=0x6,
                      log=lambda event: None, speed_probe=lambda port: None):
    """Check the candidate on KIS, then find the new ACM primary by NOP."""
    identity = capture_identity(utils, build_tag, immutable_ranges, artifact_dir)
    log({"event": "kis_identity_pinned", "build_tag": build_tag,
         "adt_digest": identity.adt_digest,
         "image_region_sha256": [region.sha256 for region in identity.regions]})
    try:
        proxy.cdc_schedule(delay_ms, flags)
    except UartTimeout as exc:
        log({"event": "kis_schedule_reply_timeout"})
        raise CdcScheduleUncertain("KIS schedule reply timed out; transition state unknown") from exc
    log({"event": "kis_schedule_acknowledged", "delay_ms": delay_ms, "flags": flags})
    proxy.iface.dev.close()
    return discover_cdc(identity, timeout=timeout, log=log, speed_probe=speed_probe,
                        utils=utils)
