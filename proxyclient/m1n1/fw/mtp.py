# SPDX-License-Identifier: MIT

import struct
import time
from construct import *
from ..constructutils import *
from ..utils import *

class HIDDescriptor(ConstructClass):
    subcon = Struct(
        "descriptor" / HexDump(GreedyBytes)
    )

class GPIOInit(ConstructClass):
    subcon = Struct(
        "unk1" / Int16ul,
        "gpio_id"/ Int16ul,
        "gpio_name" / PaddedString(32, "ascii")
    )

class InitBlock(ConstructClass):
    subcon = Struct(
        "type" / Int16ul,
        "length" / Int16ul,
        "payload" / FixedSized(this.length,
                   Switch(this.type, {
                       0: HIDDescriptor,
                       1: GPIOInit,
                       2: GreedyBytes, # Unknown 6 bytes terminator
                       7: GreedyBytes, # Device name
                   }, default=GreedyBytes))
    )

class InitMsg(ConstructClass):
    subcon = Struct(
        "msg_type" / Const(0xf0, Int8ul),
        "msg_subtype" / Const(0x01, Int8ul),
        "unk" / Const(0x00, Int8ul),
        "device_id" / Int8ul,
        "device_name" / PaddedString(16, "ascii"),
        "more_packets" / Int16ul,
        "msg" / RepeatUntil(lambda obj, lst, ctx: lst[-1].type == 2, InitBlock)
    )

class DeviceReadyMsg(ConstructClass):
    subcon = Struct(
        "msg_type" / Const(0xf1, Int8ul),
        "device_id" / Int8ul,
        "unk" / Int16ul
    )

class GPIORequestMsg(ConstructClass):
    subcon = Struct(
        "msg_type" / Const(0xa0, Int8ul),
        "device_id" / Int8ul,
        "gpio_num" / Int8ul,
        "cmd" / Int16ul,
        "args" / HexDump(GreedyBytes)
    )

NotificationMsg = Select(
    DeviceReadyMsg,
    InitMsg,
    GPIORequestMsg,
    HexDump(GreedyBytes),
)

class UnkDeviceControlMsg(ConstructClass):
    subcon = Struct(
        "command" / Int8ul,
        "args" / HexDump(GreedyBytes),
    )

class DeviceEnableMsg(ConstructClass):
    subcon = Struct(
        "command" / Const(0xb4, Int8ul),
        "device_id" / Int8ul,
    )

class DeviceResetMsg(ConstructClass):
    """Apple's "power method 2" interface power transition.

    9 bytes, not the 4 the older form used.  The J813 firmware's 0x40 handler
    checks, in this order:
        ldrh w1, [msg+2]; cmp #9        -- total length must be 9
        ldrb w1, [msg+1]; cmp #2        -- method must be 2 (old form sent 1)
        ldrb w1, [msg+3]; cmp #6, b.lo  -- state must be < 6
    Because the length test comes first, a 4-byte message is rejected with
    0xe00002c2 (kIOReturnBadArgument) before any field is read -- so sweeping
    the fields of a 4-byte message returns that same error for every
    combination, which looks like a parameter problem and is not one.

    Apple sends these in Will/HasChanged pairs:
        setInterfacePowerWillChange:  40 02 <iface> <state> 00 00 00 00 00
        setInterfacePowerHasChanged:  40 02 <iface> <state> 01 <status le32>
    """
    subcon = Struct(
        "command" / Const(0x40, Int8ul),
        "unk1" / Int8ul,
        "device_id" / Int8ul,
        "state" / Int8ul,
        "phase" / Int8ul,
        "status" / Int32ul,
    )

class InitBufMsg(ConstructClass):
    subcon = Struct(
        "command" / Const(0x91, Int8ul),
        "unk1" / Int8ul,
        "unk2" / Int8ul,
        "buf_addr" / Int64ul,
        "buf_size" / Int32ul,
    )

class InitAFEMsg(ConstructClass):
    subcon = Struct(
        "command" / Const(0x95, Int8ul),
        "unk1" / Int8ul,
        "unk2" / Int8ul,
        "iface" / Int8ul,
        "buf_addr" / Int64ul,
        "buf_size" / Int32ul,
    )

class UnkMsgC1(ConstructClass):
    subcon = Struct(
        "command" / Const(0xc1, Int8ul),
        "unk1" / Int8ul,
    )

class GPIOAckMsg(ConstructClass):
    subcon = Struct(
        "command" / Const(0xa1, Int8ul),
        "unk" / Int32ul,
        "msg" / GPIORequestMsg,
    )

