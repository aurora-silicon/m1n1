# SPDX-License-Identifier: MIT

import struct
from construct import *
from ..constructutils import *
from ..utils import *

def checksum(data):
    if len(data) % 4:
        raise ValueError("MTP checksum requires aligned data")
    return (0xffffffff - sum(word[0] for word in struct.iter_unpack("<I", data))) & 0xffffffff


def encode_packet(iface, seq, msg):
    msg += bytes(-len(msg) % 4)
    header = struct.pack("<BBHBBH", 8, 0x11, len(msg), seq, iface, 0)
    return header + msg + struct.pack("<I", checksum(header + msg))


def decode_packet(packet):
    if len(packet) < 12:
        raise ValueError("Truncated MTP packet")
    hlen, kind, size, seq, iface, pad = struct.unpack_from("<BBHBBH", packet)
    if hlen != 8 or size % 4 or len(packet) != size + 12:
        raise ValueError("Invalid MTP packet length")
    if kind not in (0x11, 0x12) or pad:
        raise ValueError("Unsupported MTP header")
    if checksum(packet[:-4]) != struct.unpack_from("<I", packet, len(packet) - 4)[0]:
        raise ValueError("MTP checksum mismatch")
    return iface, kind, seq, packet[8:-4]


def prepare_firmware(data, iface):
    if len(data) < 32:
        raise ValueError("Truncated HIDF header")
    magic, version, header_size, size, offset = struct.unpack_from("<4sIIII", data)
    if (magic, version, header_size) != (b"HIDF", 1, 32):
        raise ValueError("Unsupported HIDF header")
    if size != len(data) - header_size or offset >= size:
        raise ValueError("Invalid HIDF payload bounds")
    if not 0 < iface < 256:
        raise ValueError("Invalid firmware interface")
    payload = bytearray(data[header_size:])
    if payload[offset] != 0:
        raise ValueError("Firmware interface placeholder is not zero")
    payload[offset] = iface
    return bytes(payload)


def check_control_reply(request, expected_seq, received_seq, reply):
    if received_seq != expected_seq or reply.hdr.flags != 0x80:
        raise ValueError("Unexpected MTP control reply")
    expected = request if request[0] == 0x40 else request[:1]
    if reply.msg != expected:
        raise ValueError("MTP control reply does not match its request")
    if reply.hdr.retcode:
        raise RuntimeError(f"MTP command {request[0]:#x} failed: {reply.hdr.retcode:#x}")


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
    subcon = Struct(
        "command" / Const(0x40, Int8ul),
        "unk1" / Int8ul,
        "device_id" / Int8ul,
        "state" / Int8ul,
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
        self.send(msg.build())
        while self.last_cmd is not None:
            self.proto.work()

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
                iface.initialize()
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

    def device_reset(self, iface, unk1, state):
        self.log(f"device_reset({iface}, {unk1}, {state})")
        rmsg = DeviceResetMsg()
        rmsg.device_id = iface
        rmsg.unk1 = unk1
        rmsg.state = state
        self.device_control(rmsg)

class MTPHIDInterface(MTPInterface):
    pass

class MTPMultitouchInterface(MTPHIDInterface):
    NAME = "multi-touch"

    def initialize(self):
        super().initialize()

        #data = open("afe.bin", "rb").read()
        #self.proto.comm.init_afe(self.iface, data)
        #self.proto.comm.device_reset(self.iface, 1, 0)
        #self.proto.comm.device_reset(self.iface, 1, 2)

class MTPKeyboardInterface(MTPHIDInterface):
    NAME = "keyboard"

class MTPSTMInterface(MTPHIDInterface):
    NAME = "stm"

class MTPActuatorInterface(MTPHIDInterface):
    NAME = "actuator"

class MTPTPAccelInterface(MTPHIDInterface):
    NAME = "tp_accel"

class MTPProtocol:
    INTERFACES = [
        MTPCommInterface,
        MTPMultitouchInterface,
        MTPKeyboardInterface,
        MTPSTMInterface,
        MTPActuatorInterface,
        MTPTPAccelInterface,
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
        setattr(self, name.replace("-", "_"), obj)
        return obj

    def checksum(self, data):
        return checksum(data)

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
        self.dockchannel.write(encode_packet(iface, seq, msg))
        self.mtp.work_pending()

    def work_pending(self):
        self.mtp.work_pending()
        while self.dockchannel.rx_count != 0:
            self.work()
        self.mtp.work_pending()

    def work(self):
        devid, pkt = self.read_pkt()
        self.iface[devid].packet(pkt)

    def wait_init(self, name):
        self.log(f"Waiting for {name}...")
        while not hasattr(self, name) or not getattr(self, name).initialized:
            self.work()

    def log(self, m):
        print("[MTP]" + m)
