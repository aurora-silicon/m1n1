# SPDX-License-Identifier: MIT
from io import BytesIO
from types import SimpleNamespace

import pytest

from m1n1.fw.afk.epic import EPICCategory, EPICError
from m1n1.fw.aop.aopep import AOPLASService


def service():
    ep = SimpleNamespace(name='las', asc=SimpleNamespace(iface=None))
    return AOPLASService(ep)


def test_lid_angle_report_from_live_capture():
    las = service()
    assert las.last_report is None
    assert las.handle_report(EPICCategory.REPORT, 0xc4, 0, BytesIO(bytes.fromhex('017000')))
    assert las.last_report.angle == 112
    assert las.last_report.report_id == 1
    assert las.last_report.unknown == b'\0'


@pytest.mark.parametrize('payload', [b'', b'\1'])
def test_truncated_report_does_not_replace_measurement(payload):
    las = service()
    las.handle_report(EPICCategory.REPORT, 0xc4, 0, BytesIO(bytes.fromhex('017000')))
    previous = las.last_report
    with pytest.raises(EPICError, match='failed to parse report'):
        las.handle_report(EPICCategory.REPORT, 0xc4, 1, BytesIO(payload))
    assert las.last_report is previous
