"""Ed25519-Signaturen (RFC 8032) in reinem Python - ohne zusaetzliche Pakete.

Dient der Pruefung der Update-Signatur (SHA256SUMS.txt.sig) und dem Signieren beim Veroeffentlichen (build/sign_release.py).
Die Umsetzung folgt dem Referenzcode des RFC; sie ist langsam (Millisekunden bis Zehntelsekunden), aber fuer eine einzelne
Signatur je Update voellig ausreichend. Gegen die Testvektoren des RFC 8032 geprueft (tests/test_signing.py).
"""

from __future__ import annotations

import hashlib

P = 2**255 - 19
Q = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _inv(x: int) -> int:
    return pow(x, P - 2, P)


def _add(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    a1 = (a[1] - a[0]) * (b[1] - b[0]) % P
    b1 = (a[1] + a[0]) * (b[1] + b[0]) % P
    c1 = 2 * a[3] * b[3] * D % P
    d1 = 2 * a[2] * b[2] % P
    e, f, g, h = b1 - a1, d1 - c1, d1 + c1, b1 + a1
    return e * f % P, g * h % P, f * g % P, e * h % P


def _mul(scalar: int, point: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    result = (0, 1, 1, 0)
    while scalar > 0:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _equal(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % P == 0 and (a[1] * b[2] - b[1] * a[2]) % P == 0


def _recover_x(y: int, sign: int) -> int | None:
    if y >= P:
        return None
    x2 = (y * y - 1) * _inv(D * y * y + 1) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P != 0:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


_GY = 4 * _inv(5) % P
_GX = _recover_x(_GY, 0) or 0
_BASE = (_GX, _GY, 1, _GX * _GY % P)


def _compress(point: tuple[int, int, int, int]) -> bytes:
    zinv = _inv(point[2])
    x = point[0] * zinv % P
    y = point[1] * zinv % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(data: bytes) -> tuple[int, int, int, int] | None:
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


def _hash_mod_q(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % Q


def _expand(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("Der geheime Schluessel (Seed) muss 32 Bytes lang sein.")
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:]


def public_key(seed: bytes) -> bytes:
    scalar, _ = _expand(seed)
    return _compress(_mul(scalar, _BASE))


def sign(seed: bytes, message: bytes) -> bytes:
    scalar, prefix = _expand(seed)
    public = _compress(_mul(scalar, _BASE))
    r = _hash_mod_q(prefix + message)
    commitment = _compress(_mul(r, _BASE))
    h = _hash_mod_q(commitment + public + message)
    s = (r + h * scalar) % Q
    return commitment + int.to_bytes(s, 32, "little")


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """True nur bei gueltiger Signatur; jede Fehleingabe ergibt False (nie eine Ausnahme)."""
    try:
        if len(public) != 32 or len(signature) != 64:
            return False
        point = _decompress(public)
        commitment = _decompress(signature[:32])
        if point is None or commitment is None:
            return False
        s = int.from_bytes(signature[32:], "little")
        if s >= Q:
            return False
        h = _hash_mod_q(signature[:32] + public + message)
        return _equal(_mul(s, _BASE), _add(commitment, _mul(h, point)))
    except (ValueError, TypeError, ZeroDivisionError):
        return False
