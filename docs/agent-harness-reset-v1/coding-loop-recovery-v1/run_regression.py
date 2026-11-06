"""One deterministic test shard; file groups cover the default suite exactly."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from app.provenance import content_hash

ROOT = Path(__file__).resolve().parent


def main():
    shard = int(sys.argv[1])
    assert shard in (0, 1)
    manifest = json.loads((ROOT / "source-manifest.json").read_text())
    files = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in Path("app").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    assert files == manifest["files"]
    collected = subprocess.check_output([sys.executable, "-m", "pytest", "--collect-only", "-q"], text=True)
    cases = [(m[1], int(m[2])) for line in collected.splitlines()
        if (m := re.fullmatch(r"(tests/.*\.py): (\d+)", line))]
    assert cases
    groups, counts = [[], []], [0, 0]
    for path, count in sorted(cases, key=lambda row: (-row[1], row[0])):
        index = min(range(2), key=lambda i: counts[i])
        groups[index].append(path)
        counts[index] += count
    assert set(groups[0]).isdisjoint(groups[1])
    assert set(groups[0] + groups[1]) == {p for p, _ in cases}
    value = {"source_hash": content_hash(files), "shard": shard, "files": groups[shard],
        "collected_tests": counts[shard], "all_default_tests": sum(counts), "real_models": False}
    (ROOT / ("regression-shard-" + str(shard) + ".json")).write_text(json.dumps(value, indent=2) + "\n")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", *groups[shard],
        "--junitxml=/tmp/coding-loop-recovery-final-full-" + str(shard) + ".xml", "--disable-warnings", "--maxfail=5"])
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest for p, digest in files.items())
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
