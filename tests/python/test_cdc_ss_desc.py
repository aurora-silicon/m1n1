import ctypes
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def build_descriptors(tmp_path):
    library = tmp_path / "libcdc_desc.dylib"
    subprocess.run(
        [
            "cc", "-shared", "-fPIC", "-DCDC_DESC_HOST_TEST",
            str(ROOT / "src/usb_cdc_ss_desc.c"), "-o", str(library),
        ],
        check=True,
    )
    lib = ctypes.CDLL(str(library))
    lib.usb_cdc_ss_config.argtypes = [
        ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
    ]
    lib.usb_cdc_ss_config.restype = ctypes.c_size_t
    lib.usb_cdc_ss_bos.argtypes = [ctypes.POINTER(ctypes.c_size_t)]
    lib.usb_cdc_ss_bos.restype = ctypes.POINTER(ctypes.c_uint8)
    return lib


def hs_configuration():
    raw = bytearray([9, 2, 0, 0, 4, 1, 0, 0xC0, 250])
    for function in range(2):
        raw += bytes([9, 4, function * 2, 0, 1, 2, 2, 0, 0])
        raw += bytes([7, 5, 0x81 + function * 2, 3, 64, 0, 10])
        raw += bytes([9, 4, function * 2 + 1, 0, 2, 10, 0, 0, 0])
        raw += bytes([7, 5, 2 + function * 2, 2, 0, 2, 10])
        raw += bytes([7, 5, 0x82 + function * 2, 2, 0, 2, 10])
    raw[2:4] = len(raw).to_bytes(2, "little")
    return raw


def descriptors(raw):
    offset = 0
    while offset < len(raw):
        length = raw[offset]
        assert length >= 2 and offset + length <= len(raw)
        yield raw[offset:offset + length]
        offset += length
    assert offset == len(raw)


def test_gen1_config_has_two_acm_functions_and_six_companions(tmp_path):
    lib = build_descriptors(tmp_path)
    source = hs_configuration()
    src = (ctypes.c_uint8 * len(source)).from_buffer(source)
    dst = (ctypes.c_uint8 * 256)()
    length = lib.usb_cdc_ss_config(src, len(source), dst, len(dst))
    assert length == len(source) + 36
    raw = bytes(dst[:length])
    parsed = list(descriptors(raw))
    assert int.from_bytes(raw[2:4], "little") == length
    assert raw[4] == 4 and raw[8] == 112
    endpoints = [d for d in parsed if d[1] == 5]
    companions = [d for d in parsed if d[1] == 0x30]
    assert len(endpoints) == len(companions) == 6
    for index, descriptor in enumerate(parsed):
        if descriptor[1] == 5:
            assert parsed[index + 1][1] == 0x30
            assert descriptor[4:6] == (b"\x40\x00" if descriptor[3] == 3 else b"\x00\x04")
            assert descriptor[6] == (9 if descriptor[3] == 3 else 0)
            assert parsed[index + 1][4:6] == (b"\x40\x00" if descriptor[3] == 3 else b"\x00\x00")


def test_bos_claims_gen1_without_ssp(tmp_path):
    lib = build_descriptors(tmp_path)
    length = ctypes.c_size_t()
    raw = bytes(lib.usb_cdc_ss_bos(ctypes.byref(length))[:length.value])
    parsed = list(descriptors(raw))
    assert length.value == 22
    assert raw[:5] == bytes([5, 0x0F, 22, 0, 2])
    assert [d[2] for d in parsed[1:]] == [2, 3]
    assert parsed[2][4:6] == b"\x0f\x00"


def test_invalid_or_truncated_config_fails_closed(tmp_path):
    lib = build_descriptors(tmp_path)
    source = hs_configuration()
    src = (ctypes.c_uint8 * len(source)).from_buffer(source)
    dst = (ctypes.c_uint8 * 256)()
    assert lib.usb_cdc_ss_config(src, len(source), dst, 10) == 0
    source[2] = 0
    assert lib.usb_cdc_ss_config(src, len(source), dst, len(dst)) == 0
