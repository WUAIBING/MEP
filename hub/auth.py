import base64
import binascii
import hashlib
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def derive_node_id(pub_pem: str) -> str:
    sha = hashlib.sha256(pub_pem.encode("utf-8")).hexdigest()
    return f"node_{sha[:12]}"


def verify_signature(pub_pem: str, payload_str: str, timestamp: str, signature_b64: str) -> bool:
    try:
        if abs(time.time() - float(timestamp)) > 300:
            return False
        public_key = serialization.load_pem_public_key(pub_pem.encode("utf-8"))
        if not isinstance(public_key, ed25519.Ed25519PublicKey):
            return False
        signature = base64.b64decode(signature_b64)
        message = f"{payload_str}{timestamp}".encode("utf-8")
        public_key.verify(signature, message)
        return True
    except (InvalidSignature, ValueError, TypeError, binascii.Error):
        return False
