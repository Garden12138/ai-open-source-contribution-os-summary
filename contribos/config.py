"""Local runtime paths and server defaults."""

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


def default_home() -> Path:
    return Path(os.environ.get("CONTRIBOS_HOME", "~/.contribos")).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    home: Path = field(default_factory=default_home)
    host: str = "127.0.0.1"
    port: int = 8765
    tool_paths: dict[str, str] = field(default_factory=dict, compare=False)
    login_path_resolved: bool | None = None

    def tool(self, name: str) -> str | None:
        return shutil.which(self.tool_paths.get(name) or name)

    def tool_environment(self, environment: dict[str, str]) -> dict[str, str]:
        node = self.tool_paths.get('node')
        if node and Path(node).name == 'node':
            return {**environment, 'PATH': os.pathsep.join((str(Path(node).parent), environment.get('PATH', os.defpath)))}
        return environment

    @property
    def database_path(self) -> Path:
        return self.home / "contribos.db"

    @property
    def local_token_path(self) -> Path:
        return self.home / "local-token"


def validate_bind_host(host: str) -> str:
    """Bind every local server to the same IPv4 loopback address."""
    if host.lower() in {"localhost", "127.0.0.1"}:
        return "127.0.0.1"
    raise ValueError("Server host must be 127.0.0.1.")


def server_origin(settings: Settings) -> str:
    host = validate_bind_host(settings.host)
    if not 1 <= settings.port <= 65535:
        raise ValueError("Server port must be between 1 and 65535")
    return f"http://{host}:{settings.port}"
