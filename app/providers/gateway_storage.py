"""Fail-closed Gateway storage. The legacy Publisher vault is deliberately unchanged.

The service never initializes storage. Maintenance requires an exclusive lifetime
lease, stages a complete generation, then fsyncs one atomic pointer replacement.
Only explicit new-install/import operations create schemas or cryptographic keys.
"""
from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from typing import Iterator
from uuid import uuid4

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.provenance import canonical_json, content_hash
from app.providers.model_secrets import GatewaySecretStore, b64, unb64


VERSION = "gateway-storage-v1"
FILES = ("credentials.sqlite3", "master.key", "envelope.pem", "identity.json")
_ID = re.compile(r"[a-f0-9]{32}\Z")
_COLUMNS = {
    "credentials": [("reference", "TEXT", 0, 1), ("ciphertext", "BLOB", 1, 0)],
    "grants": [("id", "TEXT", 0, 1), ("connection_id", "TEXT", 1, 0),
               ("expires", "INTEGER", 1, 0), ("digest", "TEXT", 0, 0), ("reference", "TEXT", 0, 0)],
    "gateway_identity": [("identity", "TEXT", 1, 1)],
}


class GatewayStorageError(Exception):
    """An internal deterministic failure; never include OS/SQLite/crypto details."""
    def __init__(self, reason: str = "unavailable") -> None:
        self.code = "gateway_storage_" + reason
        super().__init__("Credential storage unavailable; explicit configuration may be required")


def _is_ephemeral(path: Path) -> bool:
    roots = (Path("/tmp"), Path("/private/tmp"), Path("/var/tmp"), Path("/dev/shm"),
             Path("/private/var/folders"), Path("/var/folders"), Path(tempfile.gettempdir()))
    return any(path == p.resolve() or path.is_relative_to(p.resolve()) for p in roots)


def persistent_root(value: str | Path) -> Path:
    """No blank/relative paths, symlink components or untrusted writable parents."""
    try:
        return _persistent_root(value)
    except OSError:
        raise GatewayStorageError("unavailable") from None


def _persistent_root(value: str | Path) -> Path:
    path = Path(value)
    if not str(value).strip() or not path.is_absolute() or ".." in path.parts:
        raise GatewayStorageError("path_invalid")
    if _is_ephemeral(path.resolve()):
        raise GatewayStorageError("path_ephemeral")
    for parent in (*reversed(path.parents), path):
        if not parent.exists() and not parent.is_symlink():
            continue
        s = parent.lstat()
        sticky_system = s.st_uid == 0 and bool(s.st_mode & stat.S_ISVTX)
        if (not stat.S_ISDIR(s.st_mode) or parent.is_symlink()
                or s.st_uid not in {0, os.geteuid()}
                or (s.st_mode & 0o022 and not sticky_system)):
            raise GatewayStorageError("path_invalid")
    return path


def _metadata(path: Path, *, directory: bool = False) -> tuple[int, int]:
    try:
        s = path.lstat()
        kind = stat.S_ISDIR if directory else stat.S_ISREG
        if not kind(s.st_mode) or s.st_uid != os.geteuid() or stat.S_IMODE(s.st_mode) != (0o700 if directory else 0o600):
            raise GatewayStorageError("permissions")
        if not directory and s.st_nlink != 1:
            raise GatewayStorageError("path_invalid")
        return s.st_dev, s.st_ino
    except OSError:
        raise GatewayStorageError("missing") from None


def _read(path: Path, limit: int = 16384) -> bytes:
    before = _metadata(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            s = os.fstat(source.fileno())
            if (s.st_dev, s.st_ino) != before or s.st_size > limit:
                raise GatewayStorageError("identity_changed")
            result = source.read(limit + 1)
        if _metadata(path) != before or len(result) > limit:
            raise GatewayStorageError("identity_changed")
        return result
    except OSError:
        raise GatewayStorageError("unavailable") from None


def _json(path: Path) -> dict:
    try:
        result = json.loads(_read(path))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeError):
        raise GatewayStorageError("identity_invalid") from None


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())


