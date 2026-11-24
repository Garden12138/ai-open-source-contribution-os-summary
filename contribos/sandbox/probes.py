"""Active checks against an empty task VM. Guest diagnostics never leave it."""

import asyncio
import fcntl
import ipaddress
import socket
import struct
import sys
import uuid

from contribos.agent.engine import TaskContext
from contribos.sandbox.base import SandboxError
from contribos.sandbox.sbx import ROOT, SbxSandbox

CHECKS = {
    'sandbox_policy': 'VM identity, mounts, credential sources and network policy',
    'sandbox_loopback': 'Host loopback HTTP canary',
    'sandbox_lan': 'Host LAN HTTP canary',
    'sandbox_registries': 'PyPI and npm over HTTPS',
    'sandbox_credentials': 'Agent environment and common credential files',
}


def _lan_address() -> str:
    # The default route can belong to a VPN. Prefer an address assigned to a LAN interface.
    ranges = tuple(map(ipaddress.ip_network, ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _, name in socket.if_nameindex():
            if not name.startswith(('en', 'eth', 'wl')):
                continue
            request = struct.pack('256s', name.encode()[:15])
            try:
                info = fcntl.ioctl(sock.fileno(), 0xc0206921 if sys.platform == 'darwin' else 0x8915, request)
            except OSError:
                continue
            address = ipaddress.IPv4Address(info[20:24])
            if any(address in net for net in ranges):
                return str(address)
    raise OSError('No private LAN interface.')


async def _reachable(vm: SbxSandbox, url: str, *, direct: bool = False) -> int:
    args = ['curl', '-q', '-fsS', '--max-time', '5', '-o', '/dev/null']
    if direct:
        args.extend(['--noproxy', '*'])
    result = await vm.exec([*args, '--', url], cwd=str(ROOT), timeout=8)
    return result.returncode


async def _canary(vm: SbxSandbox, address: str, *, host_alias: bool) -> bool:
    hits = 0
    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal hits
        hits += 1
        try:
            async with asyncio.timeout(2):
                await reader.readuntil(b'\r\n\r\n')
                writer.write(b'HTTP/1.0 204 No Content\r\nConnection: close\r\n\r\n')
                await writer.drain()
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()

    async with await asyncio.start_server(respond, address, 0) as server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.wait_for(asyncio.open_connection(address, port), 2)
        try:
            writer.write(b'GET / HTTP/1.0\r\n\r\n')
            await writer.drain()
            if not (await asyncio.wait_for(reader.readline(), 2)).startswith(b'HTTP/1.0 204 '):
                return False
        finally:
            writer.close()
            await writer.wait_closed()
        for host in ((address, 'host.docker.internal') if host_alias else (address,)):
            for direct in (False, True):
                code = await _reachable(vm, f'http://{host}:{port}/', direct=direct)
                # DNS failure, connection refused, proxy denial/reset, or network timeout.
                if code not in (6, 7, 22, 28, 52, 56) or hits != 1:
                    return False
        return hits == 1


async def probe_vm(vm: SbxSandbox) -> dict[str, str]:
    results = {'sandbox_policy': 'ok'}
    for name, loopback in [('sandbox_loopback', True), ('sandbox_lan', False)]:
        try:
            address = '127.0.0.1' if loopback else _lan_address()
            ok = await _canary(vm, address, host_alias=loopback)
            results[name] = 'ok' if ok else 'error'
        except OSError:
            results[name] = 'unavailable'
    results['sandbox_registries'] = 'ok'
    for url in ('https://pypi.org/simple/packaging/', 'https://registry.npmjs.org/typescript/latest'):
        if await _reachable(vm, url):
            results['sandbox_registries'] = 'error'
    results['sandbox_credentials'] = await vm.probe_credentials()
    return results


async def probe_empty_vm(vm: SbxSandbox) -> dict[str, str]:
    results = dict.fromkeys(CHECKS, 'not_run')
    task = TaskContext('probe-' + uuid.uuid4().hex, '', 0, '', '', isolation='sbx')
    try:
        await vm.create_empty(task)
        results = await probe_vm(vm)
    except (SandboxError, OSError, TimeoutError):
        results['sandbox_policy'] = 'error'
    finally:
        await vm.destroy()
    return results
