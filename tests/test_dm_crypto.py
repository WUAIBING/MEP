from cryptography.hazmat.primitives.asymmetric import x25519

from clients.shared.dm_crypto import (
    decode_dm_envelope,
    decrypt_dm_payload,
    encode_dm_envelope,
    encrypt_dm_payload,
    serialize_x25519_public_key,
)


def test_dm_crypto_roundtrip() -> None:
    receiver_private = x25519.X25519PrivateKey.generate()
    receiver_public_b64 = serialize_x25519_public_key(receiver_private.public_key())
    plaintext = "hello encrypted dm"

    envelope = encrypt_dm_payload(plaintext, receiver_public_b64)
    wire = encode_dm_envelope(envelope)
    parsed = decode_dm_envelope(wire)

    assert parsed is not None
    decrypted = decrypt_dm_payload(parsed, receiver_private)
    assert decrypted == plaintext


def test_decode_dm_envelope_rejects_plaintext() -> None:
    assert decode_dm_envelope("hello world") is None
