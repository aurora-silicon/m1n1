# SPDX-License-Identifier: MIT
import numpy as np


def p010_to_rgb(data, width, height, stride, luma_size):
    """Decode MSB-aligned P010 using video-range BT.709 coefficients."""
    if (width <= 0 or height <= 0 or width % 2 or height % 2 or
            stride < width * 2 or stride % 2 or luma_size % 2 or luma_size < height * stride):
        raise ValueError("Invalid P010 plane geometry")
    if len(data) < luma_size + stride * (height // 2):
        raise ValueError("Truncated P010 planes")

    def plane(offset, rows):
        words = np.frombuffer(data, dtype='<u2', count=rows * stride // 2,
                              offset=offset).reshape(rows, stride // 2)[:, :width]
        if np.any(words & 63):
            raise ValueError("Samples are not MSB-aligned 10-bit values")
        return words.astype(np.float32) / 256

    y = plane(0, height) - 16
    uv = plane(luma_size, height // 2)
    u = uv[:, 0::2].repeat(2, 0).repeat(2, 1) - 128
    v = uv[:, 1::2].repeat(2, 0).repeat(2, 1) - 128
    rgb = np.stack((1.164383 * y + 1.792741 * v,
                    1.164383 * y - .213249 * u - .532909 * v,
                    1.164383 * y + 2.112402 * u), axis=2)
    return np.rint(np.clip(rgb, 0, 255)).astype(np.uint8)
