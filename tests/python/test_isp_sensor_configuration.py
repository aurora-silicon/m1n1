import struct

import pytest

from m1n1.fw.isp.isp_sensor import T6040SensorConfiguration


def config():
    raw = bytearray(0x1c)
    struct.pack_into('<2I', raw, 0, 0, 3)
    struct.pack_into('<I', raw, 0xc, 1)
    return bytes(raw)


def model(sensor=0x958, presets=6):
    calls = []
    def query(name, opcode, size, params=(), **kwargs):
        calls.append((name, opcode, size, params, kwargs))
        raw = bytearray(size)
        if name == 'CH_INFO_GET':
            struct.pack_into('<I', raw, 0x20, sensor)
            struct.pack_into('<I', raw, 0x60, presets)
        return bytes(raw)
    return T6040SensorConfiguration(query, 15), calls


def test_qualified_sequence_and_dsid_inactive_sentinels():
    sensor, calls = model()
    assert sensor.start(config()) == (0x958, 6)
    params = calls[0][3]
    assert params[:2] == [8, 0x2fc]
    addresses = [params[2 + i*2] | params[3 + i*2] << 32 for i in range(8)]
    assert addresses == [0x2201d4000 + i*0x2000000 for i in range(4)] + [(1<<64)-1]*4
    assert calls[0][4] == {'outsize': 0}
    assert [c[0] for c in calls] == ['DSID_MULTI_BASE_SET','START','CH_INFO_GET']
    assert calls[1][3] == (0x11,)
    sensor.preset(5)
    assert calls[-1][:4] == ('CH_CAMERA_CONFIG_GET_5', 0x106, 0x120, (0,5))
    with pytest.raises(ValueError, match='already attempted'):
        sensor.start(config())
    assert len(calls) == 4


@pytest.mark.parametrize('mask', [0, 7, 16, True])
def test_unqualified_mcc_mask_no_query(mask):
    calls = []
    with pytest.raises(ValueError):
        T6040SensorConfiguration(lambda *args: calls.append(args), mask)
    assert not calls


@pytest.mark.parametrize('raw', [bytes(4), bytes(0x1c), config()[:-1]])
def test_invalid_config_before_mutating_queries(raw):
    sensor, calls = model()
    with pytest.raises(ValueError):
        sensor.start(raw)
    assert not calls and not sensor.start_attempted


@pytest.mark.parametrize('sensor_id,presets', [(0x957,6), (0x958,0), (0x958,65)])
def test_bad_sensor_latches_start_and_prevents_preset(sensor_id, presets):
    sensor, calls = model(sensor_id, presets)
    with pytest.raises(ValueError):
        sensor.start(config())
    with pytest.raises(ValueError):
        sensor.preset(0)
    with pytest.raises(ValueError):
        sensor.start(config())
    assert len(calls) == 3


@pytest.mark.parametrize('index', [-1, 6, True])
def test_bad_preset_has_no_command(index):
    sensor, calls = model()
    sensor.start(config())
    with pytest.raises(ValueError):
        sensor.preset(index)
    assert len(calls) == 3


def test_start_fault_does_not_allow_repeated_mutation():
    sensor, calls = model()
    def fail(*args, **kwargs):
        calls.append(args)
        raise RuntimeError('transport fault')
    sensor.query = fail
    with pytest.raises(RuntimeError):
        sensor.start(config())
    with pytest.raises(ValueError):
        sensor.start(config())
    assert len(calls) == 1
