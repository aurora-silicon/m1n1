# SPDX-License-Identifier: MIT
"""Sensor discovery for the qualified T6040/IMX958 firmware profile."""
import struct


class T6040SensorConfiguration:
    def __init__(self, query, active_mcc_mask):
        if type(active_mcc_mask) is not int or active_mcc_mask != 15:
            raise ValueError('Unqualified T6040 MCC mask')
        self.query = query
        self.start_attempted = False
        self.preset_count = None

    def start(self, config):
        if self.start_attempted:
            raise ValueError('Sensor start already attempted; cold recovery required')
        if (len(config) != 0x1c or struct.unpack_from('<2I', config) != (0, 3) or
                struct.unpack_from('<I', config, 0xc)[0] != 1):
            raise ValueError('Expected one camera channel in CONFIG_GET')
        params = [8, 0x2fc]
        for index in range(8):
            address = 0x2201d4000 + index * 0x2000000 if index < 4 else (1 << 64) - 1
            params.extend((address & 0xffffffff, address >> 32))
        self.start_attempted = True
        self.query('DSID_MULTI_BASE_SET', 0x3206, 0x50, params, outsize=0)
        self.query('START', 0, 0xc, (0x11,))
        info = self.query('CH_INFO_GET', 0x10d, 0x190, (0,))
        if len(info) != 0x190:
            raise ValueError('Truncated sensor information')
        sensor = struct.unpack_from('<I', info, 0x20)[0]
        presets = struct.unpack_from('<I', info, 0x60)[0]
        if sensor != 0x958 or not 0 < presets <= 64:
            raise ValueError('Unqualified sensor or preset count')
        self.preset_count = presets
        return sensor, presets

    def preset(self, index):
        if (self.preset_count is None or type(index) is not int or
                not 0 <= index < self.preset_count):
            raise ValueError('Preset outside qualified sensor configuration')
        return self.query('CH_CAMERA_CONFIG_GET_' + str(index), 0x106, 0x120, (0, index))
