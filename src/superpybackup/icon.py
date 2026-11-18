"""Programmsymbol von SuperPyBackup: ein Datenträger (Festplatte) mit Pfeil, ohne Bilddateien zur Laufzeit erzeugt.

Die Farbe zeigt den Zustand: blau = bereit, orange = Sicherung/Arbeit läuft, rot = letzter Lauf fehlgeschlagen.
Verwendet für das Fenster (PNG), das Symbol im Infobereich (HICON) und beim Build für die EXE (ICO).
"""

from __future__ import annotations

import base64
import ctypes
import struct
import zlib
from ctypes import wintypes

IDLE, BUSY, ERROR = "idle", "busy", "error"
STATES = (IDLE, BUSY, ERROR)
COLORS = {  # (oben, unten)
    IDLE: ((0x3B, 0x8E, 0xF8), (0x0B, 0x3D, 0x91)),
    BUSY: ((0xFF, 0xB3, 0x40), (0xC2, 0x5E, 0x00)),
    ERROR: ((0xF0, 0x6A, 0x62), (0x9B, 0x1C, 0x1C)),
}
WHITE = (255, 255, 255)


def _inside_rounded(x: float, y: float, left: float, top: float, right: float, bottom: float, radius: float) -> bool:
    if not (left <= x <= right and top <= y <= bottom):
        return False
    cx = min(max(x, left + radius), right - radius)
    cy = min(max(y, top + radius), bottom - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def _overlay(x: float, y: float) -> float:
    """Weisser Anteil (0..1) der Zeichnung auf dem Gehaeuse: Pfeil, Trennlinie, Schlitz und Leuchtdiode."""
    if 0.455 <= x <= 0.545 and 0.21 <= y <= 0.37:  # Pfeilschaft
        return 1.0
    if 0.37 <= y <= 0.53 and abs(x - 0.5) <= (0.53 - y) * (0.19 / 0.16):  # Pfeilspitze
        return 1.0
    if 0.14 <= x <= 0.86 and 0.585 <= y <= 0.605:  # feine Trennlinie
        return 0.35
    if 0.16 <= x <= 0.62 and 0.675 <= y <= 0.725:  # Laufwerksschlitz
        return 0.8
    if (x - 0.79) ** 2 + (y - 0.70) ** 2 <= 0.055 ** 2:  # Leuchtdiode
        return 1.0
    return 0.0


def render(size: int, state: str = IDLE) -> bytes:
    """RGBA-Pixel (zeilenweise, oben links beginnend) mit Kantenglaettung durch Unterabtastung."""
    top, bottom = COLORS[state]
    samples = 3 if size <= 64 else 2
    total = samples * samples
    out = bytearray()
    for row in range(size):
        for col in range(size):
            covered = 0
            white = 0.0
            for sy in range(samples):
                for sx in range(samples):
                    x = (col + (sx + 0.5) / samples) / size
                    y = (row + (sy + 0.5) / samples) / size
                    if _inside_rounded(x, y, 0.05, 0.13, 0.95, 0.87, 0.15):
                        covered += 1
                        white += _overlay(x, y)
            if not covered:
                out += b"\x00\x00\x00\x00"
                continue
            mix = min(max((row / size - 0.13) / 0.74, 0.0), 1.0)
            base = [top[i] + (bottom[i] - top[i]) * mix for i in range(3)]
            share = white / covered
            color = [round(base[i] * (1 - share) + WHITE[i] * share) for i in range(3)]
            out += bytes((color[0], color[1], color[2], round(255 * covered / total)))
    return bytes(out)


def png_bytes(size: int = 64, state: str = IDLE) -> bytes:
    pixels = render(size, state)
    rows = b"".join(b"\x00" + pixels[y * size * 4:(y + 1) * size * 4] for y in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")


def png_base64(size: int = 64, state: str = IDLE) -> str:
    return base64.b64encode(png_bytes(size, state)).decode("ascii")


def ico_bytes(sizes: tuple[int, ...] = (16, 24, 32, 48, 64, 128), state: str = IDLE) -> bytes:
    """ICO-Datei mit PNG-Bildern (ab Windows Vista ueblich)."""
    images = [png_bytes(size, state) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for size, data in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return header + entries + b"".join(images)


def small_icon_size() -> int:
    """Kantenlaenge des kleinen Symbols laut Windows (Infobereich), mindestens 16."""
    try:
        return max(16, int(ctypes.windll.user32.GetSystemMetrics(49)))  # SM_CXSMICON
    except (AttributeError, OSError):
        return 16


def create_hicon(size: int = 32, state: str = IDLE) -> int:
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

        rgba = render(size, state)
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
