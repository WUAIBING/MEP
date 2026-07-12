import base64
import json
import os
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

DM_ENVELOPE_PREFIX = "mepdmenc:"
DM_ENVELOPE_VERSION = 1
DM_ENVELOPE_ALG = "x25519-hkdf-sha256-aesgcm-v1"


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


def serialize_x25519_public_key(public_key: x25519.X25519PublicKey) -> str:
    return b64e(public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def parse_x25519_public_key(value: str) -> x25519.X25519PublicKey:
    raw = b64d(value)
    return x25519.X25519PublicKey.from_public_bytes(raw)


def _derive_aes_key(shared_secret: bytes, salt: bytes) -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"MEP-DM-E2EE-v1",
    )
    return hkdf.derive(shared_secret)


def encrypt_dm_payload(plaintext: str, recipient_public_key_b64: str) -> dict:
    recipient_public_key = parse_x25519_public_key(recipient_public_key_b64)
    ephemeral_private_key = x25519.X25519PrivateKey.generate()
    ephemeral_public_key = ephemeral_private_key.public_key()
    shared_secret = ephemeral_private_key.exchange(recipient_public_key)
    nonce = os.urandom(12)
    key = _derive_aes_key(shared_secret, nonce)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return {
        "enc_v": DM_ENVELOPE_VERSION,
        "enc_alg": DM_ENVELOPE_ALG,
        "ephemeral_pub": serialize_x25519_public_key(ephemeral_public_key),
        "nonce": b64e(nonce),
        "ciphertext": b64e(ciphertext),
    }


def decrypt_dm_payload(envelope: dict, recipient_private_key: x25519.X25519PrivateKey) -> str:
    if envelope.get("enc_v") != DM_ENVELOPE_VERSION:
        raise ValueError("Unsupported DM encryption version")
    if envelope.get("enc_alg") != DM_ENVELOPE_ALG:
        raise ValueError("Unsupported DM encryption algorithm")
    ephemeral_public = parse_x25519_public_key(str(envelope["ephemeral_pub"]))
    nonce = b64d(str(envelope["nonce"]))
    ciphertext = b64d(str(envelope["ciphertext"]))
    shared_secret = recipient_private_key.exchange(ephemeral_public)
    key = _derive_aes_key(shared_secret, nonce)
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")


def encode_dm_envelope(envelope: dict) -> str:
    payload = dict(envelope)
    payload["kind"] = "mep_dm_encrypted"
    return DM_ENVELOPE_PREFIX + json.dumps(payload, separators=(",", ":"))


def decode_dm_envelope(wire_payload: str) -> Optional[dict]:
    if not isinstance(wire_payload, str) or not wire_payload.startswith(DM_ENVELOPE_PREFIX):
        return None
    raw_json = wire_payload[len(DM_ENVELOPE_PREFIX) :]
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("kind") != "mep_dm_encrypted":
        return None
    required = {"enc_v", "enc_alg", "ephemeral_pub", "nonce", "ciphertext"}
    if not required.issubset(set(parsed.keys())):
        return None
    return parsed
