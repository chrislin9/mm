from __future__ import annotations

import base64
import time

from cryptography.hazmat.primitives.asymmetric import ed25519


class Signer:
    """Ed25519 request signing per https://docs.polymarket.us/api-reference/authentication."""

    def __init__(self, key_id: str, secret_b64: str):
        if not key_id or not secret_b64:
            raise ValueError("POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY must be set")
        raw = base64.b64decode(secret_b64)
        if len(raw) < 32:
            raise ValueError("secret key decodes to fewer than 32 bytes")
        self._sk = ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])
        self.key_id = key_id

    def headers(self, method: str, path: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        sig = base64.b64encode(self._sk.sign(f"{ts}{method.upper()}{path}".encode())).decode()
        return {
            "X-PM-Access-Key": self.key_id,
            "X-PM-Timestamp": ts,
            "X-PM-Signature": sig,
            "Content-Type": "application/json",
        }