def _keys(path: Path) -> tuple[bytes, rsa.RSAPrivateKey, dict]:
    try:
        master = _read(path / "master.key", 32)
        key = serialization.load_pem_private_key(_read(path / "envelope.pem"), password=None)
        identity = _json(path / "identity.json")
        if len(master) != 32 or not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 3072:
            raise ValueError()
        payload = {k: v for k, v in identity.items() if k != "signature"}
        if payload.get("version") != VERSION or not _ID.fullmatch(payload.get("store_id", "")):
            raise ValueError()
        key.public_key().verify(unb64(identity["signature"]), canonical_json(payload).encode(),
                                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
        check = unb64(payload["key_check"])
        if AESGCM(master).decrypt(check[:12], check[12:], payload["store_id"].encode()) != VERSION.encode():
            raise ValueError()
        return master, key, identity
    except (ValueError, KeyError, TypeError, InvalidSignature, InvalidTag):
        raise GatewayStorageError("key_mismatch") from None


def _identity(master: bytes, key: rsa.RSAPrivateKey, store_id: str) -> dict:
    nonce = os.urandom(12)
    payload = {"version": VERSION, "store_id": store_id,
               "key_check": b64(nonce + AESGCM(master).encrypt(nonce, VERSION.encode(), store_id.encode()))}
    signature = key.sign(canonical_json(payload).encode(),
                         padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    return {**payload, "signature": b64(signature)}


def _schema(db: sqlite3.Connection, identity: dict | None) -> None:
    expected = _COLUMNS if identity else {k: v for k, v in _COLUMNS.items() if k != "gateway_identity"}
    objects = list(db.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    if {n for n, t in objects if t == "table"} != set(expected) or any(t not in {"table", "index"} for _, t in objects):
        raise GatewayStorageError("schema_invalid")
    for table, columns in expected.items():
        actual = [(r[1], r[2], r[3], r[5]) for r in db.execute(f'PRAGMA table_info("{table}")')]
        if actual != columns:
            raise GatewayStorageError("schema_invalid")
    if db.execute("PRAGMA user_version").fetchone()[0] != (1 if identity else 0):
        raise GatewayStorageError("schema_invalid")
    if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
        raise GatewayStorageError("journal_unsupported")
    if list(db.execute("PRAGMA integrity_check")) != [("ok",)]:
        raise GatewayStorageError("integrity")
    if identity and list(db.execute("SELECT identity FROM gateway_identity")) != [(canonical_json(identity),)]:
        raise GatewayStorageError("identity_changed")


def _credentials(db: sqlite3.Connection, aes: AESGCM) -> int:
    rows = db.execute("SELECT reference,ciphertext FROM credentials LIMIT 10001").fetchall()
    if len(rows) > 10000:
        raise GatewayStorageError("limit")
    try:
        for reference, value in rows:
            if not re.fullmatch(r"cred-[a-f0-9]{32}", reference) or not isinstance(value, bytes):
                raise ValueError()
            secret = aes.decrypt(value[:12], value[12:], reference.encode())
            if not 1 <= len(secret) <= 4096 or any(c < 33 or c > 126 for c in secret):
                raise ValueError()
        if db.execute("SELECT 1 FROM grants WHERE reference IS NOT NULL AND reference NOT IN (SELECT reference FROM credentials) LIMIT 1").fetchone():
            raise ValueError()
    except (ValueError, TypeError, InvalidTag):
        raise GatewayStorageError("key_mismatch") from None
    return len(rows)


@contextmanager
def _database(path: Path, mode: str = "ro") -> Iterator[sqlite3.Connection]:
    before = _metadata(path)
    if path.stat().st_size > 32_000_000:
        raise GatewayStorageError("limit")
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, timeout=5)
        try:
            if mode == "ro":
                db.execute("PRAGMA query_only=ON")
            if _metadata(path) != before:
                raise GatewayStorageError("identity_changed")
            with db:
                yield db
            if _metadata(path) != before:
                raise GatewayStorageError("identity_changed")
        finally:
            db.close()
    except sqlite3.Error:
        raise GatewayStorageError("database_invalid") from None


class PersistentGatewaySecretStore(GatewaySecretStore):
    """Open existing only; a lifetime lease excludes every maintenance writer."""
    def __init__(self, root: str | Path, expected_store_id: str, *, environment_key: str | None = None) -> None:
        self._lease: int | None = None
        self.repository = persistent_root(root)
        _metadata(self.repository, directory=True)
        if not isinstance(expected_store_id, str) or not _ID.fullmatch(expected_store_id):
            raise GatewayStorageError("identity_invalid")
        self.store_id = expected_store_id
        self.environment_key = environment_key
        try:
            self._lease = _lease(self.repository, exclusive=False)
            pointer = _pointer(self.repository)
            if pointer["store_id"] != expected_store_id:
                raise GatewayStorageError("identity_changed")
            self.generation = pointer["generation"]
            self.root = self.repository / "generations" / self.generation
            _metadata(self.repository / "generations", directory=True)
            _metadata(self.root, directory=True)
            self.path = self.root / "credentials.sqlite3"
            master, self.private_key, self.identity = _keys(self.root)
            if self.identity["store_id"] != expected_store_id:
                raise GatewayStorageError("identity_changed")
            self.aes = AESGCM(master)
            self.key_id = hashlib.sha256(_read(self.root / "envelope.pem")).hexdigest()[:24]
            self._pins = self._fingerprints()
            self.storage_readiness()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._lease is not None:
            os.close(self._lease)
            self._lease = None

    def __enter__(self) -> PersistentGatewaySecretStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _fingerprints(self) -> dict:
        names = [self.repository, self.repository / "generations", self.root]
        result = {str(p): _metadata(p, directory=True) for p in names}
        for name in FILES:
            p = self.root / name
            result[str(p)] = (_metadata(p), None if name == "credentials.sqlite3" else hashlib.sha256(_read(p)).hexdigest())
        result["pointer"] = hashlib.sha256(_read(self.repository / "current.json")).hexdigest()
        result["lease"] = _metadata(self.repository / "service.lock")
        return result

    def _guard(self, *, writing: bool = False) -> None:
        persistent_root(self.repository)
        if self._lease is None or self._fingerprints() != self._pins:
            raise GatewayStorageError("identity_changed")
        for suffix in ("-wal", "-shm", "-journal"):
            p = Path(str(self.path) + suffix)
            if (p.exists() or p.is_symlink()) and not (writing and suffix == "-journal"):
                raise GatewayStorageError("recovery_required")

    @contextmanager
    def _open(self, mode: str) -> Iterator[sqlite3.Connection]:
        self._guard()
        with _database(self.path, mode) as db:
            _schema(db, self.identity)
            self._guard()
            yield db
            self._guard(writing=mode == "rw")
        self._guard()

    def connect(self) -> AbstractContextManager[sqlite3.Connection]:
        # Only the existing authenticated grant/save methods use this writer.
        return self._open("rw")

    def read(self, reference: str) -> str:
        with self._open("ro") as db:
            if reference == "env-nvidia":
                if not self.environment_key:
                    raise GatewayStorageError("reference_missing")
                return self.environment_key
            row = db.execute("SELECT ciphertext FROM credentials WHERE reference=?", (reference,)).fetchone()
            if not row:
                raise GatewayStorageError("reference_missing")
            try:
                value = row[0]
                return self.aes.decrypt(value[:12], value[12:], reference.encode()).decode()
            except (ValueError, TypeError, InvalidTag, UnicodeError):
                raise GatewayStorageError("key_mismatch") from None

    def has(self, reference: str) -> bool:
        try:
            self.read(reference)
            return True
        except GatewayStorageError as exc:
            if exc.code == "gateway_storage_reference_missing":
                return False
            raise

    def storage_readiness(self) -> dict:
        with self._open("ro") as db:
            _credentials(db, self.aes)
        return {"version": VERSION, "ready": True, "store_id": self.store_id, "generation": self.generation}

    def credential_readiness(self, reference: str) -> dict:
        result = self.storage_readiness()
        self.read(reference)  # Only a safe boolean leaves this boundary.
        return {**result, "credential_ready": True, "reference_hash": hashlib.sha256(reference.encode()).hexdigest()}


def _pointer(root: Path) -> dict:
    value = _json(root / "current.json")
    if any(not isinstance(value.get(n), str) or not _ID.fullmatch(value[n]) for n in ("generation", "store_id")):
        raise GatewayStorageError("identity_invalid")
    return value


def _lease(root: Path, *, exclusive: bool) -> int:
    before = _metadata(root / "service.lock")
    try:
        fd = os.open(root / "service.lock", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            s = os.fstat(fd)
            if (s.st_dev, s.st_ino) != before or _metadata(root / "service.lock") != before:
                raise GatewayStorageError("identity_changed")
            return fd
        except BaseException:
            os.close(fd)
            raise
    except OSError:
        raise GatewayStorageError("busy") from None


def _checkpoint(name: str) -> None:
    """Crash-injection seam; no environment variable enables it in production."""


def _activate(root: Path, generation: str, store_id: str, *, previous: str | None) -> dict:
    value = {"generation": generation, "store_id": store_id, "previous": previous,
             "activation_database_hash": hashlib.sha256(_read(root / "generations" / generation / "credentials.sqlite3", 32_000_000)).hexdigest()}
    tmp = root / (".pointer-" + uuid4().hex)
    _write(tmp, canonical_json(value).encode())
    _checkpoint("before_pointer")
    os.replace(tmp, root / "current.json")
    _fsync(root)
    _checkpoint("after_pointer")
    return value


def _stamp_identity(path: Path, master: bytes, key: rsa.RSAPrivateKey, store_id: str) -> None:
    identity = _identity(master, key, store_id)
    with _database(path / "credentials.sqlite3", "rw") as db:
        db.execute("CREATE TABLE gateway_identity (identity TEXT NOT NULL PRIMARY KEY)")
        db.execute("INSERT INTO gateway_identity VALUES (?)", (canonical_json(identity),))
        db.execute("PRAGMA user_version=1")
    _write(path / "identity.json", canonical_json(identity).encode())


def _validate_generation(path: Path, store_id: str) -> None:
    _metadata(path, directory=True)
    master, _, identity = _keys(path)
    if identity["store_id"] != store_id:
        raise GatewayStorageError("identity_changed")
    if any((path / ("credentials.sqlite3" + s)).exists() for s in ("-journal", "-wal", "-shm")):
        raise GatewayStorageError("recovery_required")
    with _database(path / "credentials.sqlite3") as db:
        _schema(db, identity)
        _credentials(db, AESGCM(master))


class GatewayStorageMaintenance:
    """Explicit offline operations. Never invoked by serving/readiness paths."""
    @staticmethod
    def initialize(root: str | Path, *, intent: str) -> dict:
        if intent not in {"new-install", "reconfigure-new-store"}:
            raise GatewayStorageError("explicit_intent_required")
        root = persistent_root(root)
        if root.exists() and list(root.iterdir()):
            raise GatewayStorageError("existing_material_preserved")
        root.mkdir(mode=0o700, parents=False, exist_ok=True)
        _metadata(root, directory=True)
        _write(root / "service.lock", b"")
        (root / "generations").mkdir(mode=0o700)
        generation, store_id = uuid4().hex, uuid4().hex
        path = root / "generations" / generation
        legacy = GatewaySecretStore(str(path))  # Explicit NEW empty generation only.
        master = _read(path / "master.key", 32)
        _stamp_identity(path, master, legacy.private_key, store_id)
        _validate_generation(path, store_id)
        for name in FILES:
            _fsync(path / name)
        _fsync(path)
        _fsync(root / "generations")
        return _activate(root, generation, store_id, previous=None)

    @staticmethod
    def import_legacy(source: str | Path, root: str | Path, *, material_hash: str, envelope_key_id: str) -> dict:
        """Explicit forward migration on a COPY; no source writes or key rotation.

        The caller must independently attest the exact trio and historical RSA
        identity. Legacy ciphertext authenticates the AES key, not the RSA key.
        Missing/empty originals cannot be adopted as a recovered credential.
        """
        source, root = persistent_root(source), persistent_root(root)
        _metadata(source, directory=True)
        if any((source / ("credentials.sqlite3" + s)).exists() for s in ("-wal", "-shm", "-journal")):
            raise GatewayStorageError("recovery_required")
        raw = {n: _read(source / n, 32_000_000) for n in FILES[:3]}
        if content_hash({n: hashlib.sha256(v).hexdigest() for n, v in raw.items()}) != material_hash:
            raise GatewayStorageError("backup_invalid")
        try:
            key = serialization.load_pem_private_key(raw["envelope.pem"], password=None)
            if (not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 3072
                    or hashlib.sha256(raw["envelope.pem"]).hexdigest()[:24] != envelope_key_id):
                raise ValueError()
            aes = AESGCM(raw["master.key"])
            with _database(source / "credentials.sqlite3") as db:
                _schema(db, None)
                if not _credentials(db, aes):
                    raise GatewayStorageError("legacy_pairing_unproven")
        except (ValueError, TypeError):
            raise GatewayStorageError("key_mismatch") from None
        if root.exists():
            raise GatewayStorageError("existing_material_preserved")
        root.mkdir(mode=0o700)
        _write(root / "service.lock", b"")
        (root / "generations").mkdir(mode=0o700)
        generation, store_id = uuid4().hex, uuid4().hex
        target = root / "generations" / generation
        target.mkdir(mode=0o700)
        for name, value in raw.items():
            _write(target / name, value)
        _stamp_identity(target, raw["master.key"], key, store_id)
        _validate_generation(target, store_id)
        for name in FILES:
            _fsync(target / name)
        _fsync(target)
        _fsync(target.parent)
        return _activate(root, generation, store_id, previous=None)

    @staticmethod
    @contextmanager
    def _offline(root: str | Path, store_id: str, expected_generation: str) -> Iterator[tuple[Path, Path]]:
        root = persistent_root(root)
        _metadata(root, directory=True)
        fd = _lease(root, exclusive=True)
        try:
            pointer = _pointer(root)
            if pointer["store_id"] != store_id or pointer["generation"] != expected_generation:
                raise GatewayStorageError("identity_changed")
            _metadata(root / "generations", directory=True)
            yield root, root / "generations" / expected_generation
        finally:
            os.close(fd)

    @classmethod
    def backup(cls, root: str | Path, destination: str | Path, *, store_id: str, expected_generation: str) -> str:
        with cls._offline(root, store_id, expected_generation) as (_, source):
            _validate_generation(source, store_id)
            destination = persistent_root(destination)
            destination.mkdir(mode=0o700)  # Refuse overwriting any existing candidate.
            for name in FILES:
                if name != "credentials.sqlite3":
                    _write(destination / name, _read(source / name))
            _write(destination / "credentials.sqlite3", b"")
            with _database(source / "credentials.sqlite3") as db, _database(destination / "credentials.sqlite3", "rw") as target:
                db.backup(target)
            _validate_generation(destination, store_id)
            record = {"version": VERSION, "store_id": store_id,
                      "files": {name: hashlib.sha256(_read(destination / name, 32_000_000)).hexdigest() for name in FILES}}
            master = _read(destination / "master.key", 32)
            record["mac"] = hmac.new(master, canonical_json(record).encode(), hashlib.sha256).hexdigest()
            _write(destination / "snapshot.json", canonical_json(record).encode())
            for name in (*FILES, "snapshot.json"):
                _fsync(destination / name)
            _fsync(destination)
            _fsync(destination.parent)
            return content_hash(record)

    @classmethod
    def restore(cls, root: str | Path, backup: str | Path, *, store_id: str, expected_generation: str, snapshot_hash: str) -> dict:
        with cls._offline(root, store_id, expected_generation) as (root, _):
            backup = persistent_root(backup)
            _validate_generation(backup, store_id)
            record = _json(backup / "snapshot.json")
            if content_hash(record) != snapshot_hash or record.get("store_id") != store_id or not isinstance(record.get("mac"), str):
                raise GatewayStorageError("backup_invalid")
            mac = record.pop("mac")
            raw = {n: _read(backup / n, 32_000_000) for n in FILES}
            if (not hmac.compare_digest(mac, hmac.new(raw["master.key"], canonical_json(record).encode(), hashlib.sha256).hexdigest())
                    or record.get("files") != {n: hashlib.sha256(v).hexdigest() for n, v in raw.items()}):
                raise GatewayStorageError("backup_invalid")
            generation = uuid4().hex
            target = root / "generations" / generation
            target.mkdir(mode=0o700)
            for name in FILES:
                _write(target / name, raw[name])
            _validate_generation(target, store_id)
            _fsync(target)
            _fsync(target.parent)
            _checkpoint("generation_staged")
            return _activate(root, generation, store_id, previous=expected_generation)

    @classmethod
    def rollback(cls, root: str | Path, *, store_id: str, expected_generation: str) -> dict:
        with cls._offline(root, store_id, expected_generation) as (root, current):
            pointer = _pointer(root)
            previous = pointer.get("previous")
            if not isinstance(previous, str) or not _ID.fullmatch(previous):
                raise GatewayStorageError("rollback_unavailable")
            _validate_generation(current, store_id)
            if hashlib.sha256(_read(current / "credentials.sqlite3", 32_000_000)).hexdigest() != pointer["activation_database_hash"]:
                raise GatewayStorageError("rollback_unsafe")
            _validate_generation(root / "generations" / previous, store_id)
            return _activate(root, previous, store_id, previous=None)

    @classmethod
    def recover(cls, root: str | Path, *, store_id: str, expected_generation: str) -> dict:
        """Recover a hot DELETE journal on a COPY, never repair the only original."""
        with cls._offline(root, store_id, expected_generation) as (root, source):
            _, _, identity = _keys(source)
            if identity["store_id"] != store_id:
                raise GatewayStorageError("identity_changed")
            generation = uuid4().hex
            target = root / "generations" / generation
            target.mkdir(mode=0o700)
            for name in FILES:
                _write(target / name, _read(source / name, 32_000_000))
            journal = source / "credentials.sqlite3-journal"
            if journal.exists():
                _write(target / journal.name, _read(journal, 32_000_000))
            if any((source / ("credentials.sqlite3" + s)).exists() for s in ("-wal", "-shm")):
                raise GatewayStorageError("journal_unsupported")
            with _database(target / "credentials.sqlite3", "rw") as db:
                _schema(db, identity)  # SQLite rolls back the hot journal on this private copy.
            _validate_generation(target, store_id)
            for name in FILES:
                _fsync(target / name)
            _fsync(target)
            _fsync(target.parent)
            return _activate(root, generation, store_id, previous=expected_generation)
