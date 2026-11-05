"""Build the exact frozen Harness source on the two existing trusted images."""
from __future__ import annotations

import hashlib
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("docs/agent-harness-reset-v1/source-manifest.json"))
    parser.add_argument("--output", type=Path, default=Path("docs/agent-harness-reset-v1/build-report.json"))
    args = parser.parse_args()
    out = args.output
    if out.exists():
        raise ValueError("Build report already exists")
    manifest = json.loads(args.manifest.read_text())
    assert content_hash(manifest["files"]) == manifest["source_hash"]
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in manifest["files"].items())
    bases = {
        "app": ("contribos-publisher:upeve-native-rejection-candidate", "sha256:dc077908630ea565e1125709ff165a8a2001d37e7a26e02934ca4b86c9524313"),
        "worker": ("contribos-sandbox-worker:upeve-native-rejection-candidate", "sha256:8c90879c3a3724bf35f570fcf5858db9bae957e22adf19c30820d2d0f65558e2"),
    }
    root = Path(tempfile.mkdtemp(prefix="harness-build-"))
    shutil.copytree("app", root / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(name, root / name)
    images = {}
    for role, (base, expected) in bases.items():
        image = json.loads(subprocess.check_output(["docker", "image", "inspect", base]))[0]
        assert image["Id"] == expected
        dockerfile = root / (role + ".Dockerfile")
        dockerfile.write_text(f"FROM {base}\nUSER 0:0\nWORKDIR /app\nCOPY app ./app\n"
            "COPY pyproject.toml README.md ./\n"
            "RUN python -m app.runtime_integrity && python -m pip install --no-index --no-deps --no-build-isolation .\n"
            "USER 10001:10001\n")
        tag = "contribos-" + ("publisher" if role == "app" else "sandbox-worker") + ":harness-" + manifest["source_hash"][:12]
        subprocess.run(["docker", "build", "--pull=false", "--network=none", "-f", str(dockerfile), "-t", tag, str(root)], check=True)
        images[role] = json.loads(subprocess.check_output(["docker", "image", "inspect", tag]))[0]["Id"]
    probe = ("from pathlib import Path;import hashlib,json;print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() "
        "for p in Path('app').rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}))")
    for image in images.values():
        actual = json.loads(subprocess.check_output(["docker", "run", "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--entrypoint", "python", image, "-c", probe]))
        assert actual == manifest["files"]
        subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--entrypoint", "python", image, "-I", "-m", "app.runtime_integrity"], check=True)
    value = {"version": "harness-build-v1", "source_hash": manifest["source_hash"], "source_manifest_file": str(args.manifest), "files_verified": len(manifest["files"]),
        "images": images, "base_images": {k: v[1] for k, v in bases.items()}, "build_network": "none",
        "dependencies_changed": False, "model_calls": 0, "third_party_writes": 0}
    ensure_no_sensitive_data(value, context="Harness build report")
    with out.open("x") as target:
        target.write(json.dumps(value, indent=2) + "\n")
    print(json.dumps(value))


if __name__ == "__main__":
    main()
