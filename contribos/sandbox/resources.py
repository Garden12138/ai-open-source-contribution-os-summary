"""Resource limits and per-sandbox accounting from the host's sbx daemon."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contribos.sandbox.base import SandboxError


class ResourceLimits(BaseModel):
    model_config = ConfigDict(extra='forbid')

    cpus: int = Field(default=2, strict=True, ge=1, le=1024)
    memory_mib: int = Field(default=4096, strict=True, ge=512, le=2**31 - 1)
    disk_mib: int = Field(default=20480, strict=True, ge=1, le=2**31 - 1)


DISK_ERRORS = {
    'sandbox_disk_limit': 'Sandbox disk limit exceeded. Free space or raise the disk limit, then continue.',
    'sandbox_disk_unavailable': 'Sandbox disk usage is unavailable. Check Docker Sandboxes, then continue.',
}


def resource_limits(saved: dict[str, int]) -> ResourceLimits:
    try:
        return ResourceLimits.model_validate(saved)
    except ValueError:
        raise SandboxError('sandbox_resource_config_invalid') from None


def writable_bytes(report: dict[str, Any], name: str) -> int:
    """Count the task's writable layer and its dedicated kit volume, including stopped VMs."""
    containers, volumes = report.get('containers'), report.get('volumes')
    if isinstance(containers, list) and isinstance(volumes, list):
        matches = [c for c in containers if isinstance(c, dict) and c.get('name') == name]
        if len(matches) == 1:
            container = matches[0]
            sizes = [v.get('size') for v in volumes if isinstance(v, dict) and v.get('name') == name + '-docker']
            count = container.get('local_volumes')
            if type(count) is int and count == len(sizes):
                sizes.append(container.get('size'))
                if all(type(size) is int and size >= 0 for size in sizes):
                    return sum(sizes)
    raise SandboxError('sandbox_disk_unavailable')