DeviceControlMsg = Select(
    DeviceEnableMsg,
    DeviceResetMsg,
    InitAFEMsg,
    InitBufMsg,
    UnkMsgC1,
    UnkDeviceControlMsg
)

class DeviceControlAck(ConstructClass):
    subcon = Struct(
        "command" / Int8ul
    )

class MessageHeader(ConstructClass):
    subcon = Struct(
        "flags" / Int16ul,
        "length" / Int16ul,
        "retcode" / Int32ul,
    )

class TXMessage(ConstructClass):
    subcon = Struct(
        "hdr" / MessageHeader,
        "msg" / FixedSized(this.hdr.length,
                           Switch(this.hdr.flags, {
                               0x40: HexDump(GreedyBytes),
                               0x80: DeviceControlMsg,
                               0x81: Int8ul,
                           }))
    )

    def __init__(self):
        self.hdr = MessageHeader()

class RXMessage(ConstructClass):
    subcon = Struct(
        "hdr" / MessageHeader,
        "msg" / FixedSized(this.hdr.length, HexDump(GreedyBytes)),
    )

class MTPInterface:
    def __init__(self, proto, iface):
        self.proto = proto
        self.iface = iface
        self.tx_seq = 0
        self.initialized = False
        self.gpios = {}

    def send(self, msg):
        self.proto.send(self.iface, self.tx_seq & 0xff, msg)
        self.tx_seq += 1

    def get_report(self, idx):
        msg = TXMessage()
        msg.hdr.flags = 0x81
        msg.hdr.length = 1
        msg.hdr.retcode = 0
        msg.msg = idx
        self.send(msg.build())

    def packet(self, pkt):
        self.log(f"RX: {pkt.hex()}")

    def log(self, s):
        self.proto.log(f"[{self.NAME}] " + s)

    def initialize(self):
        self.proto.comm.enable_device(self.iface)

    def report(self, msg):
        self.log(f"report: {msg.hex()}")

    def ack(self, msg):
        self.log(f"ack: {msg.hex()}")

    def unk(self, msg):
        self.log(f"unk: {msg.hex()}")

    def packet(self, pkt):
        msg = RXMessage.parse(pkt)
        mtype = msg.hdr.flags
        #self.log(f"FL:{msg.hdr.flag    s:04x} unk:{msg.hdr.unk:08x}")
        if mtype == 0x00:
            self.report(msg.msg)
        elif mtype == 0x80:
            self.ack(msg.hdr.retcode, msg.msg)
        elif mtype == 0x81:
            self.log(f"REPORT")
            chexdump(msg.msg, print_fn=self.log)
        elif mtype == 0x40:
            self.unk(msg.msg)

    def __str__(self):
        return f"{self.iface}/{self.NAME}"


