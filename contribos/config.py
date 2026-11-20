"""Local runtime paths and server defaults."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def default_home() -> Path:
    return Path(os.environ.get("CONTRIBOS_HOME", "~/.contribos")).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    home: Path = field(default_factory=default_home)
    host: str = "127.0.0.1"
    port: int = 8765

    @property
    def database_path(self) -> Path:
        return self.home / "contribos.db"

    @property
    def local_token_path(self) -> Path:
        return self.home / "local-token"


def validate_bind_host(host: str) -> str:
    """Resolve localhost deterministically and reject every non-loopback address."""
    from ipaddress import ip_address

    if host.lower() == "localhost":
        return "127.0.0.1"
    try:
        address = ip_address(host)
    except ValueError:
        raise ValueError("Server host must be a loopback IP address") from None
    if not address.is_loopback or "%" in host:
        raise ValueError("Server host must be a loopback IP address")
    return str(address)


def server_origin(settings: Settings) -> str:
    host = validate_bind_host(settings.host)
    if not 1 <= settings.port <= 65535:
        raise ValueError("Server port must be between 1 and 65535")
    authority = f"[{host}]" if ":" in host else host
    return f"http://{authority}:{settings.port}"
