import base64
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hub.auth import derive_node_id, verify_signature


def _generate_keypair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    pub_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_key, pub_pem


def _sign(private_key: Ed25519PrivateKey, payload: str, timestamp: str) -> str:
    message = f"{payload}{timestamp}".encode("utf-8")
    signature = private_key.sign(message)
    return base64.b64encode(signature).decode("utf-8")


def test_derive_node_id_is_deterministic() -> None:
    _, pub_pem = _generate_keypair()
    assert derive_node_id(pub_pem) == derive_node_id(pub_pem)


def test_derive_node_id_changes_across_different_keys() -> None:
    _, pem_1 = _generate_keypair()
    _, pem_2 = _generate_keypair()
    assert derive_node_id(pem_1) != derive_node_id(pem_2)


def test_verify_signature_accepts_valid_signature() -> None:
    private_key, pub_pem = _generate_keypair()
    payload = '{"task":"hello"}'
    timestamp = str(int(time.time()))
    signature = _sign(private_key, payload, timestamp)

    assert verify_signature(pub_pem, payload, timestamp, signature) is True


def test_verify_signature_rejects_tampered_payload() -> None:
    private_key, pub_pem = _generate_keypair()
    payload = '{"task":"hello"}'
    timestamp = str(int(time.time()))
    signature = _sign(private_key, payload, timestamp)

    assert verify_signature(pub_pem, '{"task":"tampered"}', timestamp, signature) is False


def test_verify_signature_rejects_expired_timestamp() -> None:
    private_key, pub_pem = _generate_keypair()
    payload = "payload"
    timestamp = str(int(time.time()) - 301)
    signature = _sign(private_key, payload, timestamp)

    assert verify_signature(pub_pem, payload, timestamp, signature) is False


def test_verify_signature_rejects_invalid_base64() -> None:
    _, pub_pem = _generate_keypair()
    payload = "payload"
    timestamp = str(int(time.time()))

    assert verify_signature(pub_pem, payload, timestamp, "not-base64$$$") is False