class MTPCommInterface(MTPInterface):
    NAME = "comm"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_cmd = None
        self.gpios = {}
        # Interfaces whose init packets have all arrived but whose
        # initialize() must not run yet; see report() and drain_init().
        self.pending_init = []
        self.in_control = False


    def device_control(self, dcmsg):
        while self.last_cmd is not None:
            self.proto.work()
        msg = TXMessage()
        msg.hdr.flags = 0x80
        msg.hdr.length = len(dcmsg.build())
        msg.hdr.retcode = 0
        msg.msg = dcmsg
        #self.log(f"Send device control {dcmsg}")
        self.last_cmd = dcmsg.command
        self.in_control = True
        try:
            self.send(msg.build())
            while self.last_cmd is not None:
                self.proto.work()
        finally:
            self.in_control = False

    def drain_init(self):
        # Runs only from the top-level pump, never from the nested one inside
        # device_control().
        while self.pending_init and not self.in_control:
            self.pending_init.pop(0).initialize()

    def enable_device(self, iface):
        msg = DeviceEnableMsg()
        msg.device_id = iface
        self.device_control(msg)

    def report(self, msg):
        msg = NotificationMsg.parse(msg)

        if isinstance(msg, DeviceReadyMsg):
            iface = self.proto.iface[msg.device_id]
            iface.initialized = True
            self.log(f"{iface}: init complete")
        elif isinstance(msg, InitMsg):
            iface = self.proto.get_interface(msg.device_id, msg.device_name)
            if iface is None:
                # An unmodelled interface name must not take the rest of the
                # bus down with it.  report() runs inside the work() pump that
                # device_control() spins while another device is mid-initialize,
                # so raising here aborts *that* device's initialize(), not just
                # this one -- which is how one unknown name on J813 left
                # keyboard and multi-touch permanently uninitialized.
                self.log(f"ignoring init for unmodelled interface "
                         f"{msg.device_id}/{msg.device_name}")
                return
            for blk in msg.msg:
                if isinstance(blk.payload, HIDDescriptor):
                    self.log(f"Got HID descriptor for {iface}:")
                    iface.descriptor = blk.payload.descriptor
                    self.log(hexdump(iface.descriptor))
                elif isinstance(blk.payload, GPIOInit):
                    self.log(f"GPIO Init: {blk.payload}")
                    prop = getattr(self.proto.node[msg.device_name],
                                   f"function-{blk.payload.gpio_name}".replace("-", "_"))
                    key = struct.pack(">I", prop.args[0]).decode("ascii")
                    val = prop.args[1]
                    self.log(f"GPIO key: {key}")
                    self.gpios[(msg.device_id, blk.payload.gpio_id)] = key, val
            if not msg.more_packets:
                # Deferred, not called here.  report() runs inside the work()
                # pump that device_control() spins while waiting for its ACK,
                # so initializing inline issues a second device-control command
                # before the first is acknowledged.  That overwrites last_cmd,
                # and the earlier command's ACK then trips the assert in ack().
                # J813 enumerates six interfaces back to back, so this fires
                # every boot and leaves keyboard and multi-touch uninitialized.
                self.pending_init.append(iface)
        elif isinstance(msg, GPIORequestMsg):
            self.log(f"GPIO request: {msg}")
            smcep = self.proto.smc.epmap[0x20]
            key, val = self.gpios[(msg.device_id, msg.gpio_num)]
            if msg.cmd == 3:
                smcep.write32(key, val | 1)
                smcep.write32(key, val)

            ackmsg = GPIOAckMsg()
            ackmsg.unk = 0
            ackmsg.msg = msg
            self.device_control(ackmsg)
        else:
            self.log("Unknown message!")
            print(msg)

    def ack(self, retcode, msg):
        msg = DeviceControlAck.parse(msg)
        self.log(f"Got ACK for {msg.command:#x}: {retcode:08x}")
        assert msg.command == self.last_cmd
        self.last_cmd = None

    def init_afe(self, iface, data):
        paddr, dva = self.proto.mtp.ioalloc(len(data))
        self.proto.u.iface.writemem(paddr, data)

        afemsg = InitAFEMsg()
        afemsg.unk1 = 2
        afemsg.unk2 = 0
        afemsg.iface = iface
        afemsg.buf_addr = dva
        afemsg.buf_size = len(data)
        self.device_control(afemsg)

    def device_reset(self, iface, unk1, state, phase=0, status=0):
        self.log(f"device_reset({iface}, {unk1}, {state}, phase={phase})")
        rmsg = DeviceResetMsg()
        rmsg.device_id = iface
        rmsg.unk1 = unk1
        rmsg.state = state
        rmsg.phase = phase
        rmsg.status = status
        self.device_control(rmsg)

    def set_interface_power(self, iface, state, delay=0):
        """Will/HasChanged pair for one power state, method 2.

        The delay sits between the two halves: the interface's enable sequence
        specifies 50 ms for the transition into state 2, and the firmware only
        accepts HasChanged once the hardware has actually settled.
        """
        self.device_reset(iface, 2, state, phase=0)
        if delay:
            time.sleep(delay)
        self.device_reset(iface, 2, state, phase=1, status=0)

DCHID_FW_MAGIC = 0x46444948     # "HIDF"

def dchid_firmware_payload(data, index):
    """Turn a tpmtfw-*.bin file into the buffer the IOP is given.

    The file is a 20-byte header (padded to header_length) followed by the CBOR
    image.  Only the image is DMAed, and the interface index has to be stamped
    into it at interface_offset -- the image is built per-machine but not per
    interface, so the IOP uses that byte to know which one it is describing.
    Handing over the whole file, header included, is ACKed by the 0x95 command
    and then silently ignored: no "cbor image received", no bootload.
    """
    magic, version, header_length, data_length, interface_offset = \
        struct.unpack_from("<5I", data, 0)
    if magic != DCHID_FW_MAGIC:
        raise ValueError("bad firmware magic %#x" % magic)
    payload = bytearray(data[header_length:header_length + data_length])
    if len(payload) != data_length:
        raise ValueError("firmware truncated: %d < %d" % (len(payload), data_length))
    if interface_offset:
        payload[interface_offset] = index
    return bytes(payload)

class MTPHIDInterface(MTPInterface):
    pass

