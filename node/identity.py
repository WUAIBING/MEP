import os
import time
import base64
import hashlib
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

class MEPIdentity:
    def __init__(self, key_path="private.pem"):
        self.key_path = key_path
        legacy_enc_key_path = key_path.replace(".pem", "_enc.pem")
        modern_enc_key_path = f"{key_path}.x25519.pem"
        if os.path.exists(legacy_enc_key_path):
            self.enc_key_path = legacy_enc_key_path
        elif os.path.exists(modern_enc_key_path):
            self.enc_key_path = modern_enc_key_path
        else:
            self.enc_key_path = legacy_enc_key_path
        self.generated_new_key = False
        self._load_or_generate()
        
    def _load_or_generate(self):
        # 1. Signing Key (Ed25519)
        if os.path.exists(self.key_path):
            with open(self.key_path, "rb") as f:
                self.private_key = serialization.load_pem_private_key(f.read(), password=None)
        else:
            self.generated_new_key = True
            self.private_key = ed25519.Ed25519PrivateKey.generate()
            with open(self.key_path, "wb") as f:
                f.write(self.private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption()
                ))
                
        self.public_key = self.private_key.public_key()
        self.pub_pem = self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode('utf-8')
        
        sha = hashlib.sha256(self.pub_pem.encode('utf-8')).hexdigest()
        self.node_id = f"node_{sha[:12]}"

        # 2. Encryption Key (X25519)
        if os.path.exists(self.enc_key_path):
            with open(self.enc_key_path, "rb") as f:
                self.private_enc_key = serialization.load_pem_private_key(f.read(), password=None)
        else:
            self.private_enc_key = x25519.X25519PrivateKey.generate()
            with open(self.enc_key_path, "wb") as f:
                f.write(self.private_enc_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption()
                ))
        
        self.public_enc_key = self.private_enc_key.public_key()
        self.x25519_public_key = base64.b64encode(self.public_enc_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )).decode('utf-8')
        
    def sign(self, payload: str, timestamp: str) -> str:
        message = f"{payload}{timestamp}".encode('utf-8')
        signature = self.private_key.sign(message)
        return base64.b64encode(signature).decode('utf-8')
        
    def get_auth_headers(self, payload: str) -> dict:
        ts = str(int(time.time()))
        sig = self.sign(payload, ts)
        return {
            "X-MEP-NodeID": self.node_id,
            "X-MEP-Timestamp": ts,
            "X-MEP-Signature": sig
        }

    def decrypt_from_peer(self, peer_pubkey_bytes: bytes, encrypted_b64: str) -> str:
        """Decrypts a message from a peer using X25519 ECDH + AES-GCM."""
        try:
            peer_public_key = x25519.X25519PublicKey.from_public_bytes(peer_pubkey_bytes)
            shared_key = self.private_enc_key.exchange(peer_public_key)
            
            # Derive AES key
            derived_key = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=None,
                info=b'mep-data-exchange',
            ).derive(shared_key)
            
            encrypted_data = base64.b64decode(encrypted_b64)
            iv = encrypted_data[:12]
            tag = encrypted_data[12:28] # 16 bytes tag
            ciphertext = encrypted_data[28:]
            
            decryptor = Cipher(
                algorithms.AES(derived_key),
                modes.GCM(iv, tag),
            ).decryptor()
            
            return (decryptor.update(ciphertext) + decryptor.finalize()).decode('utf-8')
        except Exception as e:
            raise ValueError(f"Decryption failed: {e}")

    def encrypt_for_peer(self, peer_pubkey_b64: str, message: str) -> str:
        """Encrypts a message for a peer."""
        peer_bytes = base64.b64decode(peer_pubkey_b64)
        peer_public_key = x25519.X25519PublicKey.from_public_bytes(peer_bytes)
        shared_key = self.private_enc_key.exchange(peer_public_key)
        
        derived_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b'mep-data-exchange',
        ).derive(shared_key)
        
        iv = os.urandom(12)
        encryptor = Cipher(
            algorithms.AES(derived_key),
            modes.GCM(iv),
        ).encryptor()
        
        ciphertext = encryptor.update(message.encode('utf-8')) + encryptor.finalize()
        
        # Format: IV (12) + Tag (16) + Ciphertext
        return base64.b64encode(iv + encryptor.tag + ciphertext).decode('utf-8')
