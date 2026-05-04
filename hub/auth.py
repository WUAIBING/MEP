import base64
import binascii
import hashlib
import time
from threading import Lock

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

# Nonce cache: prevents replay attacks within the 300-second skew window.
# Keyed by (node_id, signature_b64); entries expire after 300s.
# Uses a simple dict + eviction sweep instead of a TTL cache library
# to avoid adding a dependency.
_nonce_cache: dict[str, float] = {}
_nonce_lock = Lock()
_NONCE_TTL = 300  # seconds, matches timestamp skew window


def _evict_expired_nonces(now: float) -> None:
    expired = [k for k, ts in _nonce_cache.items() if now - ts > _NONCE_TTL]
    for k in expired:
        del _nonce_cache[k]


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

        # Anti-replay: reject duplicate signatures within the skew window
        node_id = derive_node_id(pub_pem)
        cache_key = f"{node_id}:{signature_b64}"
        now = time.time()
        with _nonce_lock:
            _evict_expired_nonces(now)
            if cache_key in _nonce_cache:
                return False
            _nonce_cache[cache_key] = now

        return True
    except (InvalidSignature, ValueError, TypeError, binascii.Error):
        return False
