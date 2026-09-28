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


class Recovery(ctypes.Structure):
    _fields_ = [("pending", ctypes.c_bool), ("attempts", ctypes.c_uint32),
                ("deadline_ms", ctypes.c_uint64)]


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
    lib.usb_cdc_recovery_arm.argtypes = [ctypes.POINTER(Recovery), ctypes.c_uint64]
    lib.usb_cdc_recovery_connected.argtypes = [ctypes.POINTER(Recovery)]
    lib.usb_cdc_recovery_due.argtypes = [ctypes.POINTER(Recovery), ctypes.c_uint64]
    lib.usb_cdc_recovery_due.restype = ctypes.c_bool
    lib.usb_cdc_recovery_attempted.argtypes = [ctypes.POINTER(Recovery), ctypes.c_uint64]
    lib.usb_cdc_dma_may_release.argtypes = [ctypes.c_bool, ctypes.c_int, ctypes.c_bool]
    lib.usb_cdc_dma_may_release.restype = ctypes.c_bool
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


def test_recovery_has_three_attempts_and_connect_resets_budget(tmp_path):
    lib = state_lib(tmp_path)
    recovery = Recovery()
    lib.usb_cdc_recovery_arm(ctypes.byref(recovery), 100)
    assert not lib.usb_cdc_recovery_due(ctypes.byref(recovery), 124)
    assert lib.usb_cdc_recovery_due(ctypes.byref(recovery), 125)
    lib.usb_cdc_recovery_attempted(ctypes.byref(recovery), 125)
    assert recovery.attempts == 1 and recovery.deadline_ms == 2125
    lib.usb_cdc_recovery_arm(ctypes.byref(recovery), 200)
    assert recovery.deadline_ms == 2125
    lib.usb_cdc_recovery_attempted(ctypes.byref(recovery), 2125)
    lib.usb_cdc_recovery_attempted(ctypes.byref(recovery), 4125)
    assert not recovery.pending and recovery.attempts == 3
    lib.usb_cdc_recovery_arm(ctypes.byref(recovery), 5000)
    assert not recovery.pending
    lib.usb_cdc_recovery_connected(ctypes.byref(recovery))
    lib.usb_cdc_recovery_arm(ctypes.byref(recovery), 6000)
    assert recovery.pending and recovery.attempts == 0


def test_reset_and_close_keep_dma_owned_trbs_at_each_transfer_phase(tmp_path):
    lib = state_lib(tmp_path)
    for phase in ("ep0-setup", "ep0-data-in", "ep0-data-out",
                  "ep0-status-in", "ep0-status-out", "bulk-first",
                  "bulk-chained", "bulk-last"):
        assert not lib.usb_cdc_dma_may_release(True, -1, False), phase
        assert lib.usb_cdc_dma_may_release(True, 0, False), phase
        assert lib.usb_cdc_dma_may_release(True, -1, True), phase
    assert lib.usb_cdc_dma_may_release(False, -1, False)
