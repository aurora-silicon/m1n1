import ctypes
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WRITE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint8,
                         ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t)
READ = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint8,
                        ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t)
NOW = ctypes.CFUNCTYPE(ctypes.c_uint64, ctypes.c_void_p)
DELAY = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint)


class Ops(ctypes.Structure):
    _fields_ = [("write", WRITE), ("read", READ),
                ("now_ms", NOW), ("delay_us", DELAY)]


def command_lib(tmp_path):
    library = tmp_path / "libtps_command.dylib"
    subprocess.run(["cc", "-shared", "-fPIC", "-DTPS6598X_COMMAND_HOST_TEST",
                    str(ROOT / "src/tps6598x_command_core.c"), "-o", str(library)],
                   check=True)
    lib = ctypes.CDLL(str(library))
    lib.tps6598x_command_execute.argtypes = [ctypes.POINTER(Ops), ctypes.c_void_p,
                                              ctypes.c_char_p, ctypes.c_void_p,
                                              ctypes.c_size_t, ctypes.c_void_p,
                                              ctypes.c_size_t]
    lib.tps6598x_command_execute.restype = ctypes.c_int
    return lib


def test_stuck_register_selection_uses_whole_command_deadline(tmp_path):
    lib = command_lib(tmp_path)
    clock = [0]
    reads = []

    def write(ctx, reg, data, length):
        if reg == 0x08:
            clock[0] += 1600  # selection never completes
        return length

    def read(ctx, reg, data, length):
        reads.append(reg)
        return -1

    ops = Ops(WRITE(write), READ(read), NOW(lambda ctx: clock[0]),
              DELAY(lambda ctx, usec: None))
    assert lib.tps6598x_command_execute(ctypes.byref(ops), None, b"TEST",
                                         None, 0, None, 0) == -2
    assert reads == []


def test_perpetually_busy_command_times_out_without_10000_polls(tmp_path):
    lib = command_lib(tmp_path)
    clock = [0]
    polls = []

    def read(ctx, reg, data, length):
        polls.append(reg)
        clock[0] += 100
        ctypes.memmove(data, b"BUSY", 4)
        return 4

    ops = Ops(WRITE(lambda ctx, reg, data, length: length), READ(read),
              NOW(lambda ctx: clock[0]), DELAY(lambda ctx, usec: None))
    assert lib.tps6598x_command_execute(ctypes.byref(ops), None, b"TEST",
                                         None, 0, None, 0) == -2
    assert 1 <= len(polls) <= 15
