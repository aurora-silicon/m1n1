import ctypes
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class State(ctypes.Structure):
    _fields_ = [
        ("state", ctypes.c_int),
        ("flags", ctypes.c_uint32),
        ("step", ctypes.c_uint32),
        ("deadline_ms", ctypes.c_uint64),
    ]


def state_lib(tmp_path):
    library = tmp_path / "libcdc_state.dylib"
    subprocess.run(
        ["cc", "-shared", "-fPIC", "-DCDC_STATE_HOST_TEST",
         str(ROOT / "src/usb_cdc_state.c"), "-o", str(library)],
        check=True,
    )
    lib = ctypes.CDLL(str(library))
    lib.usb_cdc_state_schedule.argtypes = [ctypes.POINTER(State), ctypes.c_uint32,
                                            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint64]
    lib.usb_cdc_state_due.argtypes = [ctypes.POINTER(State), ctypes.c_uint64]
    lib.usb_cdc_state_due.restype = ctypes.c_bool
    lib.usb_cdc_state_status.argtypes = [ctypes.POINTER(State)]
    lib.usb_cdc_state_status.restype = ctypes.c_uint32
    return lib


def test_schedule_is_bounded_and_rejects_unqualified_ssp(tmp_path):
    lib = state_lib(tmp_path)
    for delay, reserved, flags in [(999, 0, 6), (15001, 0, 6),
                                   (1000, 1, 6), (1000, 0, 8),
                                   (1000, 0, 4)]:
        state = State()
        assert lib.usb_cdc_state_schedule(ctypes.byref(state), delay, reserved, flags, 10) == -1
        assert lib.usb_cdc_state_status(ctypes.byref(state)) == 0


def test_acknowledged_schedule_runs_once_after_deadline(tmp_path):
    lib = state_lib(tmp_path)
    state = State()
    assert lib.usb_cdc_state_schedule(ctypes.byref(state), 1000, 0, 6, 100) == 0
    assert lib.usb_cdc_state_status(ctypes.byref(state)) == 1
    assert not lib.usb_cdc_state_due(ctypes.byref(state), 1099)
    assert lib.usb_cdc_state_due(ctypes.byref(state), 1100)
    assert lib.usb_cdc_state_schedule(ctypes.byref(state), 1000, 0, 6, 1100) == -1
    state.step = 5
    state.state = 2
    assert lib.usb_cdc_state_status(ctypes.byref(state)) == 0x502
    assert not lib.usb_cdc_state_due(ctypes.byref(state), 1200)
    state.state = 3
    assert lib.usb_cdc_state_status(ctypes.byref(state)) == 0x503
