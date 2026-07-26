import base64
import hashlib
import os
import time
import warnings
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from clients.shared.dm_crypto import serialize_x25519_public_key


def _derive_node_id(pub_pem: str) -> str:
    sha = hashlib.sha256(pub_pem.encode("utf-8")).hexdigest()
    return f"node_{sha[:12]}"


@dataclass
class MEPIdentity:
    key_path: str

    def __post_init__(self) -> None:
        self._private_key = self._load_or_create_key(self.key_path)
        self._x25519_private_key = self._load_or_create_x25519_key(self.key_path)
        self.pub_pem = self._public_pem(self._private_key)
        self.node_id = _derive_node_id(self.pub_pem)
        self.x25519_public_key = serialize_x25519_public_key(self._x25519_private_key.public_key())

    def _load_or_create_key(self, key_path: str) -> ed25519.Ed25519PrivateKey:
        os.makedirs(os.path.dirname(os.path.abspath(key_path)), exist_ok=True)
        if os.path.exists(key_path):
            with open(key_path, "rb") as f:
                data = f.read()
            if b"ENCRYPTED" not in data:
                warnings.warn(
                    "MEP identity key is stored unencrypted at rest. Protect this file with strict filesystem permissions.",
                    RuntimeWarning,
                    stacklevel=2,
                )
            key = serialization.load_pem_private_key(data, password=None)
            if not isinstance(key, ed25519.Ed25519PrivateKey):
                raise ValueError("Unsupported private key type")
            return key
        key = ed25519.Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        warnings.warn(
            "Generating unencrypted MEP identity key. Use strict filesystem permissions or switch to encrypted-key handling for production.",
            RuntimeWarning,
            stacklevel=2,
        )
        with open(key_path, "wb") as f:
            f.write(pem)
        return key

    def _load_or_create_x25519_key(self, key_path: str) -> x25519.X25519PrivateKey:
        legacy_path = key_path.replace(".pem", "_enc.pem")
        modern_path = f"{key_path}.x25519.pem"
        if os.path.exists(legacy_path) and os.path.exists(modern_path):
            warnings.warn(
                "Both legacy and modern X25519 sidecars exist; selecting the legacy _enc.pem key. "
                "Remove the stale sidecar only after confirming the Hub registration and peer encryption key.",
                RuntimeWarning,
                stacklevel=2,
            )
        if os.path.exists(legacy_path):
            x25519_path = legacy_path
        elif os.path.exists(modern_path):
            x25519_path = modern_path
        else:
            # The node runtime has historically used `_enc.pem`. Creating the
            # same sidecar here keeps one signing identity paired with one
            # encryption identity across the runtime and shared client.
            x25519_path = legacy_path
        os.makedirs(os.path.dirname(os.path.abspath(x25519_path)), exist_ok=True)
        if os.path.exists(x25519_path):
            with open(x25519_path, "rb") as f:
                data = f.read()
            key = serialization.load_pem_private_key(data, password=None)
            if not isinstance(key, x25519.X25519PrivateKey):
                raise ValueError("Unsupported X25519 private key type")
            return key
        key = x25519.X25519PrivateKey.generate()
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        with open(x25519_path, "wb") as f:
            f.write(pem)
        return key

    def _public_pem(self, key: ed25519.Ed25519PrivateKey) -> str:
        public_key = key.public_key()
        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return public_pem.decode("utf-8")

    def sign(self, payload: str, timestamp: str) -> str:
        message = f"{payload}{timestamp}".encode("utf-8")
        signature = self._private_key.sign(message)
        return base64.b64encode(signature).decode("utf-8")

    def get_auth_headers(self, payload_str: str) -> dict:
        timestamp = str(int(time.time()))
        signature = self.sign(payload_str, timestamp)
        return {
            "X-MEP-NodeID": self.node_id,
            "X-MEP-Timestamp": timestamp,
            "X-MEP-Signature": signature,
        }

    @property
    def x25519_private_key(self) -> x25519.X25519PrivateKey:
        return self._x25519_private_key
