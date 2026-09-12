"""Gateway-only credential storage; no business database or repository access."""

from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from app.provenance import canonical_json


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def management_auth(key: bytes, body: bytes, *, timestamp: int | None = None) -> str:
    stamp = str(timestamp if timestamp is not None else int(time.time()))
    signature = hmac.new(key, stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return stamp + "." + signature


def verify_management(key: bytes, body: bytes, authorization: str) -> None:
    try:
        timestamp = int(authorization.split(".")[0])
        if abs(time.time() - timestamp) > 60 or not hmac.compare_digest(
            management_auth(key, body, timestamp=timestamp), authorization
        ):
            raise ValueError()
    except (ValueError, IndexError):
        raise ValueError("Gateway management authorization rejected") from None


class GatewaySecretStore:
    def __init__(self, root: str, *, environment_key: str | None = None):
        self.root = Path(root)
        if self.root.is_symlink():
            raise ValueError("Gateway private storage is invalid")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.environment_key = environment_key
        master = self._private_file(
            "master.key", lambda: AESGCM.generate_key(bit_length=256)
        )
        self.aes = AESGCM(master)
        pem = self._private_file(
            "envelope.pem",
            lambda: rsa.generate_private_key(
                public_exponent=65537, key_size=3072
            ).private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        self.private_key = serialization.load_pem_private_key(pem, password=None)
        self.key_id = hashlib.sha256(pem).hexdigest()[:24]
        self.path = self.root / "credentials.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Gateway private storage is invalid")
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS credentials (reference TEXT PRIMARY KEY, ciphertext BLOB NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, connection_id TEXT NOT NULL, expires INTEGER NOT NULL, digest TEXT, reference TEXT)"
            )
        self.path.chmod(0o600)

    def _private_file(self, name, create):
        path = self.root / name
        if path.is_symlink():
            raise ValueError("Gateway key file is invalid")
        if not path.exists():
            value = create()
            try:
                fd = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as output:
                    output.write(value)
                    output.flush()
                    os.fsync(output.fileno())
        if path.stat().st_mode & 0o077:
            raise ValueError("Gateway key permissions must be private")
        return path.read_bytes()

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def grant(self, connection_id: str) -> dict:
        identifier = uuid4().hex
        expires = int(time.time()) + 300
        with self.connect() as db:
            db.execute(
                "DELETE FROM grants WHERE expires < ? AND reference IS NULL",
                (int(time.time()) - 3600,),
            )
            db.execute(
                "INSERT INTO grants (id, connection_id, expires) VALUES (?, ?, ?)",
                (identifier, connection_id, expires),
            )
        public = self.private_key.public_key().public_numbers()
        encode_int = lambda n: b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))
        return {
            "grant_id": identifier,
            "connection_id": connection_id,
            "expires_at": expires,
            "key_id": self.key_id,
            "public_key": {
                "kty": "RSA",
                "n": encode_int(public.n),
                "e": encode_int(public.e),
                "alg": "RSA-OAEP-256",
                "ext": True,
            },
        }

    def save(self, connection_id: str, envelope: dict) -> dict:
        digest = hashlib.sha256(canonical_json(envelope).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            grant = db.execute(
                "SELECT connection_id, expires, digest, reference FROM grants WHERE id=?",
                (envelope.get("grant_id"),),
            ).fetchone()
            if not grant or grant[0] != connection_id:
                raise ValueError("Credential grant rejected")
            if grant[3]:
                if grant[2] != digest:
                    raise ValueError("Credential grant already consumed")
                return {"credential_ref": grant[3], "configured": True}
            if grant[1] < time.time() or envelope.get("key_id") != self.key_id:
                raise ValueError("Credential grant expired")
            try:
                aad = canonical_json(
                    {
                        "connection_id": connection_id,
                        "grant_id": envelope["grant_id"],
                        "key_id": self.key_id,
                    }
                ).encode()
                key = self.private_key.decrypt(
                    unb64(envelope["wrapped_key"]),
                    padding.OAEP(
                        mgf=padding.MGF1(hashes.SHA256()),
                        algorithm=hashes.SHA256(),
                        label=None,
                    ),
                )
                secret = AESGCM(key).decrypt(
                    unb64(envelope["iv"]), unb64(envelope["ciphertext"]), aad
                )
                decoded = secret.decode()
                if not 1 <= len(secret) <= 4096 or any(
                    ord(c) < 33 or ord(c) > 126 for c in decoded
                ):
                    raise ValueError()
            except Exception:
                raise ValueError("Credential envelope rejected") from None
            reference = "cred-" + uuid4().hex
            iv = os.urandom(12)
            encrypted = iv + self.aes.encrypt(iv, secret, reference.encode())
            db.execute("INSERT INTO credentials VALUES (?, ?)", (reference, encrypted))
            db.execute(
                "UPDATE grants SET digest=?, reference=? WHERE id=?",
                (digest, reference, envelope["grant_id"]),
            )
        return {"credential_ref": reference, "configured": True}

    def read(self, reference: str) -> str:
        if reference == "env-nvidia":
            if not self.environment_key:
                raise ValueError("Gateway credential is not configured")
            return self.environment_key
        with self.connect() as db:
            row = db.execute(
                "SELECT ciphertext FROM credentials WHERE reference=?", (reference,)
            ).fetchone()
        if not row:
            raise ValueError("Gateway credential is not configured")
        encrypted = row[0]
        return self.aes.decrypt(
            encrypted[:12], encrypted[12:], reference.encode()
        ).decode()

    def has(self, reference: str) -> bool:
        try:
            self.read(reference)
            return True
        except ValueError:
            return False
