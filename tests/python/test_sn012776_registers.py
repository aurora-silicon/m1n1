import pytest

from m1n1.hw.codecs import E_PWR_MODE, R_MODE_CTRL, R_PWR_CTL


def test_sn012776_shutdown_preserves_sense_power():
    # Asahi's TAS2764/SN012776 default: both sense paths off, shutdown.
    control = R_MODE_CTRL(0x1a)
    assert control.MODE == E_PWR_MODE.SHUTDOWN
    assert control.ISNS_PD == 1
    assert control.VSNS_PD == 1
    control.MODE = E_PWR_MODE.MUTE
    assert int(control) == 0x19
    control.ISNS_PD = 0
    assert int(control) == 0x09
    control.VSNS_PD = 0
    assert int(control) == 0x01


def test_sn012776_rejects_reserved_power_mode():
    with pytest.raises(ValueError):
        R_MODE_CTRL(0x1c)


def test_tas5770_sense_fields_remain_unchanged():
    control = R_PWR_CTL(0x0e)
    assert control.ISNS_PD == 1
    assert control.VSNS_PD == 1
    assert control.MODE == E_PWR_MODE.SHUTDOWN
