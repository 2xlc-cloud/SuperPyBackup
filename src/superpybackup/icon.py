"""Programmsymbol von SuperPyBackup, ohne Bilddateien zur Laufzeit erzeugt (blaue Kachel mit Pfeil in eine Ablage).

Wird fuer das Fenster (PNG), das Symbol im Infobereich (HICON) und beim Build fuer die EXE (ICO) verwendet.
"""

from __future__ import annotations

import base64
import ctypes
import struct
import zlib
from ctypes import wintypes

TOP = (0x2F, 0x81, 0xF7)
BOTTOM = (0x0B, 0x3D, 0x91)
WHITE = (255, 255, 255)


def _inside_rounded(x: float, y: float, radius: float) -> bool:
    cx = min(max(x, radius), 1 - radius)
    cy = min(max(y, radius), 1 - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def _in_arrow(x: float, y: float) -> bool:
    if 0.455 <= x <= 0.545 and 0.17 <= y <= 0.47:  # Schaft
        return True
    if 0.46 <= y <= 0.69 and abs(x - 0.5) <= (0.69 - y) * (0.21 / 0.23):  # Spitze (Dreieck)
        return True
    if 0.24 <= x <= 0.76 and 0.745 <= y <= 0.825:  # Ablage: Boden
        return True
    if (0.24 <= x <= 0.32 or 0.68 <= x <= 0.76) and 0.64 <= y <= 0.825:  # Ablage: Seiten
        return True
    return False


def render(size: int) -> bytes:
    """RGBA-Pixel (zeilenweise, oben links beginnend) mit Kantenglaettung durch 3x3-Unterabtastung."""
    samples = 3
    out = bytearray()
    for row in range(size):
        for col in range(size):
            covered = white = 0
            for sy in range(samples):
                for sx in range(samples):
                    x = (col + (sx + 0.5) / samples) / size
                    y = (row + (sy + 0.5) / samples) / size
                    if _inside_rounded(x, y, 0.22):
                        covered += 1
                        if _in_arrow(x, y):
                            white += 1
            total = samples * samples
            if not covered:
                out += b"\x00\x00\x00\x00"
                continue
            mix = row / max(size - 1, 1)
            base = [TOP[i] + (BOTTOM[i] - TOP[i]) * mix for i in range(3)]
            share = white / covered
            color = [round(base[i] * (1 - share) + WHITE[i] * share) for i in range(3)]
            out += bytes((color[0], color[1], color[2], round(255 * covered / total)))
    return bytes(out)


def png_bytes(size: int = 64) -> bytes:
    pixels = render(size)
    rows = b"".join(b"\x00" + pixels[y * size * 4:(y + 1) * size * 4] for y in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")


def png_base64(size: int = 64) -> str:
    return base64.b64encode(png_bytes(size)).decode("ascii")


def ico_bytes(sizes: tuple[int, ...] = (16, 24, 32, 48, 64, 128)) -> bytes:
    """ICO-Datei mit PNG-Bildern (ab Windows Vista ueblich)."""
    images = [png_bytes(size) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for size, data in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return header + entries + b"".join(images)


def create_hicon(size: int = 32) -> int:
    """Erzeugt ein Windows-Symbol (HICON). Der Aufrufer gibt es mit DestroyIcon frei. 0 bei Fehler."""
    try:
        gdi32, user32 = ctypes.windll.gdi32, ctypes.windll.user32
        gdi32.CreateBitmap.restype = wintypes.HBITMAP
        gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
        user32.CreateIconIndirect.restype = wintypes.HICON
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        user32.DestroyIcon.argtypes = [wintypes.HICON]

        class ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]

        rgba = render(size)
        bgra = bytearray(len(rgba))
        bgra[0::4], bgra[1::4], bgra[2::4], bgra[3::4] = rgba[2::4], rgba[1::4], rgba[0::4], rgba[3::4]
        color_buffer = (ctypes.c_ubyte * len(bgra)).from_buffer(bgra)
        mask_buffer = (ctypes.c_ubyte * (size * ((size + 15) // 16 * 2)))()
        color = gdi32.CreateBitmap(size, size, 1, 32, ctypes.addressof(color_buffer))
        mask = gdi32.CreateBitmap(size, size, 1, 1, ctypes.addressof(mask_buffer))
        info = ICONINFO(True, 0, 0, mask, color)
        handle = user32.CreateIconIndirect(ctypes.byref(info))
        gdi32.DeleteObject(color)
        gdi32.DeleteObject(mask)
        return int(handle or 0)
    except (AttributeError, OSError):
        return 0
