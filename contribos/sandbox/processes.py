"""Inspect a host agent's process group and descendants on macOS and Linux."""

import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    parent: int
    group: int
    started: int
    zombie: bool


class _BsdInfo(ctypes.Structure):
    _fields_ = [
        ('flags', ctypes.c_uint32), ('status', ctypes.c_uint32), ('xstatus', ctypes.c_uint32),
        ('pid', ctypes.c_uint32), ('ppid', ctypes.c_uint32), ('uids_and_gids', ctypes.c_uint32 * 7),
        ('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32), ('nfiles', ctypes.c_uint32),
        ('pgid', ctypes.c_uint32), ('pjobc', ctypes.c_uint32), ('tdev', ctypes.c_uint32),
        ('tpgid', ctypes.c_uint32), ('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64),
        ('start_usec', ctypes.c_uint64),
    ]


if sys.platform == 'darwin':
    _libproc = ctypes.CDLL('/usr/lib/libproc.dylib')
    _libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    _libproc.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    _libproc.proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]


def process_info(pid: int) -> ProcessInfo | None:
    if sys.platform == 'darwin':
        info = _BsdInfo()
        if _libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
            return None
        return ProcessInfo(pid, info.ppid, info.pgid, info.start_sec * 1_000_000 + info.start_usec, info.status == 5)
    try:
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return ProcessInfo(pid, int(fields[1]), int(fields[2]), int(fields[19]), fields[0] in {'Z', 'X'})
    except (OSError, IndexError, ValueError):
        return None


def _mac_pids(call: Callable[..., int], *args: int, counts_bytes: bool = False) -> list[int]:
    capacity = 64
    while True:
        buffer = (ctypes.c_int * capacity)()
        size = ctypes.sizeof(buffer)
        count = call(*args, buffer, size)
        if counts_bytes:
            count //= ctypes.sizeof(ctypes.c_int)
        if count < capacity:
            return [pid for pid in buffer[:max(0, count)] if pid]
        capacity *= 2


def process_family(group: int) -> dict[int, ProcessInfo]:
    if sys.platform == 'darwin':
        pids = _mac_pids(_libproc.proc_listpids, 2, group, counts_bytes=True)
        seen = set(pids)
        for pid in pids:
            children = set(_mac_pids(_libproc.proc_listchildpids, pid)) - seen
            pids.extend(children)
            seen.update(children)
        return {pid: info for pid in pids if (info := process_info(pid)) and not info.zombie}
    processes = [info for path in Path('/proc').iterdir() if path.name.isdecimal()
                 and (info := process_info(int(path.name))) and not info.zombie]
    family = {info.pid: info for info in processes if info.group == group}
    children: dict[int, list[ProcessInfo]] = {}
    for info in processes:
        children.setdefault(info.parent, []).append(info)
    pids = list(family)
    for pid in pids:
        for info in children.get(pid, ()):
            if info.pid not in family:
                family[info.pid] = info
                pids.append(info.pid)
    return family
