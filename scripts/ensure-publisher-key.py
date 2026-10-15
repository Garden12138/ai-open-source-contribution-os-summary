"""One-time local management key; never prints or changes GitHub credentials."""
from pathlib import Path
import os
import re
import secrets
import tempfile


def ensure_key(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Refusing a symlink .env")
    raw = path.read_text() if path.exists() else ""
    match = re.search(r"^PUBLISHER_MANAGEMENT_KEY=(.*)$", raw, re.MULTILINE)
    if match and match[1].strip().strip("\"'"):
        if not re.fullmatch(r"(?:[a-fA-F0-9]{2}){32,}", match[1].strip().strip("\"'")):
            raise ValueError("PUBLISHER_MANAGEMENT_KEY must be 64+ hex characters")
        return
    value = "PUBLISHER_MANAGEMENT_KEY=" + secrets.token_hex(32)
    if match:
        raw = raw[:match.start()] + value + raw[match.end():]
    else:
        raw += ("\n" if raw and not raw.endswith("\n") else "") + value + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".publisher-env-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    ensure_key(Path(__file__).resolve().parents[1] / ".env")
