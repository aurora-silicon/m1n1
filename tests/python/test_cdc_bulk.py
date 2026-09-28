import ctypes
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def bulk_lib(tmp_path):
    library = tmp_path / "libcdc_bulk.dylib"
    subprocess.run(
        ["cc", "-shared", "-fPIC", "-DCDC_BULK_HOST_TEST",
         str(ROOT / "src/usb_cdc_bulk.c"), "-o", str(library)],
        check=True,
    )
    lib = ctypes.CDLL(str(library))
    lib.usb_cdc_nump.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    lib.usb_cdc_nump.restype = ctypes.c_int
    lib.usb_cdc_bulk_plan.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                      ctypes.c_size_t]
    lib.usb_cdc_bulk_plan.restype = ctypes.c_size_t
    lib.usb_cdc_bulk_retired_bytes.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                               ctypes.c_uint32, ctypes.c_bool]
    lib.usb_cdc_bulk_retired_bytes.restype = ctypes.c_int
    return lib


def test_nump_is_derived_and_bounded(tmp_path):
    lib = bulk_lib(tmp_path)
    assert lib.usb_cdc_nump(0, 0x10000) == -1
    assert lib.usb_cdc_nump(7 << 8, 0x10000) == -1
    assert lib.usb_cdc_nump(64 << 8, 0x10000) == -1
    assert lib.usb_cdc_nump(64 << 8, 0x10000000) == 16
    assert lib.usb_cdc_nump(128 << 8, 0x4000000) == 15


def test_bulk_plan_covers_zero_short_and_full_chains(tmp_path):
    lib = bulk_lib(tmp_path)
    segments = (ctypes.c_uint32 * 16)()
    assert lib.usb_cdc_bulk_plan(0, segments, 16) == 1
    assert segments[0] == 0
    assert lib.usb_cdc_bulk_plan(1025, segments, 16) == 1
    assert segments[0] == 1025
    assert lib.usb_cdc_bulk_plan(16 * 16384, segments, 16) == 16
    assert list(segments) == [16384] * 16
    assert lib.usb_cdc_bulk_plan(16 * 16384 + 1, segments, 16) == 0
    assert lib.usb_cdc_bulk_plan(32769, segments, 2) == 0


def test_short_out_frame_is_readable_before_rest_of_chain(tmp_path):
    lib = bulk_lib(tmp_path)
    segments = (ctypes.c_uint32 * 16)()
    assert lib.usb_cdc_bulk_plan(256 * 1024, segments, 16) == 16
    assert lib.usb_cdc_bulk_retired_bytes(segments[0], segments[0], 0, True) == -1
    assert lib.usb_cdc_bulk_retired_bytes(segments[0], segments[0] - 16, 0, False) == 16
    assert lib.usb_cdc_bulk_retired_bytes(segments[1], segments[1], 0, True) == -1
    assert lib.usb_cdc_bulk_retired_bytes(segments[0], segments[0] + 1, 0, False) == -2
