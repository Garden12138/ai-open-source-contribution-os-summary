"""Bounded archive inspection in the same credential-free OCI boundary."""

from __future__ import annotations
import json
import hashlib
import tempfile
from pathlib import Path
from uuid import uuid4
from app.provenance import canonical_json
from app.security import contains_sensitive_text
from app.sandbox_worker.coding_context import (
    DockerCodingContextRuntime,
    CodingContextEntry,
    CodingContextError,
    _CAPTURE_SCRIPT,
    _safe_path,
    MAX_CONTEXT_BYTES,
)

# Planning reads data only. An explicitly selected file can use the existing
# 700 KB aggregate read budget; implementation's separate file cap is unchanged.
MAX_PLANNING_READ_BYTES = 700_000
MAX_AUTOMATIC_PLANNING_FILE_BYTES = 256_000


class DockerPlanningContextRuntime(DockerCodingContextRuntime):
    async def inspect_import_metadata(self, *, archive, image_digest: str, policy, paths: tuple[str, ...]) -> dict:
        from app.sandbox_worker.import_metadata import inspect_import_metadata
        return await inspect_import_metadata(self, archive=archive, image_digest=image_digest, policy=policy, paths=paths)

    async def inspect(
        self,
        *,
        archive,
        image_digest: str,
        policy,
        query: str,
        paths: tuple[str, ...] = (),
    ) -> dict:
        if len(paths) > 64 or len(query) > 12000:
            raise CodingContextError("Planning inspection exceeds limits")
        for path in paths:
            _safe_path(path)
        archive.verify()
        with tempfile.TemporaryDirectory(prefix="contribos-planning-") as directory:
            runner_archive = self._stage_archive(archive, Path(directory))
            request_path = Path(directory) / "request.json"
            request_path.write_text(
                canonical_json({"query": query, "paths": list(paths)})
            )
            request_path.chmod(0o444)
            name = "contribos-planning-" + uuid4().hex
            argv = self._argv(
                archive=runner_archive,
                request_path=request_path,
                image_digest=image_digest,
                policy=policy,
                container_name=name,
            )
            argv = tuple(
                INSPECT_SCRIPT if part == _CAPTURE_SCRIPT else part for part in argv
            )
            raw = await self._run(
                argv, container_name=name, timeout_seconds=policy.timeout_seconds
            )
        archive.verify()
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {
            "inventory",
            "entries",
            "truncated",
        }:
            raise CodingContextError("Invalid planning context")
        if (
            len(raw) > MAX_CONTEXT_BYTES
            or len(value["inventory"]) > 10000
            or len(value["entries"]) > 64
        ):
            raise CodingContextError("Planning context exceeds limits")
        for path in value["inventory"]:
            _safe_path(path)
        total_bytes = 0
        for entry in value["entries"]:
            if set(entry) != {"path", "prior_hash", "content", "executable"}:
                raise CodingContextError("Invalid planning entry")
            content = entry["content"]
            data = content.encode("utf-8") if isinstance(content, str) else b""
            total_bytes += len(data)
            if len(data) > MAX_AUTOMATIC_PLANNING_FILE_BYTES:
                _safe_path(entry["path"])
                if (entry["path"] not in paths or len(data) > MAX_PLANNING_READ_BYTES
                        or entry["prior_hash"] != hashlib.sha256(data).hexdigest()
                        or not isinstance(entry["executable"], bool)):
                    raise CodingContextError("Invalid explicit planning file")
            else:
                CodingContextEntry(**entry)
            if total_bytes > MAX_PLANNING_READ_BYTES:
                raise CodingContextError("Planning read exceeds aggregate budget")
            if entry["content"] is not None and contains_sensitive_text(entry["content"]):
                # Keep the path in the inventory: this is an existing but
                # unavailable file, never evidence for a new/replaced file.
                # Do not rewrite text while claiming its original content hash.
                entry.update(content=None, prior_hash=None, executable=None)
                value["truncated"] = True
        return value


INSPECT_SCRIPT = r"""
import hashlib, json, re, sys, tarfile
from pathlib import PurePosixPath
request = json.load(open("/input/request.json", encoding="utf-8"))
root = sys.argv[1] + "-" + sys.argv[2]
terms = set(re.findall(r"[a-zA-Z_][a-zA-Z_0-9]{2,}", request["query"].lower()))
with tarfile.open("/input/repository.tar", "r:*") as bundle:
    files = {}
    count = 0
    for member in bundle:
        count += 1
        if count > 20000:
            raise SystemExit(10)
        parts = PurePosixPath(member.name).parts
        if not parts or parts[0] != root or ".." in parts or ".git" in parts:
            raise SystemExit(11)
        if member.isdir():
            continue
        relative = str(PurePosixPath(*parts[1:]))
        if relative in files or len(relative) > 500:
            raise SystemExit(12)
        if not member.isfile() or member.issym() or member.islnk():
            continue
        ext = PurePosixPath(relative).suffix.lower()
        if ext in {
            ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
            ".mp3", ".mp4", ".wav", ".ogg",
            ".woff", ".woff2", ".ttf", ".eot", ".otf",
            ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z",
            ".pdf", ".exe", ".dll", ".so", ".dylib", ".bin",
            ".pyc", ".pyo", ".pyd", ".class",
        } and (not request.get("paths") or relative not in request["paths"]):
            continue
        files[relative] = member
    inventory = sorted(files)
    def rank(path):
        lower = path.lower()
        name = PurePosixPath(lower).name
        localized = any(part in {"i18n", "locales", "translations"} for part in PurePosixPath(lower).parts)
        docs = (name.startswith(("readme", "contributing", "agents.md")) and not localized) or name in {
            "pyproject.toml", "package.json", "pytest.ini", "tsconfig.json", "setup.cfg"}
        hits = sum(term in lower for term in terms)
        return (int(localized), -hits, -int(docs), len(PurePosixPath(path).parts), path)
    explicit = bool(request.get("paths"))
    # Learn project conventions before filling the rest with Issue-related code.
    foundations = sorted(p for p in files if "/" not in p and (
        PurePosixPath(p).name.lower().startswith(("readme", "contributing", "agents.md"))
        or p in {"pyproject.toml", "package.json", "setup.cfg", "setup.py", "pytest.ini", "tox.ini"}
    ))[:12]
    paths = request.get("paths") or list(dict.fromkeys(foundations + sorted(files, key=rank)))[:48]
    entries = []
    size = 0
    for path in dict.fromkeys(paths):
        member = files.get(path)
        if member is None:
            entries.append(dict(path=path, prior_hash=None, content=None, executable=None))
            continue
        if member.size > (700000 if explicit else 256000):
            if explicit:
                entries.append(dict(path=path, prior_hash=None, content=None, executable=None))
            continue
        data = bundle.extractfile(member).read(member.size + 1)
        if len(data) != member.size:
            raise SystemExit(13)
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if "\x00" in content or size + len(data) > 700000:
            continue
        entries.append(dict(path=path, prior_hash=hashlib.sha256(data).hexdigest(),
            content=content, executable=bool(member.mode & 0o111)))
        size += len(data)
    result = dict(inventory=inventory[:10000], entries=entries,
        truncated=len(inventory) > 10000 or len(entries) < len(paths))
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > 1500000:
        raise SystemExit(14)
    print(encoded)
""".strip()
