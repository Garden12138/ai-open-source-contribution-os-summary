"""Bounded archive inspection in the same credential-free OCI boundary."""

from __future__ import annotations
import json
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


class DockerPlanningContextRuntime(DockerCodingContextRuntime):
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
        for entry in value["entries"]:
            CodingContextEntry(**entry)
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
        files[relative] = member
    inventory = sorted(files)
    def rank(path):
        lower = path.lower()
        name = PurePosixPath(lower).name
        docs = name.startswith(("readme", "contributing", "agents.md")) or name in {
            "pyproject.toml", "package.json", "pytest.ini", "tsconfig.json", "setup.cfg"}
        hits = sum(term in lower for term in terms)
        return (-int(docs), -hits, len(PurePosixPath(path).parts), path)
    paths = request.get("paths") or sorted(files, key=rank)[:48]
    entries = []
    size = 0
    for path in sorted(set(paths)):
        member = files.get(path)
        if member is None:
            entries.append(dict(path=path, prior_hash=None, content=None, executable=None))
            continue
        if member.size > 256000:
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
