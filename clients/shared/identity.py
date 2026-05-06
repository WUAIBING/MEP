import base64
import hashlib
import os
import time
import warnings
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def _derive_node_id(pub_pem: str) -> str:
    sha = hashlib.sha256(pub_pem.encode("utf-8")).hexdigest()
    return f"node_{sha[:12]}"

 
def _identity_password_from_env() -> bytes | None:
    raw = os.getenv("MEP_IDENTITY_KEY_PASSWORD") or os.getenv("MEP_KEY_PASSWORD")
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    return value.encode("utf-8")


@dataclass
class MEPIdentity:
    key_path: str

    def __post_init__(self) -> None:
        self._private_key = self._load_or_create_key(self.key_path)
        self.pub_pem = self._public_pem(self._private_key)
        self.node_id = _derive_node_id(self.pub_pem)

    def _load_or_create_key(self, key_path: str) -> ed25519.Ed25519PrivateKey:
        os.makedirs(os.path.dirname(os.path.abspath(key_path)), exist_ok=True)
        if os.path.exists(key_path):
            with open(key_path, "rb") as f:
                data = f.read()
            password = _identity_password_from_env()
            is_encrypted = b"ENCRYPTED" in data
            if not is_encrypted:
                warnings.warn(
                    "MEP identity key is stored unencrypted at rest. Protect this file with strict filesystem permissions.",
                    RuntimeWarning,
                    stacklevel=2,
                )
            elif password is None:
                raise ValueError(
                    "Encrypted MEP identity key requires MEP_IDENTITY_KEY_PASSWORD (or MEP_KEY_PASSWORD)."
                )
            key = serialization.load_pem_private_key(data, password=password if is_encrypted else None)
            if not isinstance(key, ed25519.Ed25519PrivateKey):
                raise ValueError("Unsupported private key type")
            return key
        key = ed25519.Ed25519PrivateKey.generate()
        password = _identity_password_from_env()
        if password is None:
            encryption = serialization.NoEncryption()
            warnings.warn(
                "Generating unencrypted MEP identity key. Set MEP_IDENTITY_KEY_PASSWORD for encrypted at-rest key storage.",
                RuntimeWarning,
                stacklevel=2,
            )
        else:
            encryption = serialization.BestAvailableEncryption(password)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=encryption,
        )
        with open(key_path, "wb") as f:
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