class MTPMultitouchInterface(MTPHIDInterface):
    NAME = "multi-touch"

    # The CBOR ("HIDF") image built from the machine's own Multitouch.im4p, not
    # the raw mtfw plist.  The plist is the source form -- a register load list
    # keyed by touch controller part number (C1FD1,4 on J813) plus TCAL/FCAL
    # calibration blobs -- and asahi_firmware.multitouch.plist_to_bin_trackpad
    # packs it into the image the IOP actually accepts.  Handing over the plist
    # itself is ACKed by init_afe and then rejected downstream with
    # "Invalid blob: Touch" from touch_algs_platform.c.
    AFE_FIRMWARE = None

    def initialize(self):
        super().initialize()

        if self.AFE_FIRMWARE:
            payload = dchid_firmware_payload(self.AFE_FIRMWARE, self.iface)
            self.log("sending CBOR image: %d bytes" % len(payload))
            self.proto.comm.init_afe(self.iface, payload)
            # Apple's performCBORBootload: register the image, then drive the
            # interface through power state 0 and then 2.
            self.proto.comm.set_interface_power(self.iface, 0)
            self.proto.comm.set_interface_power(self.iface, 2, delay=0.05)

class MTPKeyboardInterface(MTPHIDInterface):
    NAME = "keyboard"

class MTPSTMInterface(MTPHIDInterface):
    NAME = "stm"

class MTPActuatorInterface(MTPHIDInterface):
    NAME = "actuator"

class MTPTPAccelInterface(MTPHIDInterface):
    # The IOP spells this with a hyphen on the wire, like every other interface
    # name; the underscore spelling here never matched, so J813's tp-accel fell
    # through to the "unknown interface" path.  get_interface() already converts
    # to an underscore for the attribute name.
    NAME = "tp-accel"

class MTPMTPInterface(MTPHIDInterface):
    # J813 advertises the IOP itself as an interface after actuator.
    NAME = "mtp"

class MTPProtocol:
    INTERFACES = [
        MTPCommInterface,
        MTPMultitouchInterface,
        MTPKeyboardInterface,
        MTPSTMInterface,
        MTPActuatorInterface,
        MTPTPAccelInterface,
        MTPMTPInterface,
    ]

    def __init__(self, u, node, mtp, dockchannel, smc):
        self.node = node
        self.smc = smc
        self.u = u
        self.mtp = mtp
        self.dockchannel = dockchannel
        self.iface = {}

        # Add initial comm interface
        self.get_interface(0, "comm")

    def get_interface(self, iface, name):
        if iface in self.iface:
            return self.iface[iface]

        for cls in self.INTERFACES:
            if cls.NAME == name:
                break
        else:
            self.log(f"Unknown interface name {name}")
            return None
        obj = cls(self, iface)
        self.iface[iface] = obj
        # Interface names come off the wire and are not guaranteed to be safe
        # attribute names on this object.  J813 advertises one literally called
        # "mtp", which would otherwise replace self.mtp -- the StandardASC the
        # transport runs on -- and break read_pkt() on the very next packet.
        attr = name.replace("-", "_")
        if hasattr(self, attr):
            attr = "iface_" + attr
        setattr(self, attr, obj)
        return obj

    def checksum(self, d):
        assert len(d) % 4 == 0
        c = len(d) // 4
        return 0xffffffff - sum(struct.unpack(f"<{c}I", d)) & 0xffffffff

    def read_pkt(self):
        self.mtp.work_pending()
        hdr = self.dockchannel.read(8)
        hlen, mtype, size, ctr, devid, pad = struct.unpack("<BBHBBH", hdr)
        #self.log(f"<L:{hlen} T:{mtype:02x} S:{size:04x} D:{devid}")
        assert hlen == 8
        #assert mtype == 0x12
        data = self.dockchannel.read(size)
        checksum = struct.unpack("<I", self.dockchannel.read(4))[0]
        expect = self.checksum(hdr + data)
        if expect != checksum:
            self.log(f"Checksum error: expected {expect:08x}, got {checksum:08x}")
        return devid, data

    def send(self, iface, seq, msg):
        if len(msg) % 4:
            msg += bytes(4 - len(msg) % 4)
        hdr = struct.pack("<BBHBBH", 8, 0x11, len(msg), seq, iface, 0)
        checksum = self.checksum(hdr + msg)
        pkt = hdr + msg + struct.pack("<I", checksum)
        self.dockchannel.write(pkt)
        self.mtp.work_pending()

    def work_pending(self):
        self.mtp.work_pending()
        while self.dockchannel.rx_count != 0:
            self.work()
        self.mtp.work_pending()

    def work(self):
        devid, pkt = self.read_pkt()
        self.iface[devid].packet(pkt)
        self.comm.drain_init()

    def wait_init(self, name):
        self.log(f"Waiting for {name}...")
        while not hasattr(self, name) or not getattr(self, name).initialized:
            self.work()

    def log(self, m):
        print("[MTP]" + m)
