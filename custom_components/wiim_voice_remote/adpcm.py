"""IMA ADPCM decoder for WiiM Voice Remote BLE audio packets."""

from __future__ import annotations

import struct

STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
    34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
    157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658,
    724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024,
    3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
)

INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8)


def decode_wiim_adpcm_packet(pkt: bytes | bytearray) -> bytes:
    """Decode a 131-byte WiiM BLE HID Report 3 packet to 512 bytes of 16 kHz 16-bit LE mono PCM.

    Packet layout:
      - bytes[0:2]: big-endian int16 IMA ADPCM predictor
      - byte[2]:    uint8 IMA ADPCM step_index (0..88)
      - bytes[3:]:  128 bytes of 4-bit IMA ADPCM samples (low nibble first -> 256 samples)
    """
    if len(pkt) < 4:
        return b""

    pred = struct.unpack(">h", pkt[0:2])[0]
    idx = pkt[2]
    if idx > 88:
        idx = 88

    out = bytearray((len(pkt) - 3) * 4)
    pos = 0
    for b in pkt[3:]:
        for nib in (b & 0x0F, (b >> 4) & 0x0F):
            step = STEP_TABLE[idx]
            diff = step >> 3
            if nib & 1:
                diff += step >> 2
            if nib & 2:
                diff += step >> 1
            if nib & 4:
                diff += step
            if nib & 8:
                pred -= diff
            else:
                pred += diff

            if pred > 32767:
                pred = 32767
            elif pred < -32768:
                pred = -32768

            idx += INDEX_TABLE[nib]
            if idx < 0:
                idx = 0
            elif idx > 88:
                idx = 88

            struct.pack_into("<h", out, pos, pred)
            pos += 2

    return bytes(out)
