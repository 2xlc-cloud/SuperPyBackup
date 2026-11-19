"""Ed25519 gegen die Testvektoren des RFC 8032."""

from __future__ import annotations

import pytest

from superpybackup import ed25519

VECTORS = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60", "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb", "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
]


@pytest.mark.parametrize("seed, public, message, signature", VECTORS)
def test_rfc8032_vectors(seed, public, message, signature):
    seed_b, public_b, message_b, signature_b = (bytes.fromhex(x) for x in (seed, public, message, signature))
    assert ed25519.public_key(seed_b) == public_b
    assert ed25519.sign(seed_b, message_b) == signature_b
    assert ed25519.verify(public_b, message_b, signature_b) is True


def test_tampering_is_detected():
    seed = bytes(range(32))
    public = ed25519.public_key(seed)
    message = b"SHA256SUMS.txt Inhalt"
    signature = ed25519.sign(seed, message)
    assert ed25519.verify(public, message, signature)
    assert not ed25519.verify(public, message + b"x", signature)
    assert not ed25519.verify(public, message, signature[:-1] + bytes([signature[-1] ^ 1]))
    assert not ed25519.verify(public, message, bytes([signature[0] ^ 1]) + signature[1:])
    other = ed25519.public_key(bytes(reversed(range(32))))
    assert not ed25519.verify(other, message, signature)  # falscher Schluessel


def test_malformed_input_never_raises():
    seed = bytes(range(32))
    public = ed25519.public_key(seed)
    signature = ed25519.sign(seed, b"x")
    for bad in (b"", b"\x00" * 63, b"\xff" * 64, signature + b"0", b"\x00" * 64):
        assert ed25519.verify(public, b"x", bad) is False
    for bad_public in (b"", b"\x00" * 31, b"\xff" * 32, b"\x01" + b"\x00" * 31):
        assert ed25519.verify(bad_public, b"x", signature) is False
    assert ed25519.verify(public, b"x", b"\xff" * 32 + (ed25519.Q + 1).to_bytes(32, "little")) is False  # s >= q
    with pytest.raises(ValueError):
        ed25519.sign(b"zu kurz", b"x")
